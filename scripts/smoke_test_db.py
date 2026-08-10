#!/usr/bin/env python3
"""Engram · smoke test for agent/memory/db.py against a live cluster.  [PLUMBER]

Exercises the DAO end-to-end: task lifecycle, observation, memory item (no
embedding — the seed-then-backfill path), decision + tool_call audit rows,
lease acquire/renew/takeover/release, remediation ledger + idempotent
re-insert, approval CAS, procedure stats, dashboard views, audit_replay.

Every row this script creates is deleted at the end via ON DELETE CASCADE
from its own test task_id — nothing is left behind on success. On failure,
the printed task_id lets you find and clean up manually.

    pip install -r requirements.txt -r scripts/requirements-verify.txt
    python scripts/smoke_test_db.py
    python scripts/smoke_test_db.py --sslrootcert cluster-ca.crt   # fresh runner

Exit 0 only if every step (and cleanup) succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from agent.errors import StaleLeaseError
from agent.memory.db import Database

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def main(sslrootcert: str | None) -> int:
    scope_id = f"smoke-test-{uuid.uuid4().hex[:8]}"
    holder_a = "smoke-test-holder-a"
    holder_b = "smoke-test-holder-b"  # simulates a second ECS task reclaiming after stop-task

    db = await Database.connect(sslrootcert=sslrootcert)
    print(f"connected, scope_id={scope_id}")
    task_id: str | None = None
    all_ok = True

    try:
        print(f"\n{RULE}\nTASK LIFECYCLE\n{RULE}")
        task_id = await db.insert_task(scope_id, "incident", "manual",
                                        target_cluster_id="smoke-cluster",
                                        incident_fingerprint="smoke-fp-001")
        record("insert_task", True, task_id)
        dup_task_id = await db.insert_task(scope_id, "incident", "manual",
                                            target_cluster_id="smoke-cluster",
                                            incident_fingerprint="smoke-fp-001")
        record("insert_task dedupe (UniqueViolation path)", dup_task_id == task_id,
               f"got {dup_task_id}, expected {task_id}")
        await db.update_task_status(task_id, "running")
        record("update_task_status", True)

        print(f"\n{RULE}\nOBSERVATIONS + ENTITIES\n{RULE}")
        obs_id = await db.insert_observation(scope_id, "sql_probe", "metric",
                                              {"latency_ms": 4300}, task_id=task_id)
        record("insert_observation", True, obs_id)
        entity_id = await db.upsert_entity(scope_id, "table", "orders", {"rows": 2_000_000})
        entity_id_2 = await db.upsert_entity(scope_id, "table", "orders", {"rows": 2_000_050})
        record("upsert_entity (insert + conflict-update same row)", entity_id == entity_id_2)

        print(f"\n{RULE}\nMEMORY ITEM (no embedding — seed-then-backfill path)\n{RULE}")
        item_id = await db.insert_memory_item(scope_id, "episode", "smoke test episode",
                                               provenance={"task_id": task_id})
        record("insert_memory_item (embedding=None)", True, item_id)
        details = await db.get_candidate_details([item_id])
        record("get_candidate_details", len(details) == 1, f"{len(details)} row(s)")

        print(f"\n{RULE}\nDECISION + TOOL CALL AUDIT\n{RULE}")
        decision_id = await db.insert_decision(task_id, scope_id, "reason", "smoke-test-model",
                                                {"reasoning": "smoke test"})
        record("insert_decision", True, decision_id)
        tool_call_id = await db.insert_tool_call(task_id, "sql_probe", "explain_analyze",
                                                  {"sql": "SELECT 1"}, "ok",
                                                  decision_id=decision_id, latency_ms=12)
        record("insert_tool_call", True, tool_call_id)

        print(f"\n{RULE}\nLEASE ACQUIRE / RENEW / TAKEOVER / RELEASE (invariant #5)\n{RULE}")
        won_a, fence_a = await db.acquire_lease(task_id, holder_a)
        record("acquire_lease (holder_a, first claim)", won_a and fence_a == 1,
               f"won={won_a} fence={fence_a}")
        won_a2, _ = await db.acquire_lease(task_id, holder_b)
        record("acquire_lease (holder_b, live lease held by a -> must lose)", won_a2 is False,
               f"won={won_a2}")
        await db.renew_lease(task_id, holder_a, fence_a)
        record("renew_lease (holder_a, correct fence)", True)
        stale_ok = False
        try:
            await db.renew_lease(task_id, holder_a, fence_a + 99)
        except StaleLeaseError:
            stale_ok = True
        record("renew_lease (wrong fence -> StaleLeaseError)", stale_ok)
        # Simulate `aws ecs stop-task`: holder_a never releases; expire the lease early
        # for the test instead of sleeping 60s, then holder_b reclaims it.
        async with db._pool.connection() as conn:  # test-only: force expiry to avoid a real 60s sleep
            async with conn.cursor() as cur:
                await cur.execute("UPDATE agent_leases SET expires_at = now() WHERE task_id = %s", (task_id,))
        won_b, fence_b = await db.takeover_lease(task_id, holder_b)
        record("takeover_lease (holder_b, after expiry)", won_b and fence_b == fence_a + 1,
               f"won={won_b} fence={fence_b} (was {fence_a})")
        await db.release_lease(task_id, holder_b)
        record("release_lease", True)

        print(f"\n{RULE}\nREMEDIATION LEDGER (invariant #4 exactly-once)\n{RULE}")
        idem_key = f"smoke-test-{uuid.uuid4().hex}"
        action_id = await db.insert_remediation_action(
            task_id, scope_id, "smoke-cluster", "create_index", "v1",
            {"table": "orders", "columns": ["customer_id"]},
            "CREATE INDEX IF NOT EXISTS ON orders (customer_id)", idem_key, "proposed",
        )
        record("insert_remediation_action", True, action_id)
        dup_action_id = await db.insert_remediation_action(
            task_id, scope_id, "smoke-cluster", "create_index", "v1",
            {"table": "orders", "columns": ["customer_id"]},
            "CREATE INDEX IF NOT EXISTS ON orders (customer_id)", idem_key, "proposed",
        )
        record("insert_remediation_action idempotent re-insert", dup_action_id == action_id,
               f"got {dup_action_id}, expected {action_id}")
        await db.update_remediation_status(action_id, "applied", outcome="success",
                                            measured_before={"ms": 4300}, measured_after={"ms": 12})
        record("update_remediation_status", True)
        fetched = await db.get_by_idempotency_key(idem_key)
        record("get_by_idempotency_key", fetched is not None and fetched["status"] == "applied")

        print(f"\n{RULE}\nAPPROVAL CAS\n{RULE}")
        approval_id = await db.insert_approval(task_id, action_id, channel="cli")
        record("insert_approval", True, approval_id)
        won_decision = await db.decide_approval(approval_id, "smoke-test-operator", "approved")
        record("decide_approval (first CAS, must win)", won_decision is True)
        lost_decision = await db.decide_approval(approval_id, "smoke-test-operator", "rejected")
        record("decide_approval (second CAS on same row, must lose)", lost_decision is False)
        polled = await db.poll_approval(approval_id)
        record("poll_approval", polled is not None and polled["status"] == "approved")

        print(f"\n{RULE}\nPROCEDURE STATS (used by scoring.wilson_lb, not computed here)\n{RULE}")
        procedure_id = None
        async with db._pool.connection() as conn:  # test-only direct insert; no DAO method creates procedures yet
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO procedures (scope_id, name, description, steps) "
                    "VALUES (%s, 'smoke test procedure', 'smoke test', '[]'::JSONB) "
                    "RETURNING procedure_id",
                    (scope_id,),
                )
                procedure_id = str((await cur.fetchone())[0])
        await db.update_procedure_stats(procedure_id, success=True)
        await db.recompute_confidence(procedure_id, 0.42)
        record("update_procedure_stats + recompute_confidence", True, procedure_id)

        print(f"\n{RULE}\nDASHBOARD VIEWS\n{RULE}")
        recent = await db.dashboard_recent_tasks()
        feed = await db.dashboard_action_feed()
        inspector = await db.dashboard_memory_inspector()
        record("dashboard_recent_tasks", any(str(r["task_id"]) == task_id for r in recent),
               f"{len(recent)} row(s), smoke task present")
        record("dashboard_action_feed", True, f"{len(feed)} row(s)")
        record("dashboard_memory_inspector", True, f"{len(inspector)} row(s)")

        print(f"\n{RULE}\nAUDIT REPLAY (invariant #8)\n{RULE}")
        now_iso = datetime.now(timezone.utc).isoformat()
        replay = await db.audit_replay(task_id, now_iso)
        record("audit_replay", len(replay["decisions"]) >= 1 and len(replay["tool_calls"]) >= 1,
               f"decisions={len(replay['decisions'])} tool_calls={len(replay['tool_calls'])}")
        bad_ts_ok = False
        try:
            await db.audit_replay(task_id, "not-a-timestamp")
        except ValueError:
            bad_ts_ok = True
        record("audit_replay rejects invalid as_of_ts before touching SQL", bad_ts_ok)

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        if task_id is not None:
            try:
                async with db._pool.connection() as conn:
                    async with conn.cursor() as cur:
                        # ON DELETE CASCADE (migration 001) removes observations,
                        # decisions, tool_calls, remediation_actions, approvals,
                        # agent_leases, working_memory for this task in one statement.
                        await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
                        await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
                        await cur.execute("DELETE FROM entities WHERE scope_id = %s", (scope_id,))
                        await cur.execute("DELETE FROM procedures WHERE scope_id = %s", (scope_id,))
                print(f"  cleaned up scope_id={scope_id}, task_id={task_id}")
            except Exception as exc:  # noqa: BLE001
                all_ok = False
                print(f"  CLEANUP FAILED: {exc}\n  manual cleanup needed for scope_id={scope_id}")
        await db.close()

    print(f"\n{RULE}\nRESULT\n{RULE}")
    width = max((len(k) for k, _ in results), default=10)
    failures = [k for k, v in results if v.startswith("FAIL")]
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    print(f"\n  {len(results) - len(failures)}/{len(results)} checks passed")
    return 0 if all_ok and not failures else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sslrootcert", default=None,
                     help="path to the cluster CA cert (see scripts/run_sql.py --sslrootcert)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.sslrootcert)))
