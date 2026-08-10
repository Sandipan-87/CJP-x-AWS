"""Engram · agent/memory/recall.py — ANN → hybrid re-rank → context bundle.  [PLUMBER]

design/02-low-level-design.md §6.5/§5.2 (invariant #3). Takes an
already-embedded query vector — the embed step (Cohere, `input_type=
'search_query'`) belongs to the not-yet-written `agent/nodes/recall.py`
graph node and `agent/providers/cohere_embed.py` provider; this module owns
everything from the ANN call onward.

`agent/memory/db.py`'s `recall_ann()` is the only method in the codebase
containing a `<=>` operator (LLD §6.5's grep-CI rule). This module is meant
to be the only CALLER of `recall_ann()` outside tests — routing every recall
through one function is what makes invariant #3's scope_id constraint
actually enforceable in review, not just in principle.

KNOWN SCHEMA LIMITATION, stated rather than hidden (see agent/memory/
scoring.py's docstring for the paired note): `memory_items.entity_id` is a
single FK, not a set, so `item_entities` below is a set of zero or one
elements. `entity_affinity()` still computes a real Jaccard score against
`incident_entities`, it just can never reward multi-entity overlap for a
single item — a real simplification inherited from the frozen schema
(migration 001), not invented here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from agent.memory.db import Database
from agent.memory.scoring import hybrid

DEFAULT_TOP_K = 5


async def recall(
    db: Database,
    scope_id: str,
    query_vector: Sequence[float],
    incident_entities: set[str] | None = None,
    *,
    limit: int = 20,
    beam: int | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Returns up to `top_k` candidates, hard-filtered and hybrid-scored,
    highest score first. Never raises on an empty result — an incident with
    no relevant memory is a normal outcome (cold start), not an error.
    """
    incident_entities = incident_entities or set()
    candidates = await db.recall_ann(scope_id, query_vector, limit=limit, beam=beam)
    if not candidates:
        return []

    details_by_id = {
        d["item_id"]: d for d in await db.get_candidate_details([c["item_id"] for c in candidates])
    }

    now = datetime.now(timezone.utc)
    scored: list[dict[str, Any]] = []
    for c in candidates:
        d = details_by_id.get(c["item_id"], {})

        # Non-procedure classes (episode, query_fingerprint, skill) have no
        # Wilson-LB confidence — invariant #9's hard filter is a PROCEDURE
        # concept (invariant #10); exempt everything else rather than
        # rejecting items the filter was never meant to touch.
        stored_confidence = d.get("confidence")
        is_procedure = stored_confidence is not None
        if not is_procedure:
            stored_confidence = 1.0

        outcome_stats = d.get("outcome_stats") or {}
        successes = outcome_stats.get("successes", 0)
        attempts = outcome_stats.get("attempts", 0)

        created_at = d.get("created_at") or now
        age_days = max(0.0, (now - created_at).total_seconds() / 86400)

        item_entities = {str(d["entity_id"])} if d.get("entity_id") else set()

        # recall_ann's own WHERE clause already enforces status='active', so
        # this is always 'active' in practice — kept explicit because
        # hybrid()'s hard filter is a general contract, not a recall_ann one.
        status = d.get("status", "active")

        score = hybrid(
            similarity=c["similarity"],
            stored_confidence=stored_confidence,
            successes=successes,
            attempts=attempts,
            age_days=age_days,
            status=status,
            item_entities=item_entities,
            incident_entities=incident_entities,
        )
        if score == float("-inf"):
            continue
        scored.append({**c, **d, "hybrid_score": score, "age_days": age_days})

    scored.sort(key=lambda row: row["hybrid_score"], reverse=True)
    return scored[:top_k]
