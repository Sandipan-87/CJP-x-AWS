"""Engram · agent/state.py — AgentState TypedDict + nested schemas.  [BRAINS]

design/02-low-level-design.md §3. `thread_id = task_id` (LangGraph checkpoint
key). JSON-serializable, checkpoint-safe — plain TypedDicts, no pydantic
model needed here since nothing in this file validates anything yet.

SCOPE, stated up front (coding-conduct rule 1): only `Observation` and
`RecallBundle` are fully typed — `agent/nodes/recall.py` is the only node
written so far and those are the only two shapes it actually touches.
`proposal`/`approval`/`action`/`measurement`/`error` are typed as
`dict | None` placeholders rather than the LLD's `Proposal`/`Approval`/
`ActionLedgerRow`/`Measurement`/`TypedError` — inventing those now, before
`reason`/`gate`/`act_measure` exist to be checked against, would be
speculative (coding-conduct rule 2). Tighten each placeholder when its node
lands, not before.
"""

from __future__ import annotations

from typing import TypedDict


class Observation(TypedDict):
    """§3's "recent, summarized" — an in-memory slice of the `observations`
    table's shape, not a 1:1 mirror (no `observation_id`/`task_id`; those
    belong to the DB row, not the state the graph carries between nodes).

    `observe(node)` (`agent/nodes/observe.py`) actually populates `payload`
    with two related-but-different text fields — caught while designing
    `act_measure(node)`, not assumed correctly on the first try:
      - `payload["text"]` — the NORMALIZED query (literals collapsed to
        `?`, LLD §5.1 step 2). `agent/nodes/recall.py` embeds THIS one.
      - `payload["raw_text"]` — the ORIGINAL, runnable SQL. `act_measure
        (node)` needs THIS one for its own before/after `EXPLAIN ANALYZE`
        — the normalized text is not valid SQL, it can't be re-run.
    """

    source: str            # mcp|ccloud|cloudwatch|sql_probe|webhook
    kind: str               # metric|schema|query_stats|running_query|backup|alert
    fingerprint: str | None
    entity_ids: list[str]   # for recall's entity_affinity term (LLD §6.6) — 0..n entity_id strings
    payload: dict


class RecallBundle(TypedDict):
    """§5.2 recall(node)'s output: "items + scores + citations." `items` are
    `agent/memory/recall.py`'s own hybrid-scored dicts (`item_id`, `class`,
    `content`, `provenance`, `similarity`, `hybrid_score`, ...) — not
    re-typed here; that dict shape already carries everything a citation
    needs (item id, score, source).
    """

    items: list[dict]
    query_text: str
    input_type: str
    latency_ms: float
    hit: bool               # False when items is empty — §5.2 point 5: a normal
                             # outcome (cold start / zero-shot), not an error


class AgentState(TypedDict):
    task_id: str
    scope_id: str
    target_cluster_id: str
    trigger: str                       # eventbridge | webhook | manual
    phase: str                         # observe|recall|reason|gate|act_measure|done|parked
    observations: list[Observation]    # recent, summarized
    incident_fingerprint: str | None   # canonical query-shape / metric signature
    recall_bundle: RecallBundle | None # items + scores + citations
    proposal: dict | None              # TODO: Proposal (pydantic) lands with reason(node)
    approval: dict | None              # TODO: Approval lands with gate(node)
    action: dict | None                # TODO: ActionLedgerRow lands with act_measure(node)
    measurement: dict | None           # TODO: Measurement lands with act_measure(node)
    error: dict | None                 # TODO: TypedError lands with error-handling wiring
    model_meta: dict                   # model_id, version, token usage per call
    replan_count: int                  # LLD §4's gate/act_measure -> reason re-plan edge: how many
                                        # times reason(node) has already been re-entered for this
                                        # incident. Bounded by agent/nodes/gate.py's MAX_REPLANS --
                                        # this is the loop-prevention counter CLAUDE.md's own OPEN
                                        # list named as the reason this edge stayed unwired.
    replan_reason: str | None          # why the previous attempt didn't stick (human rejection or
                                        # a measured regression) -- fed to reason(node) as extra
                                        # context so a re-plan is actually informed, not a blind
                                        # repeat of the same proposal.
    initial_probe: dict | None         # NOT in the LLD's §3 listing — added for agent/graph.py
                                        # (LLD §4): a compiled LangGraph node only ever receives
                                        # `state`, so a sweep's raw ProbeResult (agent/nodes/
                                        # observe.py) has to enter the graph through a state field,
                                        # not a direct function argument. Scratch input, not
                                        # meaningful once observe(node) has consumed it.
