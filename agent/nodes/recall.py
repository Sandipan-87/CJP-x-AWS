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

Telemetry (§5.2 point 4's `memory_recall_latency_p99`, `recall_top1_score`)
is now wired via the additive `telemetry: Telemetry | None = None` param
(`agent/telemetry.py`) — `None` (unpassed) is identical to every prior
session's behavior. `recall_top1_score` isn't in LLD §12's own dashboard
table, so it's a span attribute only; `recall_hit_rate` (which IS in that
table) is emitted here as 1.0/0.0 per call — CloudWatch's own `Average`
statistic (already how `workers/metrics/handler.py` queries it) turns a
stream of 1.0/0.0 samples into the actual hit *rate* over a window.
"""

from __future__ import annotations

import os
import time

from agent.memory import recall as ann_recall
from agent.memory.db import Database
from agent.memory.embeddings import embed_and_cache
from agent.providers.base import EmbeddingProvider
from agent.state import AgentState, Observation, RecallBundle
from agent.telemetry import Telemetry, maybe_record, maybe_span, set_attr

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
    telemetry: Telemetry | None = None,
) -> dict:
    """Returns a partial `AgentState` update: `{"recall_bundle": ..., "phase": "recall"}`.

    Never raises on a cold start (no observation text yet) or a zero-hit
    recall — both are the normal outcomes §5.2 point 5 names explicitly
    ("incident proceeds with zero-shot prompt"), not failures. A real
    provider/DB error still propagates — this only absorbs the "nothing to
    recall" case, not exceptions.
    """
    t0 = time.perf_counter()
    with maybe_span(telemetry, "recall", task_id=state.get("task_id"), scope_id=state["scope_id"]) as span:
        query_text = _extract_query_text(state["observations"])
        if not query_text:
            bundle = _empty_bundle("", t0)
            set_attr(span, "retrieved_count", 0)
            set_attr(span, "latency_ms", bundle["latency_ms"])
            set_attr(span, "outcome", "cold_start")
            await maybe_record(
                telemetry, "recall_hit_rate", 0.0, dimensions={"scope_id": state["scope_id"]}
            )
            await maybe_record(
                telemetry, "memory_recall_latency_p99", bundle["latency_ms"],
                dimensions={"scope_id": state["scope_id"]},
            )
            return {"recall_bundle": bundle, "phase": "recall"}

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
        # recall_top1_score isn't in LLD §12's dashboard table -- span attribute only.
        set_attr(span, "retrieved_count", len(items))
        set_attr(span, "top1_score", items[0]["hybrid_score"] if items else 0.0)
        set_attr(span, "latency_ms", bundle["latency_ms"])
        set_attr(span, "outcome", "hit" if items else "miss")

    await maybe_record(
        telemetry, "recall_hit_rate", 1.0 if bundle["hit"] else 0.0, dimensions={"scope_id": state["scope_id"]}
    )
    await maybe_record(
        telemetry, "memory_recall_latency_p99", bundle["latency_ms"], dimensions={"scope_id": state["scope_id"]}
    )
    return {"recall_bundle": bundle, "phase": "recall"}
