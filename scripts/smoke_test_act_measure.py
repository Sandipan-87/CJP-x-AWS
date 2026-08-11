#!/usr/bin/env python3
"""Engram · smoke test for agent/nodes/act_measure.py -- applies a REAL index, for real.  [BRAINS + PLUMBER]

The closing piece of the loop: a real scenario on the target cluster, a
real rendered CREATE INDEX (via recipe_renderer), applied for real via
SqlOperator, with real before/after EXPLAIN ANALYZE measurements proving
an actual latency improvement -- then confirms the index genuinely exists
in the target cluster's own catalog afterward, not just that no exception
was raised. Uses override_backup_gate=True (no CCLOUD_TOKEN provisioned,
see cloud_api.py's module docstring) -- the audited escape hatch LLD names,
not a workaround of the gate.

    python scripts/smoke_test_act_measure.py \\
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
from agent.nodes.act_measure import act_measure
from agent.tools import recipe_renderer
from agent.tools.sql_operator import SqlOperator
from agent.tools.sql_probe import SqlProbe

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def main(target_sslrootcert: str | None, memory_sslrootcert: str | None) -> int:
    marker = uuid.uuid4().hex[:8]
    table = f"smoke_act_{marker}"
    scope_id = f"smoke-act-{marker}"
    target_dsn = os.environ["ENGRAM_TARGET_DSN"]
    if target_sslrootcert and "sslrootcert=" not in target_dsn:
        sep = "&" if "?" in target_dsn else "?"
        target_dsn = f"{target_dsn}{sep}sslrootcert={target_sslrootcert}"

    all_ok = True
    admin_conn = await psycopg.AsyncConnection.connect(target_dsn, autocommit=True)
    db = await Database.connect(sslrootcert=memory_sslrootcert)
    task_id: str | None = None

    try:
        print(f"\n{RULE}\nSETUP — real scenario on the TARGET cluster (no index yet)\n{RULE}")
        async with admin_conn.cursor() as cur:
            await cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, customer_id INT)")
            await cur.execute(f"INSERT INTO {table} SELECT g, g % 500 FROM generate_series(1, 40000) g")
        record("scenario table created + seeded (40k rows)", True, table)

        slow_sql = f"SELECT * FROM {table} WHERE customer_id = 42"

        print(f"\n{RULE}\nLEDGER SETUP — a real remediation_actions row (simulating gate() already ran)\n{RULE}")
        task_id = await db.insert_task(scope_id, "incident", "manual")
        rendered = recipe_renderer.render("create_index", {"table": table, "columns": ["customer_id"]})
        record("recipe_renderer produced real, idempotent SQL", "IF NOT EXISTS" in rendered.sql, rendered.sql)
        action_id = await db.insert_remediation_action(
            task_id, scope_id, f"smoke-target-{marker}", "create_index", "v1",
            {"table": table, "columns": ["customer_id"]}, rendered.sql,
            f"smoke-act-{marker}", "proposed",
        )
        record("real remediation_actions row created", True, action_id)

        state = {
            "task_id": task_id, "scope_id": scope_id, "target_cluster_id": f"smoke-target-{marker}",
            "observations": [{"payload": {"raw_text": slow_sql}}],
            "action": {"action_id": action_id, "status": "approved", "rendered_sql": rendered.sql},
            "proposal": {"action_kind": "create_index", "parameters": {"table": table}},
        }

        print(f"\n{RULE}\nACT_MEASURE — real before/after EXPLAIN ANALYZE + real DDL apply\n{RULE}")
        async with SqlProbe(dsn=target_dsn) as probe, SqlOperator(dsn=target_dsn) as operator:
            update = await act_measure(state, db, probe, operator, override_backup_gate=True)

        record("phase is 'done'", update["phase"] == "done")
        before_ms = update["measurement"]["measured_before"]["latency_ms"]
        after_ms = update["measurement"]["measured_after"]["latency_ms"]
        record("measured_before shows a full scan", update["measurement"]["measured_before"]["has_full_scan"])
        record("outcome is 'success' (real latency actually improved)",
               update["measurement"]["outcome"] == "success", f"{before_ms:.1f}ms -> {after_ms:.1f}ms")
        record("action status updated to 'applied'", update["action"]["status"] == "applied")

        print(f"\n{RULE}\nCONFIRM — the index genuinely exists in the target cluster's own catalog\n{RULE}")
        async with admin_conn.cursor() as cur:
            # SHOW INDEXES doesn't take a bind parameter for the table name.
            await cur.execute(f"SHOW INDEXES FROM {table}")
            indexes = await cur.fetchall()
        index_names = {row[1] for row in indexes}
        record("a real secondary index now exists on the real table",
               any(table in name for name in index_names if name != f"{table}_pkey"),
               f"{index_names}")

        print(f"\n{RULE}\nCONFIRM — real DB state after the outcome txn\n{RULE}")
        action_row = await db.get_by_idempotency_key(f"smoke-act-{marker}")
        record("remediation_actions.status is 'applied' in the DB", action_row["status"] == "applied")
        record("remediation_actions.outcome is 'success' in the DB", action_row["outcome"] == "success")
        episode_rows = await db._read(
            "SELECT item_id FROM memory_items WHERE scope_id = %s AND class = 'episode'", (scope_id,)
        )
        record("a real episode memory_item was written", len(episode_rows) == 1, f"{len(episode_rows)} row(s)")

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        try:
            async with admin_conn.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS {table}")
            print(f"  dropped {table} (and its index, along with it)")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  TARGET CLEANUP FAILED: {exc}")
        await admin_conn.close()

        try:
            async with db._pool.connection() as conn:
                async with conn.cursor() as cur:
                    if task_id:
                        await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
                    await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
            print(f"  cleaned up memory cluster: scope_id={scope_id}, task_id={task_id}")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  MEMORY CLEANUP FAILED: {exc}")
        await db.close()

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
