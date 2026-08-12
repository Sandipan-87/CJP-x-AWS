#!/usr/bin/env python3
"""Engram · smoke test for agent/main.py — real end-to-end, through
`build_runtime()`/`run_startup_checks()`/`process_message()`, WITHOUT a real
SQS queue (none exists in AWS yet — see agent/main.py's own module
docstring). This is the actual "does the agent run end-to-end" proof: a real
scenario on the TARGET cluster, real Cohere, real Ollama Cloud, a real
(non-override) `CloudApiAdapter` backup-gate check, a real checkpointer, real
(best-effort) CloudWatch telemetry, and the real pre-insert/lease/thread_id
reconciliation `process_message()` does for an incident.

    python scripts/smoke_test_main.py --sslrootcert workers/common/certs/memory-ca.crt

(The same CA file works for both clusters — confirmed directly this
session: CockroachDB Cloud uses one shared root CA across an org's
clusters, at least for this account.)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import pathlib
import sys
import uuid
from contextlib import AsyncExitStack

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import psycopg

from agent.main import Runtime, _thread_id_for_fingerprint, build_runtime, process_message, run_startup_checks
from agent.nodes.observe import fingerprint, normalize_query_text
from agent.tools.sql_probe import SqlProbe

RULE = "-" * 72
results: list[tuple[str, str]] = []


class _ForcedLatencyProbe:
    """Wraps a real `SqlProbe`, replacing only `latency_ms` on the result --
    same technique `scripts/smoke_test_graph.py` already uses inline
    (`explain_result._replace(latency_ms=5000.0)`), just wrapped here since
    `process_message()` runs `explain_analyze()` internally and has no
    injection point for a pre-built `ExplainResult`. **Measured, not
    guessed, why this is needed**: this target sandbox cluster is fast
    enough that a 40k-row full scan naturally completes in well under
    `DEFAULT_LATENCY_THRESHOLD_MS` (1000ms) -- confirmed directly this
    session, and exactly why the proven `smoke_test_graph.py` already
    overrides it too, rather than a difference this file introduces. Every
    OTHER field (`has_full_scan`, `index_candidate`, the real plan text)
    stays genuinely measured.
    """

    def __init__(self, inner: SqlProbe) -> None:
        self._inner = inner

    async def explain_analyze(self, query_text: str):
        result = await self._inner.explain_analyze(query_text)
        return result._replace(latency_ms=5000.0)


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def _approve_when_ready(runtime: Runtime, target_cluster_id: str, deadline_s: float) -> None:
    """Same technique as `scripts/smoke_test_graph.py`'s `_approve_when_ready`
    — a real concurrent approval while `process_message()`'s own `gate(node)`
    is genuinely polling in real time, not pre-seeded before the call.
    """
    deadline = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < deadline:
        rows = await runtime.db._read(  # noqa: SLF001
            "SELECT action_id FROM remediation_actions WHERE target_cluster_id = %s", (target_cluster_id,)
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
    table = f"smoke_main_{marker}"
    scope_id = f"smoke-main-{marker}"
    # MUST be the real cluster UUID, not a made-up string: unlike every other smoke test in this
    # repo (which all use override_backup_gate=True and a fake id), this one exercises the REAL
    # CloudApiAdapter -- a fake id gets a real, correct HTTP 400 "invalid cluster id" from the
    # actual CockroachDB Cloud REST API, confirmed live this session.
    target_cluster_id = os.environ["ENGRAM_TARGET_CLUSTER_ID"]
    target_dsn = os.environ["ENGRAM_TARGET_DSN"]
    if sslrootcert and "sslrootcert=" not in target_dsn:
        sep = "&" if "?" in target_dsn else "?"
        target_dsn = f"{target_dsn}{sep}sslrootcert={sslrootcert}"

    if sslrootcert:
        os.environ.setdefault("ENGRAM_MEMORY_SSLROOTCERT", sslrootcert)
        os.environ.setdefault("ENGRAM_TARGET_SSLROOTCERT", sslrootcert)
    os.environ["ENGRAM_APPROVAL_TIMEOUT_S"] = "60"  # short, for a smoke test -- not LLD's 600 default

    all_ok = True
    incident_task_id: str | None = None
    sweep_task_id: str | None = None
    normalized_texts: list[str] = []

    print(f"\n{RULE}\nSETUP — real scenario on the TARGET cluster\n{RULE}")
    admin_conn = await psycopg.AsyncConnection.connect(target_dsn, autocommit=True)
    runtime: Runtime | None = None
    expected_thread_id: str | None = None

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

            print(f"\n{RULE}\nbuild_runtime() — REAL Cohere/Ollama/SqlProbe/SqlOperator/CloudApiAdapter/checkpointer/Telemetry\n{RULE}")
            runtime = await build_runtime(stack)
            record("build_runtime() constructed a Runtime", isinstance(runtime, Runtime))

            print(f"\n{RULE}\nrun_startup_checks() — DB ping, Cohere 1024-dim, Ollama reachable, lease round-trip\n{RULE}")
            await run_startup_checks(runtime)
            record("run_startup_checks() passed (see log lines above)", True)

            print(f"\n{RULE}\nprocess_message() — INCIDENT path, REAL (non-override) backup gate\n{RULE}")
            incident_message = {
                "scope_id": scope_id, "target_cluster_id": target_cluster_id,
                "table_name": table, "query_text": slow_sql, "trigger": "manual",
            }
            real_probe = runtime.sql_probe
            runtime.sql_probe = _ForcedLatencyProbe(real_probe)  # see class docstring
            approver = asyncio.create_task(_approve_when_ready(runtime, target_cluster_id, deadline_s=50.0))
            try:
                outcome = await process_message(runtime, incident_message)
            finally:
                runtime.sql_probe = real_probe  # sweep path below must see the REAL (fast) latency
            await approver
            record("process_message() returned 'completed'", outcome == "completed", f"outcome={outcome!r}")

            normalized = normalize_query_text(slow_sql)
            normalized_texts.append(normalized)
            fp = fingerprint(normalized)
            expected_thread_id = _thread_id_for_fingerprint(fp)

            rows = await runtime.db._read(  # noqa: SLF001
                "SELECT task_id, status, checkpoint_thread_id, task_type FROM tasks "
                "WHERE target_cluster_id = %s AND task_type = 'incident'",
                (target_cluster_id,),
            )
            record("exactly one incident task row exists", len(rows) == 1, f"rows={rows}")
            if rows:
                incident_task_id = str(rows[0]["task_id"])
                record("task status is 'completed'", rows[0]["status"] == "completed", rows[0]["status"])
                record(
                    "checkpoint_thread_id matches the deterministic thread_id computed from the fingerprint",
                    rows[0]["checkpoint_thread_id"] == expected_thread_id,
                    f"got={rows[0]['checkpoint_thread_id']!r} expected={expected_thread_id!r}",
                )

            ckpt_rows = await runtime.db._read(  # noqa: SLF001
                "SELECT count(*) AS n FROM checkpoints WHERE thread_id = %s", (expected_thread_id,)
            )
            record("real checkpointer wrote >=1 row for this thread_id", ckpt_rows[0]["n"] >= 1, f"n={ckpt_rows[0]['n']}")

            lease_rows = await runtime.db._read(  # noqa: SLF001
                "SELECT count(*) AS n FROM agent_leases WHERE task_id = %s", (incident_task_id,)
            )
            record("lease was released (no agent_leases row remains)", lease_rows[0]["n"] == 0, f"n={lease_rows[0]['n']}")

            action_rows = await runtime.db._read(  # noqa: SLF001
                "SELECT outcome, status FROM remediation_actions WHERE target_cluster_id = %s", (target_cluster_id,)
            )
            if action_rows:
                record(
                    "remediation_actions outcome is 'success' (real measured latency improvement)",
                    action_rows[0]["outcome"] == "success", f"{action_rows[0]}",
                )
            else:
                record("remediation_actions row exists", False, "none found")

            async with admin_conn.cursor() as cur:
                await cur.execute(f"SHOW INDEXES FROM {table}")
                index_names = {row[1] for row in await cur.fetchall()}
            record("a real secondary index exists on the target cluster", any(n != f"{table}_pkey" for n in index_names), f"{index_names}")

            print(f"\n{RULE}\nprocess_message() — SWEEP path (no anomaly), must skip pre-insert/lease entirely\n{RULE}")
            fast_sql = f"SELECT * FROM {table} WHERE id = 1"
            sweep_message = {
                "scope_id": scope_id, "target_cluster_id": target_cluster_id,
                "table_name": table, "query_text": fast_sql, "trigger": "manual",
            }
            sweep_outcome = await process_message(runtime, sweep_message)
            record("sweep process_message() returned 'completed'", sweep_outcome == "completed", sweep_outcome)

            sweep_rows = await runtime.db._read(  # noqa: SLF001
                "SELECT task_id, task_type, checkpoint_thread_id FROM tasks "
                "WHERE target_cluster_id = %s AND task_type = 'sweep'",
                (target_cluster_id,),
            )
            record("observe(node) itself created the sweep's task row", len(sweep_rows) == 1, f"rows={sweep_rows}")
            if sweep_rows:
                sweep_task_id = str(sweep_rows[0]["task_id"])
                record(
                    "sweep task's checkpoint_thread_id was NEVER written (main.py skips reconciliation for sweeps)",
                    sweep_rows[0]["checkpoint_thread_id"] is None,
                )
            normalized_texts.append(normalize_query_text(fast_sql))

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
                        if incident_task_id:
                            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (incident_task_id,))
                        if sweep_task_id:
                            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (sweep_task_id,))
                        await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
                        for text in set(normalized_texts):
                            await cur.execute(
                                "DELETE FROM embedding_cache WHERE content_sha256 IN (%s, %s)",
                                (
                                    hashlib.sha256(f"search_document:{text}".encode()).hexdigest(),
                                    hashlib.sha256(f"search_query:{text}".encode()).hexdigest(),
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
    ap.add_argument("--sslrootcert", default=None, help="CA cert path, same file for both clusters (see module docstring)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.sslrootcert)))
