#!/usr/bin/env python3
"""Engram · smoke test for agent/nodes/recall.py — the first LangGraph node.  [BRAINS]

The actual "it remembers" demo beat's code path, end to end, against real
infrastructure: a prior memory item is seeded with a real Cohere embedding
(simulating observe(node) from an earlier incident), then the recall NODE
(not just memory/recall.py underneath it) is called with a fresh
AgentState for a "new" incident with similar text, and must: embed the
query, find the prior item, hybrid-score it, write a decisions(node=
'recall') audit row, and return a properly-shaped partial state update.
Also checks the cold-start path (no observation text) returns a clean miss
instead of raising.

    python scripts/smoke_test_recall_node.py --sslrootcert cluster-ca.crt
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
from agent.nodes.recall import recall
from agent.providers.cohere_embed import CohereEmbeddings

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def main(sslrootcert: str | None) -> int:
    scope_id = f"smoke-recall-node-{uuid.uuid4().hex[:8]}"
    marker = uuid.uuid4().hex[:8]
    db = await Database.connect(sslrootcert=sslrootcert)
    print(f"connected, scope_id={scope_id}")
    all_ok = True
    task_id = entity_id = None

    async with CohereEmbeddings() as provider:
        try:
            print(f"\n{RULE}\nSEED — simulate observe(node) from an earlier incident\n{RULE}")
            task_id = await db.insert_task(scope_id, "incident", "manual")
            entity_id = await db.upsert_entity(scope_id, "table", f"orders_{marker}", {})
            prior_text = f"slow query on orders_{marker}: full scan on customer_id, {marker}"
            from agent.memory.embeddings import embed_and_cache
            prior_vec = (await embed_and_cache(db, provider, [prior_text], "search_document"))[0]
            prior_item_id = await db.insert_memory_item(
                scope_id, "episode", prior_text, embedding=prior_vec,
                entity_id=entity_id, provenance={"note": "prior incident"},
            )
            record("seeded prior memory_item with a real embedding", True, prior_item_id)

            print(f"\n{RULE}\nRECALL NODE — a 'new' incident with similar text\n{RULE}")
            state = {
                "task_id": task_id,
                "scope_id": scope_id,
                "target_cluster_id": "smoke-cluster",
                "trigger": "manual",
                "phase": "observe",
                "observations": [
                    {
                        "source": "sql_probe",
                        "kind": "query_stats",
                        "fingerprint": "smoke-fp",
                        "entity_ids": [entity_id],
                        "payload": {"text": prior_text},  # same text -> should be a near-perfect match
                    }
                ],
                "incident_fingerprint": "smoke-fp",
                "recall_bundle": None,
                "proposal": None, "approval": None, "action": None,
                "measurement": None, "error": None, "model_meta": {},
            }
            update = await recall(state, db, provider)
            record("returns phase='recall'", update.get("phase") == "recall")
            bundle = update["recall_bundle"]
            record("bundle.hit is True", bundle["hit"] is True)
            record("bundle has at least 1 item", len(bundle["items"]) >= 1, f"{len(bundle['items'])}")
            record("top item is the prior memory_item", bundle["items"] and
                   str(bundle["items"][0]["item_id"]) == prior_item_id)
            # NOT ~1.0: the seed was embedded search_document, the query search_query --
            # Cohere's asymmetric embeddings (the exact thing embeddings.py's input_type
            # keying fix protects) mean same-text cross-type similarity is naturally
            # lower than same-type would be, while still a strong, correct match.
            record("top item's similarity is a strong match (cross-input_type, same text)",
                   bundle["items"] and bundle["items"][0]["similarity"] > 0.5,
                   f"{bundle['items'][0]['similarity'] if bundle['items'] else None}")
            record("latency_ms was recorded", bundle["latency_ms"] > 0, f"{bundle['latency_ms']:.1f}ms")

            print(f"\n{RULE}\nAUDIT — a decisions(node='recall') row was actually written\n{RULE}")
            rows = await db._read(
                "SELECT * FROM decisions WHERE task_id = %s AND node = 'recall'", (task_id,)
            )
            record("exactly one recall decision row exists", len(rows) == 1, f"{len(rows)} row(s)")
            if rows:
                record("citations were persisted", len(rows[0]["citations"]) >= 1)

            print(f"\n{RULE}\nCOLD START — no observation text, must be a clean miss, not an error\n{RULE}")
            cold_state = dict(state)
            cold_state["observations"] = []
            cold_update = await recall(cold_state, db, provider)
            cold_bundle = cold_update["recall_bundle"]
            record("cold start returns hit=False, not an exception", cold_bundle["hit"] is False)
            record("cold start returns empty items", cold_bundle["items"] == [])

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
                        await cur.execute(
                            "DELETE FROM embedding_cache WHERE content_sha256 = %s",
                            (__import__("hashlib").sha256(
                                f"search_document:{prior_text}".encode()).hexdigest(),),
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
