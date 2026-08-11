"""Engram · agent/nodes/recall.py — embed → ANN → hybrid re-rank → context bundle.  [BRAINS]

design/02-low-level-design.md §5.2. **The first LangGraph node in this
repo** — wires `agent/memory/embeddings.py` (cache-aware embed) +
`agent/memory/recall.py` (ANN + hybrid re-rank) + `agent/memory/db.py`
(decision audit row) into the actual pipeline `AgentState.recall_bundle`
exists to hold. This is the "it remembers" demo beat's real code path.

Written as a plain async function taking/returning `AgentState`, matching
LangGraph's own node contract (`async def node(state) -> dict`, a partial
state update) — `agent/graph.py` (not yet written) is what will actually
import `langgraph` and wire nodes into a `StateGraph`; this function has no
dependency on that package at all, so it's fully testable without it.

INTERFACE ASSUMPTION, stated up front (see `agent/state.py`'s `Observation`
docstring): the text to embed comes from the first `Observation` whose
`payload` has a `"text"` key — `observe(node)` (not yet written) is expected
to populate it with whatever it fingerprinted. Only `_extract_query_text`
needs to change if that assumption turns out wrong.
"""

from __future__ import annotations

import os
import time

from agent.memory import recall as ann_recall
from agent.memory.db import Database
from agent.memory.embeddings import embed_and_cache
from agent.providers.base import EmbeddingProvider
from agent.state import AgentState, Observation, RecallBundle

DEFAULT_LIMIT = 20      # §5.2 point 2
DEFAULT_BEAM = 64       # §5.2 point 2: "beam 64 for remediation class"
DEFAULT_TOP_K = 5


def _extract_query_text(observations: list[Observation]) -> str | None:
    for obs in observations:
        text = (obs.get("payload") or {}).get("text")
        if text:
            return text
    return None


def _incident_entities(observations: list[Observation]) -> set[str]:
    entities: set[str] = set()
    for obs in observations:
        entities.update(obs.get("entity_ids") or [])
    return entities


def _empty_bundle(query_text: str, t0: float) -> RecallBundle:
    return RecallBundle(
        items=[], query_text=query_text, input_type="search_query",
        latency_ms=(time.perf_counter() - t0) * 1000, hit=False,
    )


async def recall(
    state: AgentState,
    db: Database,
    embed_provider: EmbeddingProvider,
    *,
    limit: int = DEFAULT_LIMIT,
    beam: int = DEFAULT_BEAM,
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """Returns a partial `AgentState` update: `{"recall_bundle": ..., "phase": "recall"}`.

    Never raises on a cold start (no observation text yet) or a zero-hit
    recall — both are the normal outcomes §5.2 point 5 names explicitly
    ("incident proceeds with zero-shot prompt"), not failures. A real
    provider/DB error still propagates — this only absorbs the "nothing to
    recall" case, not exceptions.
    """
    t0 = time.perf_counter()
    query_text = _extract_query_text(state["observations"])
    if not query_text:
        return {"recall_bundle": _empty_bundle("", t0), "phase": "recall"}

    vectors = await embed_and_cache(db, embed_provider, [query_text], "search_query")
    query_vector = vectors[0]

    incident_entities = _incident_entities(state["observations"])
    items = await ann_recall.recall(
        db, state["scope_id"], query_vector, incident_entities,
        limit=limit, beam=beam, top_k=top_k,
    )

    citations = [
        {
            "memory_item_id": str(item["item_id"]),
            "score": item["hybrid_score"],
            "source": item.get("class", "unknown"),
        }
        for item in items
    ]
    # §5.2 point 4: persist decisions(node='recall', citations, scores) — the
    # audit row this whole pipeline exists to make possible, not an afterthought.
    await db.insert_decision(
        state["task_id"],
        state["scope_id"],
        "recall",
        model_id=os.environ.get("ENGRAM_EMBED_MODEL", "embed-english-v3.0"),
        reasoning={"query_text": query_text, "top_k": top_k},
        citations=citations,
    )

    bundle = RecallBundle(
        items=items,
        query_text=query_text,
        input_type="search_query",
        latency_ms=(time.perf_counter() - t0) * 1000,
        hit=bool(items),
    )
    return {"recall_bundle": bundle, "phase": "recall"}
