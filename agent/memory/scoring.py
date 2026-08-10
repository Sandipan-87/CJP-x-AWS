"""Engram · agent/memory/scoring.py — pure re-rank functions.  [PLUMBER]

design/02-low-level-design.md §6.6 (invariants #9, #10). Deliberately pure:
no DB access, no imports beyond `math` — everything here is a plain function
of numbers/sets, so it can be unit-tested without a cluster (LLD's own test
plan: "1/1 must not outrank 47/50; hard filters; decay monotonicity").

DEVIATION FROM THE LLD'S SHOWN SIGNATURE, stated up front: §6.6 writes
`hybrid(item, incident, age_days, z=1.96)` against two duck-typed objects
(`item.confidence`, `item.successes`, `item.entities`, ...). That protocol is
never defined elsewhere in the LLD, and the schema (`memory_items.entity_id`
is a SINGLE FK, not a set) doesn't actually support `item.entities` as
written. `hybrid()` below takes explicit scalar/set keyword arguments
instead — same math, same hard filters, no invented object type. The
entity-affinity call site (`agent/memory/recall.py`) documents the resulting
schema-driven simplification: an item has at most one entity, not a set.
"""

from __future__ import annotations

from math import exp, sqrt

# invariant #9's fixed weights — similarity / confidence / recency / entity_affinity
_W_SIMILARITY = 0.45
_W_CONFIDENCE = 0.30
_W_RECENCY = 0.15
_W_AFFINITY = 0.10

CONFIDENCE_FLOOR = 0.15  # invariant #9's hard filter


def wilson_lb(successes: int, attempts: int, z: float = 1.96) -> float:
    """Wilson score lower bound (invariant #10) — why a 1/1 procedure must
    not outrank a 47/50 one: a single success has a wide confidence interval,
    so its LOWER bound is pulled down hard; 47/50 has a narrow interval near
    0.94.
    """
    if attempts == 0:
        return 0.0
    p = successes / attempts
    denom = 1 + z * z / attempts
    centre = p + z * z / (2 * attempts)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * attempts)) / attempts)
    return max(0.0, (centre - margin) / denom)


def recency(age_days: float, tau: float = 14.0) -> float:
    """Exponential decay, half-life ~tau*ln(2) days."""
    return exp(-age_days / tau)


def entity_affinity(item_entities: set, incident_entities: set) -> float:
    """Jaccard similarity. Empty/empty is defined as 0 (no shared context to
    reward), not 1 — `max(1, ...)` in the denominator only guards div-by-zero
    when exactly one side is empty.
    """
    union = item_entities | incident_entities
    if not union:
        return 0.0
    return len(item_entities & incident_entities) / len(union)


def hybrid(
    *,
    similarity: float,
    stored_confidence: float,
    successes: int,
    attempts: int,
    age_days: float,
    status: str,
    item_entities: set = frozenset(),
    incident_entities: set = frozenset(),
    z: float = 1.96,
) -> float:
    """Invariant #9's hybrid score. Hard filter uses the STORED, already
    time-decayed confidence (invariant #10, `procedures.confidence`) — the
    `wilson_lb(...)` computed inside the weighted sum below is a SEPARATE,
    query-time-fresh estimate from raw successes/attempts, not a re-read of
    the same field. Both exist on purpose: the stored value decays on its
    own schedule (nightly decayer worker); this one reacts immediately to
    `age_days` for whichever incident is being scored right now.
    """
    if stored_confidence < CONFIDENCE_FLOOR or status != "active":
        return float("-inf")
    sim = similarity
    conf = wilson_lb(successes, attempts, z) * exp(-age_days / 90)
    rec = recency(age_days)
    aff = entity_affinity(item_entities, incident_entities)
    return _W_SIMILARITY * sim + _W_CONFIDENCE * conf + _W_RECENCY * rec + _W_AFFINITY * aff
