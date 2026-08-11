#!/usr/bin/env python3
"""Engram · smoke test for agent/graph.py — the FULL compiled StateGraph, end to end.  [BRAINS]

The complete loop, all five nodes, on live ammunition: a real scenario on
the TARGET cluster, probed for real via SqlProbe, fed into `build_graph()`'s
compiled app, and invoked for real —
`observe -> recall -> reason -> gate -> act_measure -> END`. A concurrent
task "approves" the gate mid-poll (same technique as
`smoke_test_gate_node.py`) while the graph is genuinely waiting in real
time — not pre-seeded before the call. `act_measure` then applies a REAL
index and measures a REAL latency improvement, same as
`smoke_test_act_measure.py`, but this time through the compiled graph
itself, not the node function called directly.

A second invocation with the real (fast) measurement proves the OTHER
branch: routes straight to END after `observe` alone, no anomaly.

`override_backup_gate=True` is used (no `CCLOUD_TOKEN` provisioned — see
`agent/tools/cloud_api.py`'s module docstring) — LLD's own named audited
escape hatch, not a workaround.

    python scripts/smoke_test_graph.py \\
        --target-sslrootcert target-ca.crt --memory-sslrootcert memory-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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

from agent.graph import build_graph
from agent.memory.db import Database
from agent.nodes.observe import probe_result_from_explain
from agent.providers.cohere_embed import CohereEmbeddings
from agent.providers.ollama_cloud_llm import OllamaCloudLLM
from agent.tools.sql_operator import SqlOperator
from agent.tools.sql_probe import SqlProbe

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


def _initial_state(scope_id: str, probe_payload: dict) -> dict:
    return {
        "task_id": "", "scope_id": scope_id, "target_cluster_id": probe_payload["target_cluster_id"],
        "trigger": "manual", "phase": "pending", "observations": [],
        "incident_fingerprint": None, "recall_bundle": None,
        "proposal": None, "approval": None, "action": None, "measurement": None,
        "error": None, "model_meta": {}, "initial_probe": dict(probe_payload),
    }


async def _approve_soon(db: Database, idempotency_key: str, delay_s: float) -> None:
    """Simulates a human approving mid-poll, concurrently with the graph's
    OWN real polling — not pre-seeded before `graph.ainvoke(...)` starts.
    """
    await asyncio.sleep(delay_s)
    row = await db.get_by_idempotency_key(idempotency_key)
    approvals = await db._read(
        "SELECT approval_id FROM approvals WHERE action_id = %s", (row["action_id"],)
    )
    await db.decide_approval(approvals[0]["approval_id"], "smoke-test-human", "approved")


async def main(target_sslrootcert: str | None, memory_sslrootcert: str | None) -> int:
    marker = uuid.uuid4().hex[:8]
    table = f"smoke_graph_{marker}"
    target_dsn = os.environ["ENGRAM_TARGET_DSN"]
    if target_sslrootcert and "sslrootcert=" not in target_dsn:
        sep = "&" if "?" in target_dsn else "?"
        target_dsn = f"{target_dsn}{sep}sslrootcert={target_sslrootcert}"

    scope_id = f"smoke-graph-{marker}"
    all_ok = True
    task_ids: list[str] = []
    normalized_texts: list[str] = []

    print(f"\n{RULE}\nSETUP — real scenario on the TARGET cluster\n{RULE}")
    admin_conn = await psycopg.AsyncConnection.connect(target_dsn, autocommit=True)
    db = await Database.connect(sslrootcert=memory_sslrootcert)

    try:
        async with admin_conn.cursor() as cur:
            await cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, customer_id INT)")
            await cur.execute(f"INSERT INTO {table} SELECT g, g % 500 FROM generate_series(1, 40000) g")
        record("scenario table created + seeded (40k rows)", True, table)

        slow_sql = f"SELECT * FROM {table} WHERE customer_id = 42"
        async with SqlProbe(dsn=target_dsn) as probe:
            explain_result = await probe.explain_analyze(slow_sql)
        record("real EXPLAIN captured (full scan + index candidate)",
               explain_result.has_full_scan and explain_result.index_candidate == "customer_id")

        async with (
            CohereEmbeddings() as embed_provider,
            OllamaCloudLLM() as llm,
            SqlProbe(dsn=target_dsn) as probe,
            SqlOperator(dsn=target_dsn) as operator,
        ):
            graph = build_graph(
                db, embed_provider, llm, probe, operator,
                gate_poll_interval_s=1.0, gate_timeout_s=60.0,
                override_backup_gate=True,
            )

            print(f"\n{RULE}\nINVOCATION 1 — full loop: observe -> recall -> reason -> gate -> act_measure\n{RULE}")
            target_cluster_id = f"smoke-target-{marker}"
            incident_probe = explain_result._replace(latency_ms=5000.0)
            probe_payload = probe_result_from_explain(
                incident_probe, query_text=slow_sql, table_name=table, target_cluster_id=target_cluster_id,
            )
            state1 = _initial_state(scope_id, probe_payload)

            # The idempotency key depends on the PROPOSAL's parameters, which reason(node)
            # decides -- not knowable ahead of time. So the approver below polls for the
            # remediation_actions row by target_cluster_id instead of a precomputed key.
            # Deadline is generous: observe+recall+reason (a REAL Ollama Cloud call, measured
            # ~9s elsewhere in this project) all run BEFORE gate() creates anything to approve,
            # so this clock is ticking through that time too, not just gate()'s own poll window.
            async def _approve_when_ready():
                deadline = asyncio.get_event_loop().time() + 50.0
                while asyncio.get_event_loop().time() < deadline:
                    rows = await db._read(
                        "SELECT action_id FROM remediation_actions WHERE target_cluster_id = %s",
                        (target_cluster_id,),
                    )
                    if rows:
                        approvals = await db._read(
                            "SELECT approval_id FROM approvals WHERE action_id = %s", (rows[0]["action_id"],)
                        )
                        if approvals:
                            await db.decide_approval(approvals[0]["approval_id"], "smoke-test-human", "approved")
                            return
                    await asyncio.sleep(0.3)

            approver = asyncio.create_task(_approve_when_ready())
            final1 = await graph.ainvoke(state1)
            await approver

            task_ids.append(final1["task_id"])
            normalized_texts.append(final1["observations"][0]["payload"]["text"])
            record("incident_fingerprint is set (anomaly fired)", final1["incident_fingerprint"] is not None)
            record("recall_bundle was populated", final1["recall_bundle"] is not None)
            record("a real Proposal came back from Ollama Cloud", final1["proposal"] is not None)
            record("gate picked up the REAL concurrent approval",
                   final1["approval"] is not None and final1["approval"]["status"] == "approved")
            record("phase reached 'done' via act_measure (full loop completed)",
                   final1["phase"] == "done", f"phase={final1['phase']!r}")
            record("action status is 'applied'",
                   final1["action"] is not None and final1["action"]["status"] == "applied",
                   f"action={final1['action']!r}")
            if final1.get("measurement"):
                record("outcome is 'success' (real measured latency improvement)",
                       final1["measurement"]["outcome"] == "success",
                       f"{final1['measurement']['measured_before']['latency_ms']:.1f}ms -> "
                       f"{final1['measurement']['measured_after']['latency_ms']:.1f}ms")
            else:
                record("outcome is 'success' (real measured latency improvement)", False,
                       "act_measure never ran -- gate must have expired/rejected instead of approving")

            async with admin_conn.cursor() as cur:
                await cur.execute(f"SHOW INDEXES FROM {table}")
                index_names = {row[1] for row in await cur.fetchall()}
            record("a real secondary index exists on the target cluster after the full loop",
                   any(name != f"{table}_pkey" for name in index_names), f"{index_names}")

            print(f"\n{RULE}\nINVOCATION 2 — non-anomalous probe, must route straight to END\n{RULE}")
            sweep_payload = probe_result_from_explain(
                explain_result, query_text=slow_sql, table_name=table, target_cluster_id=target_cluster_id,
            )
            state2 = _initial_state(scope_id, sweep_payload)
            final2 = await graph.ainvoke(state2)
            task_ids.append(final2["task_id"])
            normalized_texts.append(final2["observations"][0]["payload"]["text"])
            record("incident_fingerprint stayed None (no anomaly)", final2["incident_fingerprint"] is None)
            record("phase stopped at 'observe' (nothing past it ran)",
                   final2["phase"] == "observe", f"phase={final2['phase']!r}")
            record("recall_bundle stayed None (recall was skipped, not run-and-empty)",
                   final2["recall_bundle"] is None)
            record("the two invocations created DIFFERENT tasks (not falsely deduped)",
                   final1["task_id"] != final2["task_id"])

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        try:
            async with db._pool.connection() as conn:
                async with conn.cursor() as cur:
                    for tid in task_ids:
                        if tid:
                            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (tid,))
                    await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
                    for text in set(normalized_texts):
                        await cur.execute(
                            "DELETE FROM embedding_cache WHERE content_sha256 IN (%s, %s)",
                            (
                                hashlib.sha256(f"search_document:{text}".encode()).hexdigest(),
                                hashlib.sha256(f"search_query:{text}".encode()).hexdigest(),
                            ),
                        )
            print(f"  cleaned up memory cluster: scope_id={scope_id}, tasks={task_ids}")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  MEMORY CLEANUP FAILED: {exc}")
        await db.close()

        try:
            async with admin_conn.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS {table}")
            print(f"  dropped {table} (and its index, along with it)")
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
