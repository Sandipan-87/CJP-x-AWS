#!/usr/bin/env python3
"""Engram · one-time bootstrap for AsyncCockroachDBSaver on the MEMORY cluster.  [PLUMBER]

design/02-low-level-design.md §6.3 step 3 / db/migrations/README.md step 4:
`saver.setup()` must run on an EMPTY cluster, then migration 004's TTL must
apply IMMEDIATELY after -- before any checkpoint row exists (invariant #7:
adding TTL to a hot table forces a full rewrite). Doing both in one script
run, one process, back-to-back, closes the gap that two separate manual
steps (setup via a throwaway script, then 004 via the Console or db-migrate.yml)
would otherwise leave open.

Reads db/migrations/004_checkpoint_ttl.sql directly rather than duplicating
its SQL here -- one copy of the TTL statements, not two that can drift. Uses
a plain psycopg connection for everything except the `setup()` call itself
(the one thing only the library can do) -- no reliance on the saver's
private `_cursor()` internals.

    python scripts/bootstrap_checkpointer.py --sslrootcert memory-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import psycopg
from psycopg.rows import dict_row

from langchain_cockroachdb import AsyncCockroachDBSaver

MIGRATION_004 = pathlib.Path(__file__).resolve().parent.parent / "db/migrations/004_checkpoint_ttl.sql"
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

RULE = "-" * 72


def _statements_from_sql_file(path: pathlib.Path) -> list[str]:
    """Strip `--` comment lines, split on `;`. Safe here because the only
    non-comment content is three ALTER TABLE statements with no embedded
    semicolons inside their $$...$$ dollar-quoted literals.
    """
    lines = [ln for ln in path.read_text().splitlines() if not ln.strip().startswith("--")]
    text = "\n".join(lines)
    return [s.strip() for s in text.split(";") if s.strip()]


async def _table_exists(cur, table: str) -> bool:
    await cur.execute(
        "SELECT count(*) AS n FROM information_schema.tables WHERE table_name = %s", (table,)
    )
    return (await cur.fetchone())["n"] > 0


async def main(sslrootcert: str | None) -> int:
    dsn = os.environ["ENGRAM_MEMORY_DSN"]
    if sslrootcert and "sslrootcert=" not in dsn:
        sep = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{sep}sslrootcert={sslrootcert}"

    statements = _statements_from_sql_file(MIGRATION_004)
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True, row_factory=dict_row)

    try:
        print(f"{RULE}\nSTEP 0 -- refuse to run against a non-empty checkpoint cluster\n{RULE}")
        async with conn.cursor() as cur:
            for table in CHECKPOINT_TABLES:
                if await _table_exists(cur, table):
                    await cur.execute(f"SELECT count(*) AS n FROM {table}")
                    n = (await cur.fetchone())["n"]
                    if n > 0:
                        print(f"  ABORT: {table} already has {n} row(s) -- not an empty cluster. "
                              "setup()+TTL must run before any checkpoint data exists (invariant #7). "
                              "Nothing was changed.")
                        return 1
            print("  clean: no checkpoint tables exist yet, or they exist and are empty.")

        print(f"\n{RULE}\nSTEP 1 -- AsyncCockroachDBSaver.setup()\n{RULE}")
        async with AsyncCockroachDBSaver.from_conn_string(dsn) as saver:
            await saver.setup()
        print("  setup() complete.")

        async with conn.cursor() as cur:
            for table in CHECKPOINT_TABLES:
                exists = await _table_exists(cur, table)
                print(f"  table exists after setup(): {table} -> {exists}")
                if not exists:
                    print(f"  ABORT: {table} missing after setup() -- not proceeding to TTL.")
                    return 1

        print(f"\n{RULE}\nSTEP 2 -- apply migration 004 (TTL) IMMEDIATELY, same process\n{RULE}")
        async with conn.cursor() as cur:
            for stmt in statements:
                await cur.execute(stmt)
                print(f"  executed: {stmt.splitlines()[0]}...")

        print(f"\n{RULE}\nSTEP 3 -- verify TTL actually landed (measured, not assumed)\n{RULE}")
        all_ok = True
        async with conn.cursor() as cur:
            for table in CHECKPOINT_TABLES:
                await cur.execute(f"SHOW CREATE TABLE {table}")
                ddl = (await cur.fetchone())["create_statement"]
                has_ttl = "ttl_expiration_expression" in ddl
                all_ok = all_ok and has_ttl
                print(f"  {table}: ttl_expiration_expression present -> {has_ttl}")
                await cur.execute(f"SELECT count(*) AS n FROM {table}")
                n = (await cur.fetchone())["n"]
                print(f"  {table}: row count -> {n} (expected 0, still empty)")
                all_ok = all_ok and n == 0

        print(f"\n{RULE}\nRESULT\n{RULE}")
        print("  ALL OK" if all_ok else "  FAILED -- see above")
        return 0 if all_ok else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser()
    ap.add_argument("--sslrootcert", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.sslrootcert)))
