"""Engram · agent/nodes/act_measure.py — ledger-first apply + measure.  [BRAINS + PLUMBER]

design/02-low-level-design.md §5.5, ADR-004 full detail (§8). Steps 2–6 —
SCOPED, stated up front:

  Step 1 (backup gate) is fully implemented, but its LIVE network leg is
  unverified — see `agent/tools/cloud_api.py`'s module docstring for exactly
  what's measured (the real empty-list case) vs. assumed (a non-empty
  response's shape). Pass `override_backup_gate=True` to skip it — recorded
  in `decisions`, auditable, never silent; this is LLD's own named escape
  hatch, not a workaround of it.

  Step 6 names updating `procedures` stats and `approvals(decided_*)` —
  neither is implemented here: `procedures` rows don't exist until the
  not-yet-written `consolidator` worker (LLD §9) creates them, so there's
  no established link from a fresh `Proposal` to an existing `procedure_id`
  to update; and `approvals` was already decided by `gate(node)` before
  this node ever runs.

  §8.4's crash-window reconciliation (W1–W4) is NOT implemented — a real,
  meaningful gap, not hidden: if this process dies between the ledger txn
  and the outcome txn, nothing here probes the target to check whether the
  DDL actually landed before deciding whether to re-apply it. That's its
  own focused piece of work, deferred rather than half-built.

  "Success" is defined here as "measured latency went down" — the direct,
  measurable signal this whole demo beat is about. A more nuanced
  definition (e.g. requiring the full scan to disappear too) was considered
  and set aside as unnecessary complexity for what latency already answers
  unambiguously.

  Telemetry (§5.5 step 7: `time_to_remediation`, `exactly_once_conflicts_
  detected`) is now wired via the additive `telemetry: Telemetry | None =
  None` param (`agent/telemetry.py`) — `None` (unpassed) is identical to
  prior behavior. `time_to_remediation` here is THIS function's own elapsed
  wall time (backup-gate check through the outcome txn), a stated
  simplification of the LLD's fuller "observe -> remediated" span — no
  timestamp for when the incident was first observed is threaded through
  `AgentState` yet (`tasks.created_at` exists in the DB but this node never
  reads it back). `exactly_once_conflicts_detected` is NOT emitted — §8.4's
  crash-window reconciliation (W1-W4), the only place that metric would ever
  increment, is still explicitly unimplemented (see above); emitting a
  metric no code path can ever produce would be worse than the honest gap.
  `blocked_by_backup_gate` (LLD §5.5 step 1's own metric, misfiled under
  gate's step-5 list — see `gate.py`'s docstring) fires here when the gate
  actually refuses.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from agent.errors import BackupGateBlocked
from agent.memory.db import Database
from agent.state import AgentState, Observation
from agent.telemetry import Telemetry, elapsed_ms, maybe_record, maybe_span, set_attr
from agent.tools.cloud_api import CloudApiAdapter
from agent.tools.sql_operator import SqlOperator
from agent.tools.sql_probe import ExplainResult, SqlProbe

DEFAULT_BACKUP_WINDOW_HOURS = 24.0


def _extract_raw_query(observations: list[Observation]) -> str | None:
    """`payload["raw_text"]` (not `payload["text"]`, which is normalized and
    not valid SQL — see `agent/state.py`'s `Observation` docstring). Reads
    from the most recent observation backwards, same convention as
    `agent/nodes/reason.py`'s single-observation reads.
    """
    for obs in reversed(observations):
        raw = (obs.get("payload") or {}).get("raw_text")
        if raw:
            return raw
    return None


def _trim_explain(result: ExplainResult) -> dict:
    """Only the compact, structured fields — not the raw plan text. Keeps
    the ledger row small; invariant #11 is for large artifacts, and a full
    EXPLAIN plan is exactly the kind of thing that principle argues against
    inlining into a JSONB column by default.
    """
    return {
        "latency_ms": result.latency_ms,
        "has_full_scan": result.has_full_scan,
        "index_candidate": result.index_candidate,
    }


async def act_measure(
    state: AgentState,
    db: Database,
    sql_probe: SqlProbe,
    sql_operator: SqlOperator,
    *,
    backup_gate: CloudApiAdapter | None = None,
    backup_window_hours: float = DEFAULT_BACKUP_WINDOW_HOURS,
    override_backup_gate: bool = False,
    telemetry: Telemetry | None = None,
) -> dict:
    """Returns a partial `AgentState` update: `measurement`, `action`
    (status now `applied`, `outcome` set), `phase='done'`.

    Raises `BackupGateBlocked` (LLD §16: park + alert, auto-retry next
    sweep) if the gate refuses and no override was given. Raises
    `ValueError` if `state["action"]` isn't an approved action, or if no
    runnable query text is available to measure — both caller-ordering
    conditions, not recoverable runtime states.
    """
    action = state.get("action")
    if not action or action.get("status") != "approved":
        raise ValueError(
            "act_measure(node) requires an approved state['action'] -- gate(node) must run first"
        )

    t0 = time.perf_counter()
    with maybe_span(telemetry, "act_measure", task_id=state["task_id"], scope_id=state["scope_id"]) as span:
        if override_backup_gate:
            await db.insert_decision(
                state["task_id"], state["scope_id"], "act",
                model_id="human-override",
                reasoning={"override_backup_gate": True},
            )
        else:
            if backup_gate is None:
                await maybe_record(
                    telemetry, "blocked_by_backup_gate", 1.0, dimensions={"scope_id": state["scope_id"]}
                )
                set_attr(span, "outcome", "blocked_by_backup_gate")
                raise BackupGateBlocked("no backup-gate adapter configured -- safe default is refuse")
            proceed, reason = await backup_gate.check_backup_gate(
                state["target_cluster_id"], window_hours=backup_window_hours
            )
            if not proceed:
                await maybe_record(
                    telemetry, "blocked_by_backup_gate", 1.0, dimensions={"scope_id": state["scope_id"]}
                )
                set_attr(span, "outcome", "blocked_by_backup_gate")
                raise BackupGateBlocked(reason)

        query_text = _extract_raw_query(state.get("observations") or [])
        if not query_text:
            raise ValueError(
                "act_measure(node) requires observations[].payload['raw_text'] to measure"
            )

        measured_before = await sql_probe.explain_analyze(query_text)
        await db.insert_act_decision(
            state["task_id"], state["scope_id"], action["action_id"],
            model_id=os.environ.get("ENGRAM_LLM_MODEL", "minimax-m3:cloud"),
            reasoning={"action_id": action["action_id"], "query": query_text[:200]},
            measured_before=_trim_explain(measured_before),
        )

        await sql_operator.apply(action["rendered_sql"])

        measured_after = await sql_probe.explain_analyze(query_text)
        outcome = "success" if measured_after.latency_ms < measured_before.latency_ms else "failure"

        proposal = state.get("proposal") or {}
        episode_content = (
            f"Applied {proposal.get('action_kind', 'remediation')} on "
            f"{(proposal.get('parameters') or {}).get('table', 'unknown')}: "
            f"latency {measured_before.latency_ms:.1f}ms -> {measured_after.latency_ms:.1f}ms ({outcome})."
        )
        await db.insert_outcome_decision(
            action["action_id"], state["scope_id"],
            outcome=outcome,
            measured_after=_trim_explain(measured_after),
            applied_at=datetime.now(timezone.utc),
            episode_content=episode_content,
            episode_provenance={"task_id": state["task_id"], "action_id": action["action_id"]},
        )

        set_attr(span, "outcome", outcome)
        set_attr(span, "latency_ms", elapsed_ms(t0))

    # time_to_remediation is Seconds per LLD §12 -- see module docstring on scope.
    await maybe_record(
        telemetry, "time_to_remediation", elapsed_ms(t0) / 1000,
        dimensions={"scope_id": state["scope_id"], "task_id": state["task_id"]},
    )

    return {
        "measurement": {
            "measured_before": _trim_explain(measured_before),
            "measured_after": _trim_explain(measured_after),
            "outcome": outcome,
        },
        "action": {**action, "status": "applied", "outcome": outcome},
        "phase": "done",
    }
