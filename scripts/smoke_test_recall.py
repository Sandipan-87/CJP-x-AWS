#!/usr/bin/env python3
"""Engram · smoke test for agent/memory/recall.py + scoring.py against a live cluster.  [PLUMBER]

Seeds two memory_items with real 1024-dim vectors under a disposable
scope_id (no vector index needed for correctness — that's migration 003,
an efficiency structure, not what makes `<=>` return correct distances) and
checks: recall_ann finds the closer vector first, get_candidate_details
returns entity_id, and the full recall() pipeline hard-filters and
hybrid-scores correctly on real rows, not just in scoring.py's unit tests.

    python scripts/smoke_test_recall.py --sslrootcert cluster-ca.crt

Exit 0 only if every check passed and cleanup succeeded.
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
from agent.memory.recall import recall

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


def _vec(seed: float) -> list[float]:
    """A cheap deterministic 1024-dim vector: mostly `seed`, nudged by index
    so it isn't degenerate, and NOT unit-norm (cosine distance doesn't need it)."""
    return [seed + (i % 7) * 1e-4 for i in range(1024)]


async def main(sslrootcert: str | None) -> int:
    scope_id = f"smoke-recall-{uuid.uuid4().hex[:8]}"
    db = await Database.connect(sslrootcert=sslrootcert)
    print(f"connected, scope_id={scope_id}")
    all_ok = True
    close_id = near_id = far_id = entity_id = None

    try:
        print(f"\n{RULE}\nSEED\n{RULE}")
        entity_id = await db.upsert_entity(scope_id, "table", "orders", {})
        query_vec = _vec(1.0)
        near_vec = _vec(1.0001)   # very close to query_vec
        far_vec = _vec(-5.0)      # far from query_vec

        near_id = await db.insert_memory_item(
            scope_id, "episode", "near item", embedding=near_vec,
            entity_id=entity_id, provenance={"note": "near"},
        )
        far_id = await db.insert_memory_item(
            scope_id, "episode", "far item", embedding=far_vec,
            provenance={"note": "far"},
        )
        record("seed 2 memory_items with real embeddings", True, f"near={near_id} far={far_id}")

        print(f"\n{RULE}\nRECALL_ANN — correctness without a vector index\n{RULE}")
        rows = await db.recall_ann(scope_id, query_vec, limit=10)
        record("recall_ann returns both rows", len(rows) == 2, f"{len(rows)} row(s)")
        ordered_ids = [str(r["item_id"]) for r in rows]
        record("recall_ann orders near before far", ordered_ids == [near_id, far_id],
               f"got order {ordered_ids}")
        record("similarity is higher for the near item",
               rows[0]["similarity"] > rows[1]["similarity"],
               f"{rows[0]['similarity']:.6f} vs {rows[1]['similarity']:.6f}")

        print(f"\n{RULE}\nGET_CANDIDATE_DETAILS — entity_id present\n{RULE}")
        details = await db.get_candidate_details([near_id, far_id])
        by_id = {str(d["item_id"]): d for d in details}
        record("get_candidate_details includes entity_id", by_id[near_id]["entity_id"] is not None,
               str(by_id[near_id]["entity_id"]))
        record("far item has no entity_id (never set)", by_id[far_id]["entity_id"] is None)

        print(f"\n{RULE}\nFULL RECALL PIPELINE (agent/memory/recall.py)\n{RULE}")
        scored = await recall(db, scope_id, query_vec, incident_entities={entity_id}, top_k=5)
        record("recall() returns scored candidates", len(scored) == 2, f"{len(scored)} row(s)")
        if scored:
            record("near item ranks first", str(scored[0]["item_id"]) == near_id,
                   f"top item_id={scored[0]['item_id']}")
            record("near item scores higher than far (entity_affinity + similarity both favor it)",
                   scored[0]["hybrid_score"] > scored[1]["hybrid_score"],
                   f"{scored[0]['hybrid_score']:.4f} vs {scored[1]['hybrid_score']:.4f}")

        no_match = await recall(db, f"{scope_id}-empty", query_vec)
        record("recall() on an unrelated scope_id returns empty, not an error", no_match == [])

        print(f"\n{RULE}\nREGRESSION -- a NULL-embedding row (seed-then-backfill) must not crash recall\n{RULE}")
        # Real bug, 2026-08-12: a live incident's second recall() call crashed with a plain
        # TypeError ("0.45 * None") the moment an episode row with embedding=NULL (not yet
        # backfilled) showed up as an ANN candidate -- recall_ann() returned similarity=None
        # for it instead of excluding it. Fixed with `AND embedding IS NOT NULL`; this seeds
        # the exact shape that broke it and proves both recall_ann() and the full pipeline
        # now handle it cleanly.
        null_embed_id = await db.insert_memory_item(
            scope_id, "episode", "not yet backfilled", embedding=None, provenance={"note": "null-embedding"},
        )
        rows = await db.recall_ann(scope_id, query_vec, limit=10)
        record("recall_ann excludes the NULL-embedding row", null_embed_id not in {str(r["item_id"]) for r in rows})
        scored_with_null = await recall(db, scope_id, query_vec, incident_entities={entity_id}, top_k=5)
        record("recall() does not crash with a NULL-embedding row present in scope", True)
        record("recall() still excludes the NULL-embedding row from its own output",
               null_embed_id not in {str(s["item_id"]) for s in scored_with_null})

    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        try:
            async with db._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM memory_items WHERE scope_id = %s", (scope_id,))
                    await cur.execute("DELETE FROM entities WHERE scope_id = %s", (scope_id,))
            print(f"  cleaned up scope_id={scope_id}")
        except Exception as exc:  # noqa: BLE001
            all_ok = False
            print(f"  CLEANUP FAILED: {exc} -- manual cleanup needed for scope_id={scope_id}")
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
