#!/usr/bin/env python3
"""Engram · smoke test for agent/nodes/observe.py against real Cohere + CockroachDB.  [BRAINS]

Two calls with the SAME (normalized) query text, simulating two sweep
cycles hitting the same slow query: the second call must dedupe onto the
SAME incident task (via insert_incident_observation's inlined
UniqueViolation handling) rather than spawning a second agent, and the
embedding must come from cache the second time, not a fresh Cohere call.

    python scripts/smoke_test_observe_node.py --sslrootcert cluster-ca.crt
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
from agent.nodes.observe import ProbeResult, normalize_query_text, observe
from agent.providers.cohere_embed import CohereEmbeddings

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


class CountingProvider:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    async def embed(self, texts, input_type):
        self.calls += 1
        return await self._inner.embed(texts, input_type)


async def main(sslrootcert: str | None) -> int:
    scope_id = f"smoke-observe-{uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex[:8]
    db = await Database.connect(sslrootcert=sslrootcert)
    print(f"connected, scope_id={scope_id}")
    all_ok = True
    task_id = normalized = None

    async with CohereEmbeddings() as real:
        provider = CountingProvider(real)
        try:
            probe = ProbeResult(
                query_text=f"SELECT * FROM orders_{marker} WHERE customer_id = 12345",
                probe_latency_ms=4300.0,
                plan_has_seq_scan=True,
                index_candidate="customer_id",
                table_name=f"orders_{marker}",
                target_cluster_id=f"smoke-cluster-{marker}",
            )
            normalized = normalize_query_text(probe["query_text"])

            print(f"\n{RULE}\nFIRST SWEEP — new incident, must embed fresh\n{RULE}")
            state1 = {"observations": []}
            update1 = await observe(state1, db, provider, probe, scope_id=scope_id)
            task_id = update1["task_id"]
            record("phase is 'observe'", update1["phase"] == "observe")
            record("incident_fingerprint is set (anomaly rule fired)",
                   update1["incident_fingerprint"] is not None)
            record("1 observation accumulated", len(update1["observations"]) == 1)
            record("provider called once (fresh embed)", provider.calls == 1, f"calls={provider.calls}")

            print(f"\n{RULE}\nSECOND SWEEP — same query, must dedupe onto the SAME task\n{RULE}")
            update2 = await observe(update1, db, provider, probe, scope_id=scope_id)
            record("second sweep attaches to the SAME task_id (incident dedupe)",
                   update2["task_id"] == task_id, f"{update2['task_id']} vs {task_id}")
            record("observations accumulated to 2, not reset", len(update2["observations"]) == 2)
            record("provider NOT called again (embedding cache hit)", provider.calls == 1,
                   f"calls={provider.calls}")

            print(f"\n{RULE}\nDB STATE — both observation rows really exist under one task\n{RULE}")
            obs_rows = await db._read(
                "SELECT observation_id FROM observations WHERE task_id = %s", (task_id,)
            )
            record("2 observation rows in the DB for this task", len(obs_rows) == 2,
                   f"{len(obs_rows)} row(s)")
            item_rows = await db._read(
                "SELECT item_id, embedding FROM memory_items WHERE scope_id = %s AND class = 'query_fingerprint'",
                (scope_id,),
            )
            record("2 memory_items rows written (one per sweep, not deduped at this layer)",
                   len(item_rows) == 2, f"{len(item_rows)} row(s) — see note in CLAUDE.md")

        except Exception as exc:  # noqa: BLE001
            all_ok = False
            record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

        finally:
            print(f"\n{RULE}\nCLEANUP\n{RULE}")
            try:
                async with db._pool.connection() as conn:
                    async with conn.cursor() as cur:
                        if task_id:
                            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
                        await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
                        await cur.execute("DELETE FROM entities WHERE scope_id = %s", (scope_id,))
                        if normalized:
                            await cur.execute(
                                "DELETE FROM embedding_cache WHERE content_sha256 = %s",
                                (__import__("hashlib").sha256(
                                    f"search_document:{normalized}".encode()).hexdigest(),),
                            )
                print(f"  cleaned up scope_id={scope_id}, task_id={task_id}")
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
