#!/usr/bin/env python3
"""Engram · smoke test for agent/memory/leases.py against a live cluster.  [PLUMBER]

Exercises the retry/backoff policy layer on top of db.py's single-attempt
lease primitives: a second holder correctly times out while a live lease is
held, a simulated `aws ecs stop-task` (forced expiry) lets a new holder
`takeover()`, and the original holder's background renew loop detects the
loss and signals it via `wait_until_lost()` -- the actual mechanism the
kill-and-resume demo beat depends on.

Uses short jitter/renew intervals (not the LLD's real 1-3s / 15s) so the
whole test runs in a few seconds; a task_id (not a real incident) is created
and deleted via ON DELETE CASCADE, taking its agent_leases row with it.

    python scripts/smoke_test_leases.py --sslrootcert cluster-ca.crt
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

from agent.errors import LeaseAcquireTimeoutError
from agent.memory import leases
from agent.memory.db import Database

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def main(sslrootcert: str | None) -> int:
    scope_id = f"smoke-leases-{uuid.uuid4().hex[:8]}"
    db = await Database.connect(sslrootcert=sslrootcert)
    print(f"connected, scope_id={scope_id}")
    all_ok = True
    task_id: str | None = None
    handle_a = handle_b = None

    try:
        print(f"\n{RULE}\nSETUP\n{RULE}")
        task_id = await db.insert_task(scope_id, "incident", "manual")
        record("insert_task", True, task_id)

        print(f"\n{RULE}\nACQUIRE (holder_a, fresh)\n{RULE}")
        handle_a = await leases.acquire(db, task_id, "holder-a",
                                         renew_interval_s=1, jitter_s=(0.1, 0.2))
        record("acquire (holder_a)", handle_a.fence_token == 1 and handle_a.is_valid,
               f"fence={handle_a.fence_token}")

        print(f"\n{RULE}\nSECOND HOLDER BLOCKED — must time out, not hang forever\n{RULE}")
        timed_out = False
        try:
            await leases.acquire(db, task_id, "holder-b", max_attempts=2, jitter_s=(0.1, 0.2))
        except LeaseAcquireTimeoutError:
            timed_out = True
        record("acquire (holder_b, live lease held) raises LeaseAcquireTimeoutError", timed_out)

        print(f"\n{RULE}\nSIMULATE `aws ecs stop-task` — force-expire, then takeover\n{RULE}")
        async with db._pool.connection() as conn:  # test-only: skip the real 60s TTL wait
            async with conn.cursor() as cur:
                await cur.execute("UPDATE agent_leases SET expires_at = now() WHERE task_id = %s",
                                   (task_id,))
        handle_b = await leases.takeover(db, task_id, "holder-b",
                                          renew_interval_s=1, jitter_s=(0.1, 0.2))
        record("takeover (holder_b, after expiry)", handle_b.fence_token == 2,
               f"fence={handle_b.fence_token}")

        print(f"\n{RULE}\nHOLDER_A DETECTS THE LOSS — via its own background renew loop\n{RULE}")
        try:
            await asyncio.wait_for(handle_a.wait_until_lost(), timeout=5)
            detected = True
        except asyncio.TimeoutError:
            detected = False
        record("holder_a's renew loop detects staleness within 5s", detected)
        record("holder_a.is_valid is now False", handle_a.is_valid is False)

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        for name, handle in (("holder_a", handle_a), ("holder_b", handle_b)):
            if handle is not None:
                try:
                    await handle.release()
                except Exception as exc:  # noqa: BLE001
                    print(f"  release({name}) raised (likely already lost, harmless): {exc}")
        if task_id is not None:
            try:
                async with db._pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
                print(f"  cleaned up task_id={task_id}")
            except Exception as exc:  # noqa: BLE001
                all_ok = False
                print(f"  CLEANUP FAILED: {exc} -- manual cleanup needed for task_id={task_id}")
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
