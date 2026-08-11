#!/usr/bin/env python3
"""Engram · one-time bootstrap for engram_reader (dashboard SSE) on the MEMORY cluster.  [PLUMBER]

design/02-low-level-design.md §2 (`ENGRAM_READER_DSN`) / §11 (dashboard SSE) / HLD §5.6: "No DB
credentials in the frontend; only the engram_reader DSN is present in the *serverless function*
(read-only)." The role itself already exists -- `db/migrations/002_grants.sql` created it with
`GRANT SELECT ON v_recent_tasks, v_action_feed, v_memory_inspector, observations TO
engram_reader` -- but `CREATE ROLE engram_reader;` has no LOGIN/PASSWORD clause, so it has never
actually been able to connect. Same shape of gap `scripts/bootstrap_target_roles.py` closed for
the target cluster's probe/operator roles last session; this closes the equivalent one for the
dashboard.

Sets a random password (never printed), constructs `ENGRAM_READER_DSN`, writes it into `.env`,
then LIVE-VERIFIES the actual privilege boundary:
  - SELECT on the three dashboard views + `observations` succeeds (the only four grants LLD
    names).
  - SELECT directly on an underlying base table the views join (`remediation_actions`,
    `decisions`) FAILS -- proving the view-level grant doesn't leak base-table access (CockroachDB
    views execute with the view owner's privileges, same as Postgres).
  - Any mutation (INSERT) fails.

    python scripts/bootstrap_reader_role.py --sslrootcert memory-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import secrets
import sys
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import psycopg
from psycopg import sql

ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / ".env"
RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


def _dsn_with_credentials(base_dsn: str, user: str, password: str) -> str:
    p = urlparse(base_dsn)
    netloc = f"{user}:{password}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _upsert_env_var(key: str, value: str) -> None:
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}\n"
            ENV_PATH.write_text("".join(lines))
            return
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(f"{prefix}{value}\n")
    ENV_PATH.write_text("".join(lines))


async def _try_as(dsn: str, sslrootcert: str | None, stmt: str) -> tuple[bool, str]:
    full_dsn = dsn
    if sslrootcert and "sslrootcert=" not in full_dsn:
        sep = "&" if "?" in full_dsn else "?"
        full_dsn = f"{full_dsn}{sep}sslrootcert={sslrootcert}"
    try:
        async with await psycopg.AsyncConnection.connect(full_dsn, autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(stmt)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def main(sslrootcert: str | None) -> int:
    admin_dsn = os.environ["ENGRAM_MEMORY_DSN"]
    full_admin_dsn = admin_dsn
    if sslrootcert and "sslrootcert=" not in full_admin_dsn:
        sep = "&" if "?" in full_admin_dsn else "?"
        full_admin_dsn = f"{full_admin_dsn}{sep}sslrootcert={sslrootcert}"

    print(f"{RULE}\nSTEP 1 -- confirm engram_reader exists (created by migration 002)\n{RULE}")
    conn = await psycopg.AsyncConnection.connect(full_admin_dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT rolname FROM pg_roles WHERE rolname = 'engram_reader'")
            exists = (await cur.fetchone()) is not None
        record("engram_reader role exists", exists)
        if not exists:
            print("  ABORT: run db/migrations/002_grants.sql first.")
            return 1

        print(f"\n{RULE}\nSTEP 2 -- set a fresh random password\n{RULE}")
        password = secrets.token_urlsafe(24)
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
                .format(sql.Identifier("engram_reader"), sql.Literal(password))
            )
        print("  password set for engram_reader (not printed)")

        reader_dsn = _dsn_with_credentials(admin_dsn, "engram_reader", password)
        _upsert_env_var("ENGRAM_READER_DSN", reader_dsn)
        print("  wrote ENGRAM_READER_DSN to .env")

        print(f"\n{RULE}\nSTEP 3 -- live-verify the privilege boundary (measured, not assumed)\n{RULE}")

        for view in ("v_recent_tasks", "v_action_feed", "v_memory_inspector"):
            ok, detail = await _try_as(reader_dsn, sslrootcert, f"SELECT * FROM {view} LIMIT 1")
            record(f"engram_reader: SELECT {view} succeeds", ok, detail)

        ok, detail = await _try_as(reader_dsn, sslrootcert, "SELECT * FROM observations LIMIT 1")
        record("engram_reader: SELECT observations succeeds", ok, detail)

        # migration 005: LLD §11.1's own frozen SSE table names an `approvals` feed reading the
        # base table directly -- migration 002 never granted it, caught while building the
        # dashboard. Checked here so a future re-run of this script re-verifies it stays granted.
        ok, detail = await _try_as(reader_dsn, sslrootcert, "SELECT * FROM approvals LIMIT 1")
        record("engram_reader: SELECT approvals succeeds (migration 005)", ok, detail)

        ok, detail = await _try_as(
            reader_dsn, sslrootcert, "SELECT * FROM remediation_actions LIMIT 1"
        )
        record("engram_reader: SELECT remediation_actions (base table) correctly FAILS",
               not ok, detail)

        ok, detail = await _try_as(reader_dsn, sslrootcert, "SELECT * FROM decisions LIMIT 1")
        record("engram_reader: SELECT decisions (not granted) correctly FAILS", not ok, detail)

        ok, detail = await _try_as(
            reader_dsn, sslrootcert,
            "INSERT INTO observations (task_id, source, kind) VALUES (gen_random_uuid(), 'x', 'x')",
        )
        record("engram_reader: INSERT correctly FAILS (read-only)", not ok, detail)

    finally:
        await conn.close()

    print(f"\n{RULE}\nRESULT\n{RULE}")
    width = max((len(k) for k, _ in results), default=10)
    failures = [k for k, v in results if v.startswith("FAIL")]
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    print(f"\n  {len(results) - len(failures)}/{len(results)} checks passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser()
    ap.add_argument("--sslrootcert", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.sslrootcert)))
