#!/usr/bin/env python3
"""Engram · smoke test for agent/nodes/gate.py against the live memory cluster.  [BRAINS]

Three real scenarios: (1) a human "approves" mid-poll by writing directly
to `approvals` while gate() is actually polling in real time -- proves the
polling mechanism itself, not just the branch logic already proven by the
scripted unit tests; (2) a rejection, confirming the real outcome write
(remediation_actions.status='skipped') and the real episode memory row
both actually land in the database; (3) calling insert_gate_decision a
SECOND time with the SAME idempotency_key, confirming real reconciliation
onto the existing row rather than a second ledger entry.

    python scripts/smoke_test_gate_node.py --sslrootcert cluster-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from agent.memory.db import Database
from agent.nodes.gate import _compute_idempotency_key, gate

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


def _proposal(columns: list[str]) -> dict:
    return {
        "action_kind": "create_index",
        "parameters": {"table": "orders", "columns": columns},
        "citations": [],
        "reasoning": "smoke test",
    }


async def _approve_soon(db: Database, idempotency_key: str, delay_s: float) -> None:
    """Simulates a human clicking "approve" on a dashboard, concurrently
    with gate()'s own real polling loop -- not pre-seeded before gate()
    starts, and not mocked.
    """
    await asyncio.sleep(delay_s)
    row = await db.get_by_idempotency_key(idempotency_key)
    approvals = await db._read(
        "SELECT approval_id FROM approvals WHERE action_id = %s", (row["action_id"],)
    )
    await db.decide_approval(approvals[0]["approval_id"], "smoke-test-human", "approved")


async def main(sslrootcert: str | None) -> int:
    scope_id = f"smoke-gate-{uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex[:8]
    db = await Database.connect(sslrootcert=sslrootcert)
    print(f"connected, scope_id={scope_id}")
    all_ok = True
    task_ids: list[str] = []

    try:
        print(f"\n{RULE}\nSCENARIO 1 — real concurrent approval mid-poll\n{RULE}")
        task_id_1 = await db.insert_task(scope_id, "incident", "manual")
        task_ids.append(task_id_1)
        state1 = {"task_id": task_id_1, "scope_id": scope_id,
                   "target_cluster_id": f"smoke-target-{marker}-a",
                   "proposal": _proposal([f"customer_id_{marker}"])}
        key1 = _compute_idempotency_key(state1["target_cluster_id"], state1["proposal"])

        approver = asyncio.create_task(_approve_soon(db, key1, delay_s=2.5))
        update1 = await gate(state1, db, poll_interval_s=1.0, timeout_s=15.0)
        await approver
        record("gate() picked up a REAL concurrent approval", update1["phase"] == "gate",
               f"phase={update1['phase']!r}")
        record("approval status is 'approved'", update1["approval"]["status"] == "approved")

        print(f"\n{RULE}\nSCENARIO 2 — rejection writes a real outcome + episode row\n{RULE}")
        task_id_2 = await db.insert_task(scope_id, "incident", "manual")
        task_ids.append(task_id_2)
        state2 = {"task_id": task_id_2, "scope_id": scope_id,
                   "target_cluster_id": f"smoke-target-{marker}-b",
                   "proposal": _proposal([f"region_{marker}"])}
        key2 = _compute_idempotency_key(state2["target_cluster_id"], state2["proposal"])

        async def _reject_soon():
            await asyncio.sleep(1.0)
            row = await db.get_by_idempotency_key(key2)
            approvals = await db._read(
                "SELECT approval_id FROM approvals WHERE action_id = %s", (row["action_id"],)
            )
            await db.decide_approval(approvals[0]["approval_id"], "smoke-test-human", "rejected")

        rejecter = asyncio.create_task(_reject_soon())
        update2 = await gate(state2, db, poll_interval_s=0.5, timeout_s=10.0)
        await rejecter
        record("rejected outcome routes to phase='done'", update2["phase"] == "done")

        action_row = await db.get_by_idempotency_key(key2)
        record("remediation_actions.status really is 'skipped' in the DB",
               action_row["status"] == "skipped", action_row["status"])
        episode_rows = await db._read(
            "SELECT item_id FROM memory_items WHERE scope_id = %s AND class = 'episode'",
            (scope_id,),
        )
        record("a real episode memory_item was written", len(episode_rows) == 1,
               f"{len(episode_rows)} row(s)")

        print(f"\n{RULE}\nSCENARIO 3 — real idempotent reconciliation, no second ledger entry\n{RULE}")
        decision_id, action_id_again, approval_id_again = await db.insert_gate_decision(
            task_id_2, scope_id, state2["target_cluster_id"],
            model_id="smoke-test", reasoning={}, citations=[],
            action_kind="create_index", recipe_version="v1",
            parameters=state2["proposal"]["parameters"], rendered_sql="irrelevant for this check",
            idempotency_key=key2,
        )
        record("second call with the SAME key returns decision_id=None (no new decision)",
               decision_id is None)
        record("second call returns the SAME action_id",
               action_id_again == str(action_row["action_id"]),
               f"{action_id_again} vs {action_row['action_id']}")

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        try:
            async with db._pool.connection() as conn:
                async with conn.cursor() as cur:
                    for tid in task_ids:
                        await cur.execute("DELETE FROM tasks WHERE task_id = %s", (tid,))
                    await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
            print(f"  cleaned up scope_id={scope_id}, tasks={task_ids}")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  CLEANUP FAILED: {exc}")
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
    ap.add_argument("--sslrootcert", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.sslrootcert)))
