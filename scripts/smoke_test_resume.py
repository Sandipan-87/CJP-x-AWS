#!/usr/bin/env python3
"""Engram · smoke test for the checkpoint-resume fix in agent/main.py
(`_should_resume`) — CLAUDE.md's Next-action item #1, closed this session.

Session 41 proved kill-and-resume was CORRECT (exactly-once held) but not
EFFICIENT: a redelivered incident always replayed the full graph from
`observe`, paying for a second real Ollama call even though `reason` had
already completed and checkpointed before the kill. This script proves the
fix without touching AWS/ECS at all — no real `aws ecs stop-task` is needed
to exercise `_should_resume`, only a real cancelled `graph.ainvoke()` call
against the real memory cluster's checkpointer, which is the same signal a
real container kill leaves behind (a task row stuck at `status='running'`
with real checkpoint progress, and a lease that's no longer being renewed).

WHAT THIS SCRIPT DELIBERATELY SIMPLIFIES, stated not hidden: a real
`aws ecs stop-task` leaves the lease to expire via TTL (proven separately in
`scripts/smoke_test_leases.py` and Session 41's live AWS test) rather than
releasing it cleanly. This script calls `lease.release()` directly after
cancelling the in-flight `ainvoke()` — a faithful stand-in for "the lease
row is free again," without waiting out a real TTL, since `leases.py`'s own
release/expiry mechanics are already proven elsewhere and are not what this
script is testing. What IS real: the cancelled task's LangGraph checkpoint
progress, the DB task row left at `status='running'`, and the second
`process_message()` call going through `_should_resume`'s real logic
against the real checkpointer.

    python scripts/smoke_test_resume.py --sslrootcert workers/common/certs/memory-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import pathlib
import sys
import uuid
from contextlib import AsyncExitStack, suppress

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import psycopg

from agent.main import (
    Runtime,
    _initial_state,
    _thread_id_for_fingerprint,
    build_runtime,
    process_message,
)
from agent.memory import leases
from agent.nodes.observe import fingerprint, normalize_query_text, probe_result_from_explain
from agent.tools.sql_probe import SqlProbe

RULE = "-" * 72
results: list[tuple[str, str]] = []


class _ForcedLatencyProbe:
    """Same technique/rationale as `scripts/smoke_test_main.py`'s own class of
    this name — this target sandbox is fast enough that the real scan
    finishes under `DEFAULT_LATENCY_THRESHOLD_MS`, so only `latency_ms` is
    overridden; every other field stays genuinely measured.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    async def explain_analyze(self, query_text: str):
        result = await self._inner.explain_analyze(query_text)
        return result._replace(latency_ms=5000.0)


def _patch_counting_complete(llm) -> dict:
    """Monkeypatches the `complete` method directly ON the real llm INSTANCE
    `build_runtime()` already constructed, rather than swapping which object
    `runtime.llm` points to. `agent/graph.py`'s own module docstring says why
    the latter wouldn't work: "every dependency is bound via closures over
    its node" — `build_graph()` closes over the `llm` object passed in at
    COMPILE time (inside `build_runtime()`, before this function ever runs),
    so re-pointing `runtime.llm` at a wrapper afterward is invisible to the
    already-compiled graph; `reason(node)` would keep calling the original,
    unwrapped instance. Patching `llm.complete` in place — the same object,
    a new bound method — is visible everywhere, since every caller (this
    script AND the graph's closure) resolves `.complete` on that one object
    at call time, not at closure-creation time. Returns a mutable counter
    dict so the caller can watch it live without needing a class at all.
    """
    original_complete = llm.complete
    counter = {"n": 0}

    async def _counting_complete(*args, **kwargs):
        # Incremented AFTER the real await returns -- the caller waits on this
        # count specifically to know when `reason(node)`'s Ollama round-trip has
        # finished (not merely started), so it can cancel shortly after that.
        result = await original_complete(*args, **kwargs)
        counter["n"] += 1
        return result

    llm.complete = _counting_complete
    return counter


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def _approve_when_ready(runtime: Runtime, task_id: str, deadline_s: float) -> None:
    """Same idea as every other smoke test in this repo (`smoke_test_main.py`,
    `smoke_test_graph.py`) — a real concurrent approval while `gate(node)` is
    genuinely polling in real wall-clock time. Deliberately scoped by
    `task_id`, NOT `target_cluster_id` like those other scripts: this
    sandbox target cluster is shared and reused across every prior live
    smoke test in this project's history, and `remediation_actions` rows
    from those earlier runs are intentionally left in place (this project's
    own stated convention — "this is the real system doing its real job,
    not test debris"). An unscoped query here would risk polling and
    deciding a stale HISTORICAL action instead of this run's real pending
    one — confirmed live, not theoretical: an earlier draft of this exact
    script did exactly that and its own gate(node) call genuinely expired
    waiting for an approval that never came.
    """
    deadline = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < deadline:
        rows = await runtime.db._read(  # noqa: SLF001
            "SELECT action_id FROM remediation_actions WHERE task_id = %s", (task_id,)
        )
        if rows:
            approvals = await runtime.db._read(  # noqa: SLF001
                "SELECT approval_id FROM approvals WHERE action_id = %s", (rows[0]["action_id"],)
            )
            if approvals:
                await runtime.db.decide_approval(approvals[0]["approval_id"], "smoke-test-human", "approved")
                return
        await asyncio.sleep(0.3)


async def main(sslrootcert: str | None) -> int:
    marker = uuid.uuid4().hex[:8]
    table = f"smoke_resume_{marker}"
    scope_id = f"smoke-resume-{marker}"
    target_cluster_id = os.environ["ENGRAM_TARGET_CLUSTER_ID"]
    target_dsn = os.environ["ENGRAM_TARGET_DSN"]
    if sslrootcert and "sslrootcert=" not in target_dsn:
        sep = "&" if "?" in target_dsn else "?"
        target_dsn = f"{target_dsn}{sep}sslrootcert={sslrootcert}"

    if sslrootcert:
        os.environ.setdefault("ENGRAM_MEMORY_SSLROOTCERT", sslrootcert)
        os.environ.setdefault("ENGRAM_TARGET_SSLROOTCERT", sslrootcert)
    os.environ["ENGRAM_APPROVAL_TIMEOUT_S"] = "60"

    all_ok = True
    task_id: str | None = None
    expected_thread_id: str | None = None
    runtime: Runtime | None = None

    print(f"\n{RULE}\nSETUP — real scenario on the TARGET cluster\n{RULE}")
    admin_conn = await psycopg.AsyncConnection.connect(target_dsn, autocommit=True)

    async with AsyncExitStack() as stack:
        try:
            async with admin_conn.cursor() as cur:
                await cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, customer_id INT)")
                await cur.execute(f"INSERT INTO {table} SELECT g, g % 500 FROM generate_series(1, 40000) g")
            record("scenario table created + seeded (40k rows)", True, table)

            slow_sql = f"SELECT * FROM {table} WHERE customer_id = 42"
            async with SqlProbe(dsn=target_dsn) as probe:
                pre_check = await probe.explain_analyze(slow_sql)
            record(
                "real EXPLAIN confirms an anomaly is detectable (full scan + index candidate)",
                pre_check.has_full_scan and pre_check.index_candidate == "customer_id",
            )

            print(f"\n{RULE}\nbuild_runtime() — real deps, including the real checkpointer\n{RULE}")
            runtime = await build_runtime(stack)
            record("build_runtime() constructed a Runtime with a checkpointer", runtime.checkpointer is not None)

            real_probe = runtime.sql_probe
            runtime.sql_probe = _ForcedLatencyProbe(real_probe)
            llm_call_counter = _patch_counting_complete(runtime.llm)

            normalized = normalize_query_text(slow_sql)
            fp = fingerprint(normalized)
            expected_thread_id = _thread_id_for_fingerprint(fp)
            config = {"configurable": {"thread_id": expected_thread_id}}

            print(f"\n{RULE}\nRUN 1 — start the incident, kill it right after `reason` checkpoints\n{RULE}")
            task_id = await runtime.db.insert_task(
                scope_id, "incident", "manual", target_cluster_id=target_cluster_id, incident_fingerprint=fp,
            )
            await runtime.db.set_checkpoint_thread_id(task_id, expected_thread_id)
            await runtime.db.update_task_status(task_id, "running")
            lease = await leases.acquire(runtime.db, task_id, runtime.holder_id, renew_interval_s=runtime.lease_renew_s)

            explain_result = await runtime.sql_probe.explain_analyze(slow_sql)
            probe_payload = probe_result_from_explain(
                explain_result, query_text=slow_sql, table_name=table, target_cluster_id=target_cluster_id,
            )
            state = _initial_state(scope_id, target_cluster_id, "manual", probe_payload)

            run1 = asyncio.ensure_future(runtime.graph.ainvoke(state, config=config))
            deadline = asyncio.get_event_loop().time() + 90.0
            while llm_call_counter["n"] < 1 and asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.2)
            record("run 1 made a real Ollama call (reason node ran)", llm_call_counter["n"] == 1, f"calls={llm_call_counter['n']}")

            # A completed `llm.complete()` call is NOT the same as a completed `reason(node)` --
            # falsification/pydantic-validation/`insert_decision` all still run afterward, and
            # LangGraph only checkpoints a node once its function fully RETURNS. Poll for the real
            # decision row (the actual signal that the node -- not just the LLM call inside it --
            # finished) instead of guessing a fixed sleep duration; measured live that a fixed 1.0s
            # buffer was NOT always enough and cancelled the node mid-return, which is a real,
            # checkpoint-consistent case (a genuinely incomplete node correctly gets re-run on
            # resume) but not the case this script means to exercise.
            decision_deadline = asyncio.get_event_loop().time() + 20.0
            reason_committed = False
            while asyncio.get_event_loop().time() < decision_deadline:
                rows = await runtime.db._read(  # noqa: SLF001
                    "SELECT count(*) AS n FROM decisions WHERE task_id = %s AND node = 'reason'", (task_id,)
                )
                if rows[0]["n"] >= 1:
                    reason_committed = True
                    break
                await asyncio.sleep(0.2)
            record("reason(node)'s decision row committed before the kill", reason_committed)
            await asyncio.sleep(0.5)  # small buffer for LangGraph's own post-return checkpoint commit

            # Simulate a kill: cancel the in-flight run, then free the lease directly (see module
            # docstring) WITHOUT ever calling update_task_status -- the row is left at 'running',
            # exactly what a real SIGKILL leaves behind.
            run1.cancel()
            with suppress(asyncio.CancelledError):
                await run1
            await lease.release()
            record("run 1 cancelled mid-flight; task row left at status='running'", True)

            reason_rows_after_kill = await runtime.db._read(  # noqa: SLF001
                "SELECT count(*) AS n FROM decisions WHERE task_id = %s AND node = 'reason'", (task_id,)
            )
            record(
                "exactly one 'reason' decision exists after the kill (it completed before dying)",
                reason_rows_after_kill[0]["n"] == 1, f"n={reason_rows_after_kill[0]['n']}",
            )

            ckpt_rows_after_kill = await runtime.db._read(  # noqa: SLF001
                "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", (expected_thread_id,)
            )
            record(
                "real checkpoint progress exists for this thread_id after the kill",
                ckpt_rows_after_kill[0]["n"] > 0, f"n={ckpt_rows_after_kill[0]['n']}",
            )

            status_row = await runtime.db._read("SELECT status FROM tasks WHERE task_id = %s", (task_id,))  # noqa: SLF001
            record("task status is still 'running' after the kill", status_row[0]["status"] == "running", status_row[0]["status"])

            print(f"\n{RULE}\nRUN 2 — redeliver the SAME message through the REAL process_message()\n{RULE}")
            incident_message = {
                "scope_id": scope_id, "target_cluster_id": target_cluster_id,
                "table_name": table, "query_text": slow_sql, "trigger": "manual",
            }
            approver = asyncio.create_task(_approve_when_ready(runtime, task_id, deadline_s=50.0))
            outcome = await process_message(runtime, incident_message)
            await approver
            record("run 2 (redelivery) returned 'completed'", outcome == "completed", f"outcome={outcome!r}")

            record(
                "run 2 made ZERO additional real Ollama calls -- reason(node) was skipped, not replayed",
                llm_call_counter["n"] == 1, f"total calls={llm_call_counter['n']}",
            )

            decision_counts = await runtime.db._read(  # noqa: SLF001
                "SELECT node, count(*) AS n FROM decisions WHERE task_id = %s GROUP BY node", (task_id,)
            )
            counts = {row["node"]: row["n"] for row in decision_counts}
            record(
                "each of recall/reason/gate/act ran exactly ONCE total across both runs combined",
                counts.get("recall") == 1 and counts.get("reason") == 1 and counts.get("gate") == 1 and counts.get("act") == 1,
                f"counts={counts}",
            )

            obs_rows = await runtime.db._read(  # noqa: SLF001
                "SELECT count(*) AS n FROM observations WHERE task_id = %s", (task_id,)
            )
            record(
                "exactly one observation row -- observe(node) also did not re-run on resume",
                obs_rows[0]["n"] == 1, f"n={obs_rows[0]['n']}",
            )

            final_status = await runtime.db._read("SELECT status FROM tasks WHERE task_id = %s", (task_id,))  # noqa: SLF001
            record("final task status is 'completed'", final_status[0]["status"] == "completed", final_status[0]["status"])

            lease_rows = await runtime.db._read(  # noqa: SLF001
                "SELECT count(*) AS n FROM agent_leases WHERE task_id = %s", (task_id,)
            )
            record("lease was released after run 2 (no agent_leases row remains)", lease_rows[0]["n"] == 0, f"n={lease_rows[0]['n']}")

            action_rows = await runtime.db._read(  # noqa: SLF001
                "SELECT outcome FROM remediation_actions WHERE task_id = %s", (task_id,)
            )
            record(
                "exactly one remediation_actions row, outcome='success'",
                len(action_rows) == 1 and action_rows[0]["outcome"] == "success", f"{action_rows}",
            )

            async with admin_conn.cursor() as cur:
                await cur.execute(f"SHOW INDEXES FROM {table}")
                index_names = {row[1] for row in await cur.fetchall()}
            record("a real secondary index exists on the target cluster", any(n != f"{table}_pkey" for n in index_names), f"{index_names}")

        except Exception as exc:  # noqa: BLE001
            all_ok = False
            record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        if runtime is not None:
            try:
                async with runtime.db._pool.connection() as conn:  # noqa: SLF001
                    async with conn.cursor() as cur:
                        if expected_thread_id:
                            await cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (expected_thread_id,))
                            await cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (expected_thread_id,))
                            await cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (expected_thread_id,))
                        if task_id:
                            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
                        await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
                        await cur.execute(
                            "DELETE FROM embedding_cache WHERE content_sha256 IN (%s, %s)",
                            (
                                hashlib.sha256(f"search_document:{normalized}".encode()).hexdigest(),
                                hashlib.sha256(f"search_query:{normalized}".encode()).hexdigest(),
                            ),
                        )
                print(f"  cleaned up memory cluster + checkpoint rows: scope_id={scope_id}")
            except Exception as exc:  # noqa: BLE001
                all_ok = False
                print(f"  MEMORY CLEANUP FAILED: {exc}")
        else:
            print("  runtime was never built -- nothing on the memory cluster to clean up")

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
    ap.add_argument("--sslrootcert", default=None, help="CA cert path, same file for both clusters")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.sslrootcert)))
