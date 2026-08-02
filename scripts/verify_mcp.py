#!/usr/bin/env python3
"""Engram · PHASE 0 · P0-B2 — Managed MCP server limits verification.  Role: [BRAINS]

Connects to https://cockroachlabs.cloud/mcp with a service-account API key and
EMPIRICALLY measures the four constraints CLAUDE.md §4 depends on, instead of
trusting the docs:

    A. 10 KiB max response      -> measured bytes per tool result
    B. 20 s query timeout       -> wall-clock ceiling observed on a slow query
    C. SELECT defaults LIMIT 25 -> row count from an unlimited SELECT
    D. deny-listed schemas      -> crdb_internal / pg_catalog / information_schema

Setup:
    pip install -r scripts/requirements-verify.txt
    export CRDB_MCP_TOKEN=<service-account API key>     # scope: mcp:read
    export CRDB_MCP_CLUSTER_ID=<memory cluster uuid>    # pins mcp-cluster-id
    # optional, enables probes C and D against the Phase 0 probe table:
    export CRDB_MCP_DATABASE=defaultdb
    export CRDB_MCP_PROBE_TABLE=vec_probe
    python scripts/verify_mcp.py

Run this WHILE db/phase0_vector_probe.sql has vec_probe seeded (i.e. between
its STEP 3 and STEP 8) to get the most informative LIMIT-25 evidence.

Exit 0 = connected and all measurements captured.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time

# Load .env from the project root (this file lives in scripts/), so the script
# works without `source .env` first.
try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    sys.exit("FATAL: mcp not installed. pip install -r scripts/requirements-verify.txt")

MCP_URL = os.environ.get("CRDB_MCP_URL", "https://cockroachlabs.cloud/mcp")
TOKEN = os.environ.get("CRDB_MCP_TOKEN")
CLUSTER_ID = os.environ.get("CRDB_MCP_CLUSTER_ID")
DATABASE = os.environ.get("CRDB_MCP_DATABASE")
PROBE_TABLE = os.environ.get("CRDB_MCP_PROBE_TABLE", "vec_probe")

DOCUMENTED_MAX_BYTES = 10 * 1024
DOCUMENTED_TIMEOUT_S = 20.0
DOCUMENTED_DEFAULT_LIMIT = 25
DOCUMENTED_SQL_CHAR_LIMIT = 16_384

RULE = "-" * 72
results: list[tuple[str, str]] = []


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def record(key: str, value: str) -> None:
    results.append((key, value))
    print(f"  >> {key}: {value}")


def result_text(result) -> str:
    """Flatten an MCP CallToolResult to the text a model would actually see."""
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(repr(block))
    if not parts and getattr(result, "structuredContent", None) is not None:
        parts.append(json.dumps(result.structuredContent))
    return "\n".join(parts)


async def timed_call(session: ClientSession, tool: str, args: dict) -> tuple[float, str, bool]:
    """Return (elapsed_seconds, payload_text, is_error). Never raises."""
    t0 = time.perf_counter()
    try:
        res = await session.call_tool(tool, args)
        elapsed = time.perf_counter() - t0
        return elapsed, result_text(res), bool(getattr(res, "isError", False))
    except Exception as exc:  # noqa: BLE001 - transport/protocol/timeouts all matter equally here
        return time.perf_counter() - t0, f"{type(exc).__name__}: {exc}", True


def show(elapsed: float, payload: str, is_error: bool, *, preview: int = 600) -> int:
    raw = payload.encode("utf-8")
    n = len(raw)
    print(f"  elapsed : {elapsed:.3f} s")
    print(f"  bytes   : {n:,}  ({n / 1024:.2f} KiB of the {DOCUMENTED_MAX_BYTES // 1024} KiB ceiling)")
    print(f"  isError : {is_error}")
    body = payload if len(payload) <= preview else payload[:preview] + f"… [+{len(payload) - preview} chars]"
    print("  payload :")
    for line in body.splitlines() or [""]:
        print(f"    {line}")
    return n


async def run() -> int:
    if not TOKEN:
        sys.exit("FATAL: CRDB_MCP_TOKEN is not set.")
    if not CLUSTER_ID:
        sys.exit("FATAL: CRDB_MCP_CLUSTER_ID is not set (the mcp-cluster-id pin is mandatory).")

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "mcp-cluster-id": CLUSTER_ID,
    }

    head(f"CONNECT  {MCP_URL}")
    print(f"  mcp-cluster-id : {CLUSTER_ID}")
    print(f"  token          : …{TOKEN[-6:]} (len {len(TOKEN)})")

    t_connect = time.perf_counter()
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            record("connect+initialize seconds", f"{time.perf_counter() - t_connect:.3f}")
            srv = getattr(init, "serverInfo", None)
            if srv is not None:
                record("server", f"{getattr(srv, 'name', '?')} {getattr(srv, 'version', '?')}")
            record("protocolVersion", str(getattr(init, "protocolVersion", "?")))

            # ---------------------------------------------------------------
            # Tool inventory — confirm no write tools were granted to this key
            # ---------------------------------------------------------------
            head("TOOL INVENTORY  (confirm mcp:read only — no write tools)")
            listed = await session.list_tools()
            names = sorted(t.name for t in listed.tools)
            for t in listed.tools:
                desc = (t.description or "").replace("\n", " ")[:88]
                print(f"    {t.name:<24} {desc}")
            record("tool count", str(len(names)))
            record("tools", ", ".join(names))
            writeish = [n for n in names if any(k in n.lower() for k in ("insert", "update", "delete", "create", "drop", "alter", "write", "execute"))]
            record("write-capable tools exposed", ", ".join(writeish) if writeish else "NONE (correct)")

            # ---------------------------------------------------------------
            # A. list_clusters — size + latency baseline
            # ---------------------------------------------------------------
            head("PROBE A  list_clusters — response size & latency (10 KiB / 20 s limits)")
            elapsed, payload, is_error = await timed_call(session, "list_clusters", {})
            n = show(elapsed, payload, is_error)
            record("list_clusters bytes", f"{n:,}")
            record("list_clusters seconds", f"{elapsed:.3f}")
            record(
                "10 KiB ceiling",
                f"{'not reached' if n < DOCUMENTED_MAX_BYTES else 'REACHED/TRUNCATED'} "
                f"({n} vs {DOCUMENTED_MAX_BYTES})",
            )

            # ---------------------------------------------------------------
            # B. 20 s timeout — a query engineered to exceed it
            # ---------------------------------------------------------------
            head("PROBE B  20 s query timeout — deliberately slow SELECT")
            slow_sql = (
                "SELECT count(*) FROM generate_series(1, 2000000000) AS g(i) "
                "WHERE (i * 2654435761) % 7 = 3"
            )
            print(f"  sql: {slow_sql}")
            elapsed, payload, is_error = await timed_call(session, "select_query", {"database": DATABASE or "defaultdb", "query": slow_sql})
            show(elapsed, payload, is_error, preview=400)
            record("slow-query seconds", f"{elapsed:.3f}")
            record("slow-query errored", str(is_error))
            record(
                "server-side timeout",
                f"~{elapsed:.1f}s (docs claim {DOCUMENTED_TIMEOUT_S:.0f}s) — "
                + ("consistent" if is_error and elapsed <= DOCUMENTED_TIMEOUT_S + 8 else "REVIEW THIS"),
            )
            print(
                "  >>> Phase 2 P2-B3 depends on this number: the client timeout must sit\n"
                "  >>> INSIDE it (15 s), so the agent sees a typed timeout, not a hang."
            )

            # ---------------------------------------------------------------
            # C. default LIMIT 25 on an unlimited SELECT
            # ---------------------------------------------------------------
            head(f"PROBE C  implicit LIMIT — unlimited SELECT against {PROBE_TABLE}")
            if not DATABASE:
                print("  SKIPPED: set CRDB_MCP_DATABASE (and seed vec_probe) to run this probe.")
                record("default LIMIT", "NOT MEASURED (probe skipped)")
            else:
                unbounded = f"SELECT id, scope_id, label FROM {PROBE_TABLE}"
                print(f"  sql: {unbounded}   (no LIMIT clause)")
                elapsed, payload, is_error = await timed_call(
                    session, "select_query", {"database": DATABASE, "query": unbounded}
                )
                n = show(elapsed, payload, is_error, preview=500)
                # Row-count heuristics: prefer JSON, fall back to line counting.
                rows = None
                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, list):
                        rows = len(parsed)
                    elif isinstance(parsed, dict):
                        for k in ("rows", "results", "data"):
                            if isinstance(parsed.get(k), list):
                                rows = len(parsed[k])
                                break
                except json.JSONDecodeError:
                    rows = max(0, len([ln for ln in payload.splitlines() if ln.strip()]) - 1)
                record("rows returned without LIMIT", str(rows))
                record(
                    "default LIMIT confirmed",
                    "YES (25)" if rows == DOCUMENTED_DEFAULT_LIMIT else f"NO — got {rows}, docs say {DOCUMENTED_DEFAULT_LIMIT}",
                )
                record("bytes for that result", f"{n:,}")

                explicit = f"SELECT id FROM {PROBE_TABLE} LIMIT 400"
                print(f"\n  sql: {explicit}   (explicit LIMIT above the default)")
                elapsed, payload, is_error = await timed_call(
                    session, "select_query", {"database": DATABASE, "query": explicit}
                )
                n2 = show(elapsed, payload, is_error, preview=300)
                record("explicit LIMIT 400 bytes", f"{n2:,}")
                record(
                    "explicit LIMIT 400 vs 10 KiB",
                    "truncated/errored — 10 KiB binds before LIMIT does"
                    if (is_error or n2 >= DOCUMENTED_MAX_BYTES)
                    else "fit under the ceiling",
                )

            # ---------------------------------------------------------------
            # D. deny-listed schemas must be refused
            # ---------------------------------------------------------------
            head("PROBE D  deny-listed schemas (expect refusal on every one)")
            for sql in (
                "SELECT * FROM crdb_internal.cluster_queries LIMIT 1",
                "SELECT * FROM information_schema.tables LIMIT 1",
                "SELECT * FROM pg_catalog.pg_class LIMIT 1",
            ):
                elapsed, payload, is_error = await timed_call(session, "select_query", {"database": DATABASE or "defaultdb", "query": sql})
                first = (payload.splitlines() or [""])[0][:110]
                status = "REFUSED (correct)" if is_error else "!! ALLOWED — deny-list assumption is WRONG"
                print(f"    {sql[:52]:<54} {status}")
                print(f"      {first}")
                record(f"deny-list · {sql.split('.')[0].split()[-1]}", status)

            # ---------------------------------------------------------------
            # E. explain_query — the tool Reason depends on (P2-B4)
            # ---------------------------------------------------------------
            head("PROBE E  explain_query — hypothesis-falsification tool (P2-B4)")
            if not DATABASE:
                print("  SKIPPED: set CRDB_MCP_DATABASE to run this probe.")
                record("explain_query", "NOT MEASURED (probe skipped)")
            else:
                sql = f"SELECT id FROM {PROBE_TABLE} WHERE scope_id = 'org-alpha' LIMIT 3"
                elapsed, payload, is_error = await timed_call(
                    session, "explain_query", {"database": DATABASE, "query": sql}
                )
                n = show(elapsed, payload, is_error, preview=900)
                record("explain_query bytes", f"{n:,}")
                record("explain_query seconds", f"{elapsed:.3f}")
                record("explain_query usable", "no" if is_error else "yes")

            # ---------------------------------------------------------------
            # F. 16,384-char SQL limit
            # ---------------------------------------------------------------
            head(f"PROBE F  SQL length limit (docs: {DOCUMENTED_SQL_CHAR_LIMIT:,} chars)")
            padding = "-- " + ("x" * 200) + "\n"
            oversized = padding * 90 + "SELECT 1"   # ~18.3k chars
            print(f"  sql length: {len(oversized):,} chars (deliberately over the limit)")
            elapsed, payload, is_error = await timed_call(session, "select_query", {"database": DATABASE or "defaultdb", "query": oversized})
            show(elapsed, payload, is_error, preview=300)
            record("oversized SQL rejected", "yes (correct)" if is_error else "NO — limit is higher than documented")

            # ---------------------------------------------------------------
            # G. index introspection over MCP.
            # `SHOW INDEXES FROM vec_probe` returns "Internal error" on
            # CockroachDB CCL v26.2.1 when the table carries a vector index,
            # and SHOW CREATE TABLE silently omits it. This probe asks whether
            # the MCP control plane has the same blind spot — which decides
            # whether P2-B3 can trust it for self-diagnosis at all.
            # ---------------------------------------------------------------
            head("PROBE G  get_table_schema — does MCP see the vector index?")
            tool_schemas = {t.name: (t.inputSchema or {}) for t in listed.tools}
            if "get_table_schema" not in tool_schemas:
                print("  get_table_schema is not offered by this server — skipping.")
                record("get_table_schema", "TOOL NOT OFFERED")
            elif not DATABASE:
                print("  SKIPPED: set CRDB_MCP_DATABASE to run this probe.")
                record("get_table_schema", "NOT MEASURED (probe skipped)")
            else:
                decl = tool_schemas["get_table_schema"]
                props = list((decl.get("properties") or {}).keys())
                print(f"  declared properties : {props}")
                print(f"  declared required   : {decl.get('required')}")

                # Build args from whatever the server actually declares rather
                # than guessing a fixed shape.
                built: dict[str, str] = {}
                for key, val in (
                    ("database", DATABASE), ("database_name", DATABASE),
                    ("schema", "public"), ("schema_name", "public"),
                    ("table", PROBE_TABLE), ("table_name", PROBE_TABLE),
                ):
                    if key in props:
                        built[key] = val
                attempts = [built] if built else []
                fallback = {"database": DATABASE, "table": PROBE_TABLE}
                if fallback != built:
                    attempts.append(fallback)

                for args_ in attempts:
                    print(f"\n  get_table_schema({args_})")
                    elapsed, payload, is_error = await timed_call(session, "get_table_schema", args_)
                    n = show(elapsed, payload, is_error, preview=1200)
                    if is_error:
                        continue
                    saw_index = "vec_probe_scope_cos" in payload
                    saw_vector = "VECTOR" in payload.upper()
                    record("get_table_schema bytes", f"{n:,}")
                    record("MCP sees VECTOR(1024) column", "yes" if saw_vector else "no")
                    record(
                        "MCP sees the vector INDEX",
                        "YES — vec_probe_scope_cos visible"
                        if saw_index
                        else "NO — same introspection blind spot as SHOW INDEXES",
                    )
                    break
                else:
                    record("get_table_schema", "every argument shape failed — see output above")

            # ---------------------------------------------------------------
            # H. show_statement — a SECOND path to the index artifact.
            # Its own description advertises 'SHOW INDEXES FROM mytable' and
            # 'SHOW CREATE TABLE mytable', and it executes server-side via the
            # Cloud API rather than through the console UI that returned
            # "Internal error". If it works, it is the P0-P1 §1.2 artifact.
            # ---------------------------------------------------------------
            head("PROBE H  show_statement — SHOW INDEXES via MCP (console UI errored)")
            if "show_statement" not in {t.name for t in listed.tools}:
                print("  show_statement not offered — skipping.")
                record("show_statement", "TOOL NOT OFFERED")
            elif not DATABASE:
                print("  SKIPPED: set CRDB_MCP_DATABASE.")
                record("show_statement", "NOT MEASURED (probe skipped)")
            else:
                for stmt in (f"SHOW INDEXES FROM {PROBE_TABLE}",
                             f"SHOW CREATE TABLE {PROBE_TABLE}",
                             "SHOW DATABASES"):
                    print(f"\n  show_statement({stmt!r})")
                    elapsed, payload, is_error = await timed_call(
                        session, "show_statement", {"database": DATABASE, "query": stmt}
                    )
                    n = show(elapsed, payload, is_error, preview=900)
                    key = stmt.split(" FROM ")[0].split(" TABLE ")[0].strip()
                    if is_error:
                        record(f"show_statement · {key}", f"ERROR — {payload.splitlines()[0][:80]}")
                    else:
                        hit = "vec_probe_scope_cos" in payload
                        record(
                            f"show_statement · {key}",
                            "OK — vector index VISIBLE" if hit else f"OK ({n:,} bytes), vector index not named",
                        )

    head("P0-B2 MEASURED LIMITS  (copy this block into docs/phase0-verification.md)")
    width = max(len(k) for k, _ in results)
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    print(
        "\n  Reminder (CLAUDE.md §4): MCP is a CONTROL PLANE. These limits are why\n"
        "  hot-path memory reads go through psycopg3, never through MCP."
    )
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\nFATAL: {type(exc).__name__}: {exc}")
        print(
            "\nTriage:\n"
            "  401/403        -> key lacks mcp:read, or the service account is not\n"
            "                    Cluster Operator on this cluster.\n"
            "  404 / bad path -> confirm CRDB_MCP_URL; the endpoint is /mcp.\n"
            "  cluster errors -> CRDB_MCP_CLUSTER_ID must be the cluster UUID, not its name.\n"
            "  TaskGroup/ExceptionGroup noise -> that is anyio wrapping the real cause;\n"
            "                    the innermost message above is the one that matters."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
