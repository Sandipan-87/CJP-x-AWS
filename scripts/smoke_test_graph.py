#!/usr/bin/env python3
"""Engram · smoke test for agent/graph.py — the compiled StateGraph, end to end.  [BRAINS]

The full loop the user asked for: a real scenario on the TARGET cluster,
probed for real via SqlProbe, fed into `build_graph()`'s compiled app, and
invoked for real — `observe -> recall -> reason -> END` on live ammunition,
not a dry run with mocked payloads. Two invocations: one that fires an
incident (routes all the way through reason, real Ollama Cloud call
included) and one that doesn't (routes straight to END after observe),
proving the conditional edge actually branches both ways.

Latency is deliberately bumped in the "incident" case (see inline note) —
this test proves the GRAPH ROUTES correctly off whatever `is_anomaly()`
decides, which `smoke_test_sql_probe.py` already proved measures real
values; re-seeding a multi-million-row table just to naturally exceed the
1000ms production threshold would test the same thing slower and pricier.

    python scripts/smoke_test_graph.py \\
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

from agent.graph import build_graph
from agent.memory.db import Database
from agent.providers.cohere_embed import CohereEmbeddings
from agent.providers.ollama_cloud_llm import OllamaCloudLLM
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
            await cur.execute(f"INSERT INTO {table} SELECT g, g % 500 FROM generate_series(1, 20000) g")
        record("scenario table created + seeded", True, table)

        async with SqlProbe(dsn=target_dsn) as probe:
            slow_sql = f"SELECT * FROM {table} WHERE customer_id = 42"
            explain_result = await probe.explain_analyze(slow_sql)
            record("real EXPLAIN captured (full scan + index candidate)",
                   explain_result.has_full_scan and explain_result.index_candidate == "customer_id")

        async with CohereEmbeddings() as embed_provider, OllamaCloudLLM() as llm:
            graph = build_graph(db, embed_provider, llm)

            print(f"\n{RULE}\nINVOCATION 1 — anomalous probe, must route observe -> recall -> reason\n{RULE}")
            # Latency bumped to simulate a genuinely slow production query on top of the
            # REAL full-scan/index-candidate signal SqlProbe measured — see module docstring.
            # ExplainResult is a NamedTuple, hence ._replace (its own built-in), not dataclasses.replace.
            incident_probe = explain_result._replace(latency_ms=5000.0)
            from agent.nodes.observe import probe_result_from_explain
            probe_payload = probe_result_from_explain(
                incident_probe, query_text=slow_sql, table_name=table,
                target_cluster_id=f"smoke-target-{marker}",
            )
            state1 = _initial_state(scope_id, probe_payload)
            final1 = await graph.ainvoke(state1)
            task_ids.append(final1["task_id"])
            normalized_texts.append(final1["observations"][0]["payload"]["text"])
            record("incident_fingerprint is set (anomaly fired)", final1["incident_fingerprint"] is not None)
            record("phase progressed to 'recall' then 'reason' (graph did not stop early)",
                   final1["phase"] == "reason", f"phase={final1['phase']!r}")
            record("recall_bundle was populated", final1["recall_bundle"] is not None)
            record("a real Proposal came back from Ollama Cloud", final1["proposal"] is not None)
            if final1["proposal"]:
                record("proposal has an allowlisted action_kind",
                       final1["proposal"]["action_kind"] in ("create_index", "analyze_table"),
                       final1["proposal"]["action_kind"])
                record("proposal carries non-empty audit-grade reasoning",
                       len(final1["proposal"]["reasoning"]) > 0)

            print(f"\n{RULE}\nINVOCATION 2 — non-anomalous probe, must route straight to END\n{RULE}")
            # Real, unmodified measurement (~tens of ms) -- genuinely below the production
            # 1000ms default threshold, so this exercises the OTHER branch for real.
            sweep_payload = probe_result_from_explain(
                explain_result, query_text=slow_sql, table_name=table,
                target_cluster_id=f"smoke-target-{marker}",
            )
            state2 = _initial_state(scope_id, sweep_payload)
            final2 = await graph.ainvoke(state2)
            task_ids.append(final2["task_id"])
            normalized_texts.append(final2["observations"][0]["payload"]["text"])
            record("incident_fingerprint stayed None (no anomaly)", final2["incident_fingerprint"] is None)
            record("phase stopped at 'observe' (recall never ran)",
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
                                __import__("hashlib").sha256(f"search_document:{text}".encode()).hexdigest(),
                                __import__("hashlib").sha256(f"search_query:{text}".encode()).hexdigest(),
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
    ap.add_argument("--target-sslrootcert", default=None)
    ap.add_argument("--memory-sslrootcert", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.target_sslrootcert, args.memory_sslrootcert)))
