#!/usr/bin/env python3
"""Engram · smoke test for SqlProbe.get_table_columns() + recipe_renderer.py, live.  [PLUMBER]

Builds a real scenario table on the TARGET cluster, fetches its REAL column
set via SqlProbe (information_schema, not a mock), then proves
recipe_renderer.render() both accepts a genuine column and rejects a
fabricated one using that real schema data — LLD §10's "no fabricated
objects" check, exercised against an actual cluster, not assumed.

    python scripts/smoke_test_recipe_renderer.py --target-sslrootcert target-ca.crt
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

from agent.tools.recipe_renderer import RecipeRejectedError, render
from agent.tools.sql_probe import SqlProbe

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def main(target_sslrootcert: str | None) -> int:
    marker = uuid.uuid4().hex[:8]
    table = f"smoke_recipe_{marker}"
    target_dsn = os.environ["ENGRAM_TARGET_DSN"]
    if target_sslrootcert and "sslrootcert=" not in target_dsn:
        sep = "&" if "?" in target_dsn else "?"
        target_dsn = f"{target_dsn}{sep}sslrootcert={target_sslrootcert}"

    all_ok = True
    admin_conn = await psycopg.AsyncConnection.connect(target_dsn, autocommit=True)

    try:
        print(f"\n{RULE}\nSETUP — real scenario table on the TARGET cluster\n{RULE}")
        async with admin_conn.cursor() as cur:
            await cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, customer_id INT, region STRING)")
        record("scenario table created", True, table)

        print(f"\n{RULE}\nREAL SCHEMA INTROSPECTION — SqlProbe.get_table_columns()\n{RULE}")
        async with SqlProbe(dsn=target_dsn) as probe:
            columns = await probe.get_table_columns(table)
            record("real columns fetched via information_schema",
                   columns == {"id", "customer_id", "region"}, f"{columns}")

            missing = await probe.get_table_columns(f"{table}_does_not_exist")
            record("nonexistent table returns None, not an empty set", missing is None)

        print(f"\n{RULE}\nRENDER — real column accepted, using real schema data\n{RULE}")
        result = render("create_index", {"table": table, "columns": ["customer_id"]},
                         known_columns=columns)
        record("real column accepted, schema_checked=True", result.schema_checked is True)
        record("rendered SQL references the real table", table in result.sql)

        print(f"\n{RULE}\nREJECT — fabricated column rejected against REAL schema data\n{RULE}")
        rejected = False
        try:
            render("create_index", {"table": table, "columns": ["totally_made_up_column"]},
                   known_columns=columns)
        except RecipeRejectedError as exc:
            rejected = True
            record("fabricated column rejected with a clear reason", "fabricated objects" in str(exc))
        record("fabrication was actually rejected (not silently allowed)", rejected)

        print(f"\n{RULE}\nREJECT — nonexistent table rejected via its own None column set\n{RULE}")
        rejected2 = False
        try:
            render("analyze_table", {"table": f"{table}_does_not_exist"}, known_columns=missing or set())
        except RecipeRejectedError:
            rejected2 = True
        record("nonexistent table rejected", rejected2)

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        try:
            async with admin_conn.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS {table}")
            print(f"  dropped {table}")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  CLEANUP FAILED: {exc}")
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
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.target_sslrootcert)))
