"""Engram · agent/nodes/gate.py — ledger txn + approval poll.  [BRAINS]

design/02-low-level-design.md §5.4. Steps 1, 3, 4 — SCOPED, stated up front:

  Step 2 ("SSE push via dashboard feed") is skipped entirely — no
  dashboard/SSE surface exists yet (that's [ILLUSIONIST]'s Next.js work).
  A human approves by writing directly to `approvals` for now (the same
  row this node polls); nothing about that path needs the SSE push to be
  correct, only to be visible sooner.

  Step 5 (telemetry: `gate_wait_ms`, `blocked_by_backup_gate`) is now wired
  via the additive `telemetry: Telemetry | None = None` param (`agent/
  telemetry.py`) — `None` (unpassed) is identical to prior behavior.
  `gate_wait_ms` isn't in LLD §12's own dashboard table, so it's a span
  attribute only, not a CloudWatch metric (see that module's docstring).
  `blocked_by_backup_gate` is still not this node's own metric — it's
  `act_measure(node)`'s (LLD §5.5 step 1), named in gate's step-5 list but
  computed one node later, once that node's own backup-gate check runs.

  Step 1's "ONE txn" is `db.insert_gate_decision` — see that method's own
  docstring for why it checks the idempotency key BEFORE inserting, not
  by catching a UniqueViolation like every other composite write in this
  repo so far.

  The rendered SQL's schema cross-check (`recipe_renderer`'s step 2) is
  OPTIONAL here — pass `sql_probe` to actually run it against the target
  cluster's real `information_schema`; omit it and the render still
  succeeds, just with `schema_checked=False` (recorded, not hidden).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

from agent.memory.db import Database
from agent.state import AgentState
from agent.telemetry import Telemetry, elapsed_ms, maybe_span, set_attr
from agent.tools import recipe_renderer
from agent.tools.sql_probe import SqlProbe

DEFAULT_POLL_INTERVAL_S = 2.0        # LLD §5.4 step 3
DEFAULT_TIMEOUT_S = 600.0            # LLD §2 config contract: ENGRAM_APPROVAL_TIMEOUT_S default
RECIPE_VERSION = "v1"
MAX_REPLANS = 2                      # LLD §4's gate -> reason re-plan edge, loop-prevention bound.
                                      # Small on purpose, matching reason(node)'s own MAX_ROUNDS
                                      # convention for "try again with feedback" -- not unlimited.
                                      # Allows up to 3 total proposals per incident (1 initial + 2
                                      # re-plans); a third straight rejection parks it rather than
                                      # asking a fourth time.


def _compute_idempotency_key(target_cluster_id: str, proposal: dict) -> str:
    """LLD §6.2: `idempotency_key = sha256(cluster_id ‖ canonical_change)`.
    "Canonical" here means `action_kind` + `parameters`, JSON-serialized
    with sorted keys so the same logical change always hashes identically
    regardless of dict insertion order.
    """
    canonical = json.dumps(
        {"action_kind": proposal["action_kind"], "parameters": proposal["parameters"]},
        sort_keys=True,
    )
    return hashlib.sha256(f"{target_cluster_id}|{canonical}".encode("utf-8")).hexdigest()


async def gate(
    state: AgentState,
    db: Database,
    *,
    sql_probe: SqlProbe | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    telemetry: Telemetry | None = None,
) -> dict:
    """Returns a partial `AgentState` update. On approval: `phase='gate'`
    (ready for `act_measure`, not yet written). On reject/expiry: LLD §5.4
    step 4's "outcome row (skipped) + episode memory + done" —
    `phase='done'`.

    Raises `ValueError` if `state["proposal"]` is missing — `gate(node)`
    has nothing to gate without one; this is a caller-ordering bug, not a
    recoverable runtime condition.
    """
    proposal = state.get("proposal")
    if proposal is None:
        raise ValueError("gate(node) requires state['proposal'] -- reason(node) must run first")

    with maybe_span(telemetry, "gate", task_id=state["task_id"], scope_id=state["scope_id"]) as span:
        known_columns = None
        if sql_probe is not None:
            known_columns = await sql_probe.get_table_columns(proposal["parameters"]["table"])
        rendered = recipe_renderer.render(
            proposal["action_kind"], proposal["parameters"], known_columns=known_columns
        )

        idempotency_key = _compute_idempotency_key(state["target_cluster_id"], proposal)
        _decision_id, action_id, approval_id = await db.insert_gate_decision(
            state["task_id"], state["scope_id"], state["target_cluster_id"],
            model_id=os.environ.get("ENGRAM_LLM_MODEL", "minimax-m3:cloud"),
            reasoning=proposal,
            citations=proposal.get("citations"),
            action_kind=proposal["action_kind"],
            recipe_version=RECIPE_VERSION,
            parameters=proposal["parameters"],
            rendered_sql=rendered.sql,
            idempotency_key=idempotency_key,
        )

        wait_t0 = time.perf_counter()
        approval = await _poll_until_decided(db, approval_id, poll_interval_s, timeout_s)
        # gate_wait_ms isn't in LLD §12's dashboard table -- span attribute only, see module docstring.
        set_attr(span, "gate_wait_ms", elapsed_ms(wait_t0))
        set_attr(span, "outcome", approval["status"])

        if approval["status"] == "approved":
            return {
                "approval": approval,
                "action": {"action_id": action_id, "status": "approved", "rendered_sql": rendered.sql},
                "phase": "gate",
            }

        # rejected / expired -> this action is skipped either way, LLD §5.4 step 4
        await db.update_remediation_status(action_id, "skipped", outcome="skipped")

        # LLD §4's gate -> reason re-plan edge, now actually wired: a HUMAN REJECTION (not an
        # expiry -- nobody was watching, so automatically retrying would just time out again for
        # nothing) with re-plan budget left goes back to reason(node) for a genuinely different
        # proposal instead of ending the incident here. `proposal` is cleared so act_measure/gate
        # never see a stale one if something upstream forgets to check `phase` -- reason(node) is
        # about to set a fresh one anyway.
        replan_count = state.get("replan_count", 0)
        if approval["status"] == "rejected" and replan_count < MAX_REPLANS:
            replan_reason = f"a human reviewer rejected the proposed {proposal['action_kind']}"
            if approval.get("comment"):
                replan_reason += f" ({approval['comment']})"
            return {
                "approval": approval,
                "action": {"action_id": action_id, "status": "skipped", "rendered_sql": rendered.sql},
                "proposal": None,
                "replan_count": replan_count + 1,
                "replan_reason": replan_reason,
                "phase": "replan",
            }

        # Terminal: expired, or rejected with no re-plan budget left -- outcome row already
        # written above; record the episode now, since this really is the end of the road for
        # this incident (LLD §5.4 step 4's own "episode memory + done").
        await db.insert_memory_item(
            state["scope_id"],
            "episode",
            f"Remediation for {proposal['action_kind']} on "
            f"{proposal['parameters'].get('table', 'unknown')} was {approval['status']} at the gate.",
            provenance={"task_id": state["task_id"], "action_id": action_id, "approval_id": approval_id},
        )  # embedding=None -- seed-then-backfill (LLD §6.3 step 4), same as observe(node)

        return {
            "approval": approval,
            "action": {"action_id": action_id, "status": "skipped", "rendered_sql": rendered.sql},
            "phase": "done",
        }


async def _poll_until_decided(
    db: Database, approval_id: str, poll_interval_s: float, timeout_s: float
) -> dict:
    """LLD §5.4 step 3: poll every `poll_interval_s` up to `timeout_s`. A
    still-`pending` approval at the deadline is marked `expired` here —
    something has to close it out, and gate is the thing enforcing this
    particular deadline; a lifecycle worker (LLD §9, not yet written)
    would be the real long-term sweeper for approvals whose owning process
    died before its own deadline.
    """
    deadline = time.monotonic() + timeout_s
    approval = await db.poll_approval(approval_id)
    while approval and approval["status"] == "pending" and time.monotonic() < deadline:
        await asyncio.sleep(poll_interval_s)
        approval = await db.poll_approval(approval_id)

    if approval and approval["status"] == "pending":
        await db.decide_approval(approval_id, "system:gate_timeout", "expired")
        approval = await db.poll_approval(approval_id)

    return approval
