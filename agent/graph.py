"""Engram · agent/graph.py — LangGraph StateGraph assembly.  [BRAINS]

design/02-low-level-design.md §4. **All five nodes the LLD names now
exist, and (2026-08-13) so does every edge the LLD names** — the full
loop, including both re-plan paths, is compiled and invocable:

    observe ──► recall ──► reason ──► gate ──► act_measure ──► (done)
      │                       ▲          │           │
      ▼                       └──────────┴───────────┘
    (no anomaly → done)     (reject, budget left / measurement fails, budget left → reason)
                             (reject, no budget / expire → done)

The conditional edges are LLD §4's own: "observe → done if no anomaly,"
"gate → done on reject/expiry, → reason (re-plan) if measurement fails."
**Closing the gap this file's own history carried since Session 26**: a
rejection at `gate` OR a measured regression at `act_measure` now routes
back to `reason` for a genuinely different proposal — but ONLY while
re-plan budget remains (`agent/nodes/gate.py`/`agent/nodes/act_measure.py`'s
own `MAX_REPLANS=2`, `state["replan_count"]` the loop-prevention counter
this docstring previously said didn't exist yet). An `expiry` at `gate`
never re-plans regardless of budget — nobody was watching to reject it,
so retrying automatically would likely just time out again for nothing.
`state["replan_reason"]` carries WHY the previous attempt didn't stick
(the human's rejection comment, or the measured before/after latency) into
`reason(node)`'s next prompt, so a re-plan is actually informed, not a
blind repeat of the same proposal.

A failed `reason` (exhausted repair rounds, `LLMSchemaError`) or a blocked
`act_measure` (`BackupGateBlocked`) both raise rather than route anywhere —
LLD §16's "park" recovery is a caller-level concern (the process that
invoked `graph.ainvoke(...)` catches the exception and parks the task),
not a graph edge.

CHECKPOINTER now wired (Phase 3, `scripts/bootstrap_checkpointer.py` closed
the bootstrap gap this deferred): `build_graph()` takes an optional
`checkpointer: BaseCheckpointSaver | None`. Passing `None` (the default)
compiles exactly as before — this is additive, not a breaking change for
any existing caller. When a real `AsyncCockroachDBSaver` is passed,
`graph.ainvoke(state, config={"configurable": {"thread_id": ...}})` MUST
supply a `thread_id` in `config` — LangGraph raises without one once a
checkpointer is set.

**Deliberately NOT closed by this chunk, stated not hidden:** LLD §3 says
"`thread_id = task_id`," but `tasks` (migration 001) already has a
*separate* `checkpoint_thread_id STRING` column — meaning the schema itself
already anticipated these being two different values, not one. That makes
sense given the actual data flow: a LangGraph `thread_id` must be chosen
BEFORE `graph.ainvoke()` is ever called (it lives in `config`, supplied by
the caller), but the real DB `task_id` doesn't exist until `observe(node)`
dedupes an incident and inserts (or reuses) a `tasks` row — *after* the
graph is already running. So whatever mints a `thread_id` today (not yet
written — that's `main.py`'s job, still unbuilt) has no way to know the
`task_id` in advance, and nothing in this codebase yet writes a chosen
`thread_id` back into `tasks.checkpoint_thread_id` for reconciliation. That
write is real follow-up work, not assumed away by wiring the checkpointer.

Real DB writes still happen independent of whatever LangGraph itself is or
isn't checkpointing — every node writes to CockroachDB directly regardless.
Kill-and-resume already lives entirely in `agent/memory/leases.py`, proven
in `scripts/smoke_test_leases.py`; LangGraph-level checkpointing is a
second, additive layer on top of that, not a replacement for it.

TELEMETRY now wired the same additive way: `build_graph()` takes an optional
`telemetry: Telemetry | None` (`agent/telemetry.py`), passed straight through
to every node's own `telemetry=` param. `None` (the default) compiles and
runs exactly as before every node's telemetry wiring landed — no existing
caller or test needed to change.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.memory.db import Database
from agent.nodes.act_measure import act_measure as act_measure_fn
from agent.nodes.gate import DEFAULT_POLL_INTERVAL_S, DEFAULT_TIMEOUT_S
from agent.nodes.gate import gate as gate_fn
from agent.nodes.observe import ProbeResult
from agent.nodes.observe import observe as observe_fn
from agent.nodes.reason import reason as reason_fn
from agent.nodes.recall import recall as recall_fn
from agent.providers.base import EmbeddingProvider, LLMProvider
from agent.state import AgentState
from agent.telemetry import Telemetry
from agent.tools.cloud_api import DEFAULT_WINDOW_HOURS, CloudApiAdapter
from agent.tools.sql_operator import SqlOperator
from agent.tools.sql_probe import SqlProbe


def _route_after_observe(state: AgentState) -> str:
    """LLD §4: "observe → done if no anomaly." `incident_fingerprint` is
    only set when `agent/nodes/observe.py`'s `is_anomaly()` fired.
    """
    return "recall" if state.get("incident_fingerprint") else END


def _route_after_gate(state: AgentState) -> str:
    """LLD §4: "gate → done on reject/expiry, → reason (re-plan) if
    [it decides to]." `gate(node)` returns `phase='gate'` on approval
    (ready for `act_measure`), `phase='replan'` on a rejection with re-plan
    budget left (`agent/nodes/gate.py`'s `MAX_REPLANS`), or `phase='done'`
    on expiry / a rejection with no budget left.
    """
    phase = state.get("phase")
    if phase == "gate":
        return "act_measure"
    if phase == "replan":
        return "reason"
    return END


def _route_after_act_measure(state: AgentState) -> str:
    """LLD §4: "→ reason (re-plan) if measurement fails." `act_measure
    (node)` returns `phase='replan'` on a measured regression with re-plan
    budget left (`agent/nodes/act_measure.py`'s `MAX_REPLANS`), else
    `phase='done'` regardless of success/failure.
    """
    return "reason" if state.get("phase") == "replan" else END


def build_graph(
    db: Database,
    embed_provider: EmbeddingProvider,
    llm: LLMProvider,
    sql_probe: SqlProbe,
    sql_operator: SqlOperator,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    backup_gate: CloudApiAdapter | None = None,
    override_backup_gate: bool = False,
    gate_poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    gate_timeout_s: float = DEFAULT_TIMEOUT_S,
    backup_window_hours: float = DEFAULT_WINDOW_HOURS,
    telemetry: Telemetry | None = None,
) -> CompiledStateGraph:
    """Returns a compiled, invocable LangGraph app.

    Every dependency is bound via closures over its node — LangGraph nodes
    only ever receive `state`, so this is how the per-process dependencies
    (live connection pools, live provider clients) get in without
    threading them through every `graph.ainvoke(...)` call. `sql_probe` is
    shared between `gate` (optional schema cross-check) and `act_measure`
    (before/after `EXPLAIN ANALYZE`) — one connection, reused across both.

    `checkpointer`, if given (typically a live `AsyncCockroachDBSaver` —
    see `scripts/bootstrap_checkpointer.py` for the one-time setup its
    tables need first), makes every node return a real checkpoint commit
    (LLD §4). Every `graph.ainvoke(...)` call then MUST pass
    `config={"configurable": {"thread_id": ...}}` — see module docstring
    for the still-open `thread_id`/`task_id` reconciliation gap.
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
            telemetry=telemetry,
        )

    async def _recall(state: AgentState) -> dict[str, Any]:
        return await recall_fn(state, db, embed_provider, telemetry=telemetry)

    async def _reason(state: AgentState) -> dict[str, Any]:
        return await reason_fn(state, db, llm, telemetry=telemetry)

    async def _gate(state: AgentState) -> dict[str, Any]:
        return await gate_fn(
            state, db, sql_probe=sql_probe,
            poll_interval_s=gate_poll_interval_s, timeout_s=gate_timeout_s,
            telemetry=telemetry,
        )

    async def _act_measure(state: AgentState) -> dict[str, Any]:
        return await act_measure_fn(
            state, db, sql_probe, sql_operator,
            backup_gate=backup_gate, backup_window_hours=backup_window_hours,
            override_backup_gate=override_backup_gate,
            telemetry=telemetry,
        )

    graph = StateGraph(AgentState)
    graph.add_node("observe", _observe)
    graph.add_node("recall", _recall)
    graph.add_node("reason", _reason)
    graph.add_node("gate", _gate)
    graph.add_node("act_measure", _act_measure)
    graph.set_entry_point("observe")
    graph.add_conditional_edges("observe", _route_after_observe, {"recall": "recall", END: END})
    graph.add_edge("recall", "reason")
    graph.add_edge("reason", "gate")
    graph.add_conditional_edges(
        "gate", _route_after_gate, {"act_measure": "act_measure", "reason": "reason", END: END}
    )
    graph.add_conditional_edges("act_measure", _route_after_act_measure, {"reason": "reason", END: END})

    return graph.compile(checkpointer=checkpointer)
