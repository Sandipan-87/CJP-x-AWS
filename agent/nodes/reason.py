"""Engram · agent/nodes/reason.py — hypothesis + falsification → Proposal.  [BRAINS]

design/02-low-level-design.md §5.3. Steps 1, 2, 4, 5 — SCOPED, stated up front:

  Step 3's falsification loop, as written, assumes a live `explain_query`
  MCP tool the model can call mid-conversation ("run EXPLAIN on the
  original slow SELECT... via MCP `explain_query`"). MCP doesn't exist yet
  (`agent/tools/mcp_tool.py`). Instead: `SqlProbe` already measured the
  optimizer's real index recommendation for THIS incident, back in
  `observe(node)`, and that measurement is already sitting in
  `observations[-1].payload["index_candidate"]`. Falsification here is a
  DETERMINISTIC comparison, computed in Python, between the LLM's proposed
  `(table, columns)` and that already-real recommendation — never asked of
  the model itself (the model grading its own falsification evidence would
  defeat the point of it being an external check). `Evidence`/`Citation`
  are populated by THIS module, never emitted by the LLM
  (`agent/schemas.py`'s docstring says the same).

  LLD names TWO separate bounds: "rounds < 3" for falsification, "1 repair
  turn" for schema failure. Both are merged into ONE round counter here
  (`max_rounds`, default 3) — a stated simplification, not a silent
  blending: a schema failure and a falsification mismatch are both "the
  model needs to try again with feedback," and tracking them as two
  independent counters buys nothing a reviewer couldn't get from reading
  `last_error` in the persisted decision row anyway.

  `<mm:think>` stripping happens inside `OllamaCloudLLM.complete()` itself
  (defense-in-depth, not primary-path — see that module's docstring);
  audit rationale is `Proposal.reasoning`, validated below.

  Telemetry (§5.3 step 6: `llm_latency_ms`, `llm_token_usage`, `llm_failures`)
  is now wired via the additive `telemetry: Telemetry | None = None` param
  (`agent/telemetry.py`) — `None` (unpassed) is identical to prior behavior.
  All three are per-`llm.complete()`-call, not per-node-call: a round that
  gets rejected (bad schema, falsification mismatch) still made a real LLM
  call with real latency/tokens, and `llm_failures` fires once per rejected
  round, not just on final exhaustion. `llm_token_usage` sums whatever
  numeric fields `LLMResult.usage` actually carries (provider-specific,
  "may be empty" per that field's own docstring) — `OllamaCloudLLM` today
  supplies `eval_count`/`prompt_eval_count`.
"""

from __future__ import annotations

import os
import time

from pydantic import ValidationError

from agent.errors import LLMSchemaError
from agent.memory.db import Database
from agent.providers.base import LLMProvider
from agent.schemas import Citation, Evidence, Proposal
from agent.state import AgentState, Observation, RecallBundle
from agent.telemetry import Telemetry, elapsed_ms, maybe_record, maybe_span, set_attr

MAX_ROUNDS = 3  # LLD §5.3 step 3's "rounds < 3" — see module docstring on merging bounds

SYSTEM_PROMPT = (
    "You are a CockroachDB reliability engineer. You will be shown a real, "
    "measured incident (a slow query, its plan, and the optimizer's own index "
    "recommendation if one exists) plus any similar past incidents recalled "
    "from memory. Call propose_remediation exactly once with your single best "
    "remediation. Only create_index or analyze_table are permitted action kinds. "
    "The 'reasoning' field is the only place your rationale is recorded for "
    "audit — write it as if someone will read it later to understand why."
)

# Note: this tool's own top-level "parameters" (the JSON-schema keyword for the
# tool's arguments) is a different thing from the "parameters" PROPERTY inside
# it (Proposal.parameters, e.g. {"table": ..., "columns": [...]}) — same name,
# two different levels, not a typo.
PROPOSE_REMEDIATION_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_remediation",
        "description": "Propose exactly one CockroachDB remediation for the observed incident.",
        "parameters": {
            "type": "object",
            "properties": {
                "reasoning": {"type": "string", "description": "Audit-grade rationale."},
                "hypothesis": {"type": "string", "description": "What is causing the slowdown."},
                "action_kind": {"type": "string", "enum": ["create_index", "analyze_table"]},
                "parameters": {
                    "type": "object",
                    "properties": {
                        "table": {"type": "string"},
                        "columns": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["table", "columns"],
                },
                "expected_effect": {"type": "string"},
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "confidence": {"type": "number", "description": "0.0 to 1.0"},
            },
            "required": [
                "reasoning", "hypothesis", "action_kind", "parameters",
                "expected_effect", "risk", "confidence",
            ],
        },
    },
}


def _compute_falsification(observation: Observation | None, proposed_columns: list[str]) -> Evidence:
    """LLD §5.3 step 3's pre-gate check, computed deterministically — see
    module docstring for why this isn't a live MCP tool round-trip.
    """
    index_candidate = (observation or {}).get("payload", {}).get("index_candidate")
    if not index_candidate:
        return Evidence(
            result_summary="no optimizer index recommendation was available to compare against",
            index_recommendation_match=None,
        )
    candidate_cols = {c.strip() for c in index_candidate.split(",") if c.strip()}
    proposed_cols = {c.strip() for c in proposed_columns}
    match = bool(candidate_cols & proposed_cols)
    return Evidence(
        result_summary=(
            f"optimizer recommended columns {sorted(candidate_cols)}; "
            f"proposal targets {sorted(proposed_cols)} -> {'match' if match else 'mismatch'}"
        ),
        index_recommendation_match=match,
    )


def _citations_from_bundle(bundle: RecallBundle | None) -> list[Citation]:
    """Populated from `AgentState.recall_bundle`, never invented by the LLM."""
    if not bundle:
        return []
    return [
        Citation(
            memory_item_id=str(item["item_id"]),
            score=item.get("hybrid_score", item.get("similarity", 0.0)),
            source=item.get("class", "unknown"),
        )
        for item in bundle.get("items", [])
    ]


def _build_context_message(state: AgentState) -> str:
    observation = state["observations"][-1] if state.get("observations") else None
    payload = (observation or {}).get("payload", {})
    bundle = state.get("recall_bundle")
    lines = [
        f"Query involved: {payload.get('text', 'unknown')!r}",
        f"Measured latency: {payload.get('latency_ms')} ms",
        f"Plan shows a full/sequential scan: {payload.get('plan_has_seq_scan')}",
        f"Optimizer's own index recommendation: {payload.get('index_candidate') or 'none'}",
    ]
    if bundle and bundle.get("items"):
        lines.append("Similar past incidents/procedures recalled from memory:")
        for item in bundle["items"][:3]:
            score = item.get("hybrid_score", item.get("similarity", 0.0))
            lines.append(f"  - {item.get('class')}: {str(item.get('content'))[:150]!r} (score={score:.3f})")
    else:
        lines.append("No relevant memory recalled — proceed zero-shot (LLD §5.2 point 5).")
    return "\n".join(lines)


async def reason(
    state: AgentState,
    db: Database,
    llm: LLMProvider,
    *,
    max_rounds: int = MAX_ROUNDS,
    telemetry: Telemetry | None = None,
) -> dict:
    """Returns a partial `AgentState` update: `{"proposal": ..., "phase": "reason"}`.

    Raises `LLMSchemaError` (LLD §16: park, human can retry) if no valid,
    falsification-consistent proposal emerges within `max_rounds` — this
    function does not silently guess at a malformed or contradicted proposal.
    """
    model_id = os.environ.get("ENGRAM_LLM_MODEL", "minimax-m3:cloud")
    observation = state["observations"][-1] if state.get("observations") else None
    context = _build_context_message(state)
    messages: list[dict] = [{"role": "user", "content": context}]
    if state.get("replan_reason"):
        # LLD §4's gate/act_measure -> reason re-plan edge: a rejected or measured-regression
        # proposal already tried once, and this is a genuinely different graph-level re-entry,
        # not one of this function's own intra-call repair rounds below -- without this, reason
        # (node) would have no way to know its FIRST idea already failed and could just propose
        # the identical thing again.
        messages.append({
            "role": "user",
            "content": f"A previous remediation attempt for this incident did not succeed: "
                       f"{state['replan_reason']}. Propose a DIFFERENT remediation this time.",
        })
    citations = _citations_from_bundle(state.get("recall_bundle"))

    proposal: Proposal | None = None
    last_error: str | None = None
    rounds_used = 0

    with maybe_span(telemetry, "reason", task_id=state.get("task_id"), scope_id=state["scope_id"], model_id=model_id) as span:
        for _round in range(1, max_rounds + 1):
            rounds_used = _round
            if last_error:
                messages.append({
                    "role": "user",
                    "content": f"Your previous proposal was rejected: {last_error}. "
                                f"Revise and call propose_remediation again.",
                })

            call_t0 = time.perf_counter()
            result = await llm.complete(SYSTEM_PROMPT, messages, [PROPOSE_REMEDIATION_TOOL])
            call_latency_ms = elapsed_ms(call_t0)
            await maybe_record(telemetry, "llm_latency_ms", call_latency_ms, dimensions={"model_id": model_id})
            token_usage = sum(v for v in result.usage.values() if isinstance(v, (int, float)))
            if token_usage:
                await maybe_record(telemetry, "llm_token_usage", token_usage, dimensions={"model_id": model_id})

            if not result.tool_calls:
                last_error = f"no tool call returned; model answered in prose: {result.text[:200]!r}"
                await maybe_record(telemetry, "llm_failures", 1.0, dimensions={"model_id": model_id})
                continue

            args = result.tool_calls[0].get("arguments") or {}
            proposed_columns = (args.get("parameters") or {}).get("columns", [])
            evidence = _compute_falsification(observation, proposed_columns)

            candidate = {
                **args,
                "falsification": [evidence.model_dump()],
                "citations": [c.model_dump() for c in citations],
            }
            try:
                validated = Proposal.model_validate(candidate)
            except ValidationError as exc:
                last_error = f"schema validation failed: {exc}"
                await maybe_record(telemetry, "llm_failures", 1.0, dimensions={"model_id": model_id})
                continue

            if evidence.index_recommendation_match is False:
                last_error = evidence.result_summary
                await maybe_record(telemetry, "llm_failures", 1.0, dimensions={"model_id": model_id})
                continue

            proposal = validated
            break

        set_attr(span, "rounds_used", rounds_used)
        set_attr(span, "outcome", "success" if proposal is not None else "failure")

        if proposal is None:
            raise LLMSchemaError(last_error or "exhausted rounds with no valid proposal")

        await db.insert_decision(
            state["task_id"],
            state["scope_id"],
            "reason",
            model_id=model_id,
            reasoning=proposal.model_dump(mode="json"),
            citations=[c.model_dump() for c in proposal.citations],
        )

    return {"proposal": proposal.model_dump(mode="json"), "phase": "reason"}
