"""Engram · agent/graph.py — LangGraph StateGraph assembly.  [BRAINS]

design/02-low-level-design.md §4. Wires the three nodes that exist —
`observe`, `recall`, `reason` — per the diagram's first two edges and
observe's own conditional edge ("no anomaly → done"):

    observe ──► recall ──► reason ──► (done)
      │
      ▼
    (no anomaly → done)

`gate`/`act_measure` don't exist yet, so this graph currently ends at
`reason` → END — not because the LLD's diagram stops there, but because
there is nothing real to wire past it yet (coding-conduct rule 2: no
speculative stub nodes standing in for unwritten ones). A failed `reason`
(exhausted repair rounds) raises `LLMSchemaError` rather than routing
anywhere — LLD §16's "park" recovery is a caller-level concern (the
process that invoked `graph.ainvoke(...)` catches it and parks the task),
not a graph edge.

CHECKPOINTER DEFERRED, stated up front, not hidden: LLD §4 calls for
`AsyncCockroachDBSaver` on the memory cluster ("no side effect without a
checkpoint commit"). Wiring it needs its own bootstrap sequence
(`saver.setup()` on an EMPTY cluster, immediately followed by migration
004's TTL — `db/migrations/README.md`) — a real, separate piece of work,
not a side effect of assembling two nodes. This graph compiles and runs
WITHOUT a persistent checkpointer for now (`compile()`'s default). Real DB
writes still happen for real: `observe`/`recall` write to CockroachDB
directly, independent of whatever LangGraph itself is or isn't
checkpointing — only cross-*run* resume-from-checkpoint isn't wired yet.
Kill-and-resume today lives entirely in `agent/memory/leases.py`, proven
in `scripts/smoke_test_leases.py`; LangGraph-level checkpointing is a
second, additive layer, not a prerequisite for the mechanism that already
exists.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.memory.db import Database
from agent.nodes.observe import ProbeResult
from agent.nodes.observe import observe as observe_fn
from agent.nodes.reason import reason as reason_fn
from agent.nodes.recall import recall as recall_fn
from agent.providers.base import EmbeddingProvider, LLMProvider
from agent.state import AgentState


def _route_after_observe(state: AgentState) -> str:
    """LLD §4: "observe → done if no anomaly." `incident_fingerprint` is
    only set when `agent/nodes/observe.py`'s `is_anomaly()` fired.
    """
    return "recall" if state.get("incident_fingerprint") else END


def build_graph(
    db: Database, embed_provider: EmbeddingProvider, llm: LLMProvider
) -> CompiledStateGraph:
    """Returns a compiled, invocable LangGraph app.

    `db`/`embed_provider`/`llm` are bound via closures over each node —
    LangGraph nodes only ever receive `state`, so this is how the
    per-process dependencies (a live connection pool, a live embedding
    provider, a live LLM provider) get in without threading them through
    every `graph.invoke(...)` call.
    """

    async def _observe(state: AgentState) -> dict[str, Any]:
        probe = state.get("initial_probe")
        if probe is None:
            raise ValueError(
                "graph invocation must seed state['initial_probe'] (a ProbeResult-shaped "
                "dict, e.g. from agent.nodes.observe.probe_result_from_explain) -- "
                "observe(node) has no other way to receive a sweep's raw signal"
            )
        return await observe_fn(
            state, db, embed_provider, ProbeResult(**probe),
            scope_id=state["scope_id"], trigger=state.get("trigger", "manual"),
        )

    async def _recall(state: AgentState) -> dict[str, Any]:
        return await recall_fn(state, db, embed_provider)

    async def _reason(state: AgentState) -> dict[str, Any]:
        return await reason_fn(state, db, llm)

    graph = StateGraph(AgentState)
    graph.add_node("observe", _observe)
    graph.add_node("recall", _recall)
    graph.add_node("reason", _reason)
    graph.set_entry_point("observe")
    graph.add_conditional_edges("observe", _route_after_observe, {"recall": "recall", END: END})
    graph.add_edge("recall", "reason")
    graph.add_edge("reason", END)  # gate(node) doesn't exist yet — see module docstring

    return graph.compile()
