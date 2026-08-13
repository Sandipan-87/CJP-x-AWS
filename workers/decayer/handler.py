"""Engram · workers/decayer/handler.py — nightly confidence decay + retirement.  [PLUMBER]

design/02-low-level-design.md §9: "`UPDATE procedures SET confidence = wilson(successes,attempts)
* exp(-(now()-updated_at)/90d)`; `status='retired'` when < 0.15; `memory_items.status='retired'`
for orphaned embeddings." Idempotency: "batch by `(procedure_id)` -- reruns are naturally
idempotent."

**Why reruns are naturally idempotent, stated explicitly (this is load-bearing, not incidental)**:
`confidence` is recomputed EVERY run as a pure function of `outcome_stats` (unchanged by this
worker) and `age_days = now() - updated_at`. `update_procedure_confidence` never touches
`updated_at` -- if it did, every nightly run would reset the decay clock to zero and no procedure
would ever actually decay. Running this twice in a row (or twice concurrently) produces the same
`confidence` value both times, give or take the few seconds `now()` moved between calls -- there
is no state here a rerun could corrupt.

`CONFIDENCE_FLOOR` (0.15) is invariant #9's own hard recall filter, restated here as the
retirement threshold: a procedure below the floor could never be recalled anyway (invariant #9),
so leaving it 'active' forever would just be a permanently-dead row `recall()` scans past on
every single query. Retiring it also retires its own `memory_items(class='procedure')` row(s) --
LLD's "orphaned embeddings" -- so nothing in the vector index still points at a procedure nobody
can act on any more.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from common.db import get_decayer_connection, list_decaying_procedures, retire_orphaned_memory_items, update_procedure_confidence
from common.scoring import CONFIDENCE_FLOOR, decayed_confidence

logger = logging.getLogger("engram.decayer")
logger.setLevel(logging.INFO)


def _age_days(updated_at: datetime) -> float:
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - updated_at
    return max(0.0, delta.total_seconds() / 86400.0)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    conn = get_decayer_connection()
    rows = list_decaying_procedures(conn)
    logger.info("decayer: %d non-retired procedure(s)", len(rows))

    decayed = 0
    retired = 0
    orphaned_items_retired = 0
    failed = 0
    for row in rows:
        try:
            stats = row["outcome_stats"] or {}
            successes = int(stats.get("successes", 0))
            attempts = int(stats.get("attempts", 0))
            confidence = decayed_confidence(successes, attempts, _age_days(row["updated_at"]))
            retire = confidence < CONFIDENCE_FLOOR
            update_procedure_confidence(conn, row["procedure_id"], confidence, retire)
            decayed += 1
            if retire:
                retired += 1
                orphaned_items_retired += retire_orphaned_memory_items(conn, row["procedure_id"])
        except Exception as exc:  # noqa: BLE001 -- one bad procedure must not block the batch
            failed += 1
            logger.error("failed to decay procedure_id=%s: %s", row.get("procedure_id"), exc)

    logger.info(
        "decayer complete: decayed=%d retired=%d orphaned_items_retired=%d failed=%d candidates=%d",
        decayed, retired, orphaned_items_retired, failed, len(rows),
    )
    return {
        "statusCode": 200,
        "decayed": decayed,
        "retired": retired,
        "orphaned_items_retired": orphaned_items_retired,
        "failed": failed,
        "candidates": len(rows),
    }
