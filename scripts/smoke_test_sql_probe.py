#!/usr/bin/env python3
"""Engram · smoke test for agent/tools/sql_probe.py, end to end into observe(node).  [BRAINS]

The real payoff test: sets up a genuine scenario on the TARGET cluster (a
table with no index on the filtered column), probes it with SqlProbe (real
EXPLAIN ANALYZE + EXPLAIN), bridges the result through
probe_result_from_explain(), and feeds it into the actual observe(node) —
which writes to the MEMORY cluster. Two clusters, two roles, in one test,
exactly as the architecture describes. Also runs a negative control (an
indexed lookup) to confirm the full-scan detector doesn't just always say yes.

Scenario table is created and dropped via the admin-level ENGRAM_TARGET_DSN
(DDL — SqlProbe itself never does this, it only ever runs EXPLAIN).

    python scripts/smoke_test_sql_probe.py \\
        --target-sslrootcert target-ca.crt --memory-sslrootcert memory-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import psycopg

from agent.memory.db import Database
from agent.nodes.observe import probe_result_from_explain
from agent.nodes.observe import observe as observe_node
from agent.providers.cohere_embed import CohereEmbeddings
from agent.tools.sql_probe import SqlProbe

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def main(target_sslrootcert: str | None, memory_sslrootcert: str | None) -> int:
    marker = uuid.uuid4().hex[:8]
    table = f"smoke_probe_{marker}"
    target_dsn = os.environ["ENGRAM_TARGET_DSN"]
    if target_sslrootcert and "sslrootcert=" not in target_dsn:
        sep = "&" if "?" in target_dsn else "?"
        target_dsn = f"{target_dsn}{sep}sslrootcert={target_sslrootcert}"

    all_ok = True
    scope_id = f"smoke-sql-probe-{marker}"
    task_id = normalized = None

    print(f"\n{RULE}\nSETUP — real scenario on the TARGET cluster (admin DSN, DDL)\n{RULE}")
    admin_conn = await psycopg.AsyncConnection.connect(target_dsn, autocommit=True)
    try:
        async with admin_conn.cursor() as cur:
            await cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, customer_id INT, amount DECIMAL)")
            await cur.execute(
                f"INSERT INTO {table} SELECT g, g % 500, g * 1.5 FROM generate_series(1, 20000) g"
            )
        record("scenario table created + seeded (20k rows, no index on customer_id)", True, table)

        print(f"\n{RULE}\nPROBE — real EXPLAIN ANALYZE + EXPLAIN against the slow query\n{RULE}")
        slow_sql = f"SELECT * FROM {table} WHERE customer_id = 42"
        async with SqlProbe(dsn=target_dsn) as probe:
            explain_result = await probe.explain_analyze(slow_sql)
            record("latency_ms parsed and positive", explain_result.latency_ms > 0,
                   f"{explain_result.latency_ms}ms")
            record("full scan correctly detected", explain_result.has_full_scan is True)
            record("index candidate correctly identified", explain_result.index_candidate == "customer_id",
                   f"{explain_result.index_candidate!r}")

            print(f"\n{RULE}\nNEGATIVE CONTROL — primary key lookup, must NOT show a full scan\n{RULE}")
            pk_result = await probe.explain_analyze(f"SELECT * FROM {table} WHERE id = 42")
            record("primary key lookup is NOT a full scan", pk_result.has_full_scan is False)

        print(f"\n{RULE}\nBRIDGE — real probe result feeds the actual observe(node)\n{RULE}")
        probe_payload = probe_result_from_explain(
            explain_result, query_text=slow_sql, table_name=table,
            target_cluster_id=f"smoke-target-{marker}",
        )
        record("bridged ProbeResult carries the real measured latency",
               probe_payload["probe_latency_ms"] == explain_result.latency_ms)

        db = await Database.connect(sslrootcert=memory_sslrootcert)
        try:
            async with CohereEmbeddings() as embed_provider:
                # 20k rows is a small, fast scenario (~19ms measured) -- well under the
                # production-default 1000ms anomaly threshold. Lowered here on purpose:
                # this test exercises the MECHANISM (does full-scan + latency + index
                # candidate correctly trigger the incident flag), not the production
                # threshold value itself, which is untouched in observe.py.
                state = {"observations": []}
                update = await observe_node(
                    state, db, embed_provider, probe_payload, scope_id=scope_id,
                    latency_threshold_ms=10.0,
                )
                task_id = update["task_id"]
                normalized = update["observations"][0]["payload"]["text"]
                record("observe(node) fired an incident from a REAL probe result",
                       update["incident_fingerprint"] is not None)
                record("observation payload carries the real latency",
                       update["observations"][0]["payload"]["latency_ms"] == explain_result.latency_ms)

                rows = await db._read(
                    "SELECT * FROM memory_items WHERE scope_id = %s AND class = 'query_fingerprint'",
                    (scope_id,),
                )
                record("a real memory_item was written from real target-cluster data",
                       len(rows) == 1, f"{len(rows)} row(s)")
        finally:
            print(f"\n{RULE}\nCLEANUP — memory cluster\n{RULE}")
            try:
                async with db._pool.connection() as conn:
                    async with conn.cursor() as cur:
                        if task_id:
                            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
                        await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
                        if normalized:
                            await cur.execute(
                                "DELETE FROM embedding_cache WHERE content_sha256 = %s",
                                (__import__("hashlib").sha256(
                                    f"search_document:{normalized}".encode()).hexdigest(),),
                            )
                print(f"  cleaned up scope_id={scope_id}, task_id={task_id}")
            except Exception as exc:  # noqa: BLE001
                all_ok = False
                print(f"  MEMORY CLEANUP FAILED: {exc}")
            await db.close()

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP — target cluster\n{RULE}")
        try:
            async with admin_conn.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS {table}")
            print(f"  dropped {table}")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  TARGET CLEANUP FAILED: {exc}")
        await admin_conn.close()

    print(f"\n{RULE}\nRESULT\n{RULE}")
    width = max((len(k) for k, _ in results), default=10)
    failures = [k for k, v in results if v.startswith("FAIL")]
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    print(f"\n  {len(results) - len(failures)}/{len(results)} checks passed")
    return 0 if all_ok and not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-sslrootcert", default=None)
    ap.add_argument("--memory-sslrootcert", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.target_sslrootcert, args.memory_sslrootcert)))
