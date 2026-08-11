#!/usr/bin/env python3
"""Engram · smoke test for the checkpointer wired into agent/graph.py.  [BRAINS]

Proves `build_graph(..., checkpointer=saver)` actually persists — not just
compiles — by invoking the compiled graph with a real `AsyncCockroachDBSaver`
and a chosen `thread_id`, then reading the `checkpoints` table (and
`saver.aget_tuple()`) back directly to confirm a real row landed there.

Deliberately uses the CHEAP path only (a fast probe, no anomaly, routes
straight to `observe -> END` per `_route_after_observe`) — this test is
about checkpoint persistence, not re-proving the full five-node loop
already covered by `scripts/smoke_test_graph.py`. No Ollama call, no
`CREATE INDEX` on the target cluster.

Requires `scripts/bootstrap_checkpointer.py` to have run once already
(creates + TTLs the three checkpoint tables) — this script does not bootstrap
them itself, matching the "setup() once, never again on a hot cluster"
invariant #7 discipline.

    python scripts/smoke_test_checkpointer.py \\
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
from langchain_cockroachdb import AsyncCockroachDBSaver

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


async def main(target_sslrootcert: str | None, memory_sslrootcert: str | None) -> int:
    marker = uuid.uuid4().hex[:8]
    table = f"smoke_ckpt_{marker}"
    target_dsn = os.environ["ENGRAM_TARGET_DSN"]
    if target_sslrootcert and "sslrootcert=" not in target_dsn:
        sep = "&" if "?" in target_dsn else "?"
        target_dsn = f"{target_dsn}{sep}sslrootcert={target_sslrootcert}"

    memory_dsn = os.environ["ENGRAM_MEMORY_DSN"]
    if memory_sslrootcert and "sslrootcert=" not in memory_dsn:
        sep = "&" if "?" in memory_dsn else "?"
        memory_dsn = f"{memory_dsn}{sep}sslrootcert={memory_sslrootcert}"

    thread_id = f"smoke-thread-{marker}"
    scope_id = f"smoke-ckpt-{marker}"
    all_ok = True
    task_id: str | None = None
    normalized_text: str | None = None

    print(f"\n{RULE}\nSETUP — real (fast, non-anomalous) probe on the TARGET cluster\n{RULE}")
    admin_conn = await psycopg.AsyncConnection.connect(target_dsn, autocommit=True)
    db = await Database.connect(sslrootcert=memory_sslrootcert)
    verify_conn = await psycopg.AsyncConnection.connect(memory_dsn, autocommit=True)

    try:
        async with admin_conn.cursor() as cur:
            await cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, customer_id INT)")
            await cur.execute(f"INSERT INTO {table} SELECT g, g % 500 FROM generate_series(1, 500) g")
        record("scenario table created + seeded (500 rows, small on purpose)", True, table)

        fast_sql = f"SELECT * FROM {table} WHERE id = 1"
        async with SqlProbe(dsn=target_dsn) as probe:
            explain_result = await probe.explain_analyze(fast_sql)
        record("real EXPLAIN captured", True, f"latency={explain_result.latency_ms:.1f}ms")

        async with (
            CohereEmbeddings() as embed_provider,
            OllamaCloudLLM() as llm,
            SqlProbe(dsn=target_dsn) as probe,
            SqlOperator(dsn=target_dsn) as operator,
            AsyncCockroachDBSaver.from_conn_string(memory_dsn) as saver,
        ):
            graph = build_graph(
                db, embed_provider, llm, probe, operator,
                checkpointer=saver, override_backup_gate=True,
            )

            print(f"\n{RULE}\nINVOCATION — observe -> END (no anomaly), WITH a real checkpointer\n{RULE}")
            target_cluster_id = f"smoke-ckpt-target-{marker}"
            probe_payload = probe_result_from_explain(
                explain_result, query_text=fast_sql, table_name=table, target_cluster_id=target_cluster_id,
            )
            state = _initial_state(scope_id, probe_payload)
            config = {"configurable": {"thread_id": thread_id}}

            final = await graph.ainvoke(state, config=config)
            task_id = final["task_id"]
            normalized_text = final["observations"][0]["payload"]["text"]
            record("graph ran to completion (phase='observe', no anomaly)",
                   final["phase"] == "observe", f"phase={final['phase']!r}")

            print(f"\n{RULE}\nVERIFY — a REAL row landed in `checkpoints` for this thread_id\n{RULE}")
            async with verify_conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,)
                )
                n = (await cur.fetchone())[0]
            record("checkpoints table has >=1 row for our thread_id", n >= 1, f"n={n}")

            tuple_ = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
            record("saver.aget_tuple() retrieves it back", tuple_ is not None)
            if tuple_ is not None:
                restored_phase = tuple_.checkpoint["channel_values"].get("phase")
                record("restored checkpoint's phase channel matches the final state",
                       restored_phase == "observe", f"restored phase={restored_phase!r}")

            # A second graph, no checkpointer, must still work unaffected --
            # checkpointer is opt-in, not a hidden requirement.
            print(f"\n{RULE}\nREGRESSION — build_graph() with checkpointer=None (the default) still works\n{RULE}")
            graph_no_ckpt = build_graph(
                db, embed_provider, llm, probe, operator, override_backup_gate=True,
            )
            state2 = _initial_state(scope_id, probe_payload)
            final2 = await graph_no_ckpt.ainvoke(state2)
            record("uncheckpointed graph still runs to completion", final2["phase"] == "observe")
            if final2.get("task_id"):
                async with db._pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("DELETE FROM tasks WHERE task_id = %s", (final2["task_id"],))

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        try:
            async with verify_conn.cursor() as cur:
                await cur.execute("DELETE FROM checkpoints WHERE thread_id = %s", (thread_id,))
                await cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id = %s", (thread_id,))
                await cur.execute("DELETE FROM checkpoint_writes WHERE thread_id = %s", (thread_id,))
            print(f"  cleaned up checkpoint rows for thread_id={thread_id}")
            async with db._pool.connection() as conn:
                async with conn.cursor() as cur:
                    if task_id:
                        await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
                    await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
                    if normalized_text:
                        await cur.execute(
                            "DELETE FROM embedding_cache WHERE content_sha256 IN (%s, %s)",
                            (
                                hashlib.sha256(f"search_document:{normalized_text}".encode()).hexdigest(),
                                hashlib.sha256(f"search_query:{normalized_text}".encode()).hexdigest(),
                            ),
                        )
            print(f"  cleaned up memory cluster: scope_id={scope_id}, task_id={task_id}")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  MEMORY CLEANUP FAILED: {exc}")
        await db.close()
        await verify_conn.close()

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
