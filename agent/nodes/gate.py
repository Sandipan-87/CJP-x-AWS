"""Engram · agent/nodes/gate.py — ledger txn + approval poll.  [BRAINS]

design/02-low-level-design.md §5.4. Steps 1, 3, 4 — SCOPED, stated up front:

  Step 2 ("SSE push via dashboard feed") is skipped entirely — no
  dashboard/SSE surface exists yet (that's [ILLUSIONIST]'s Next.js work).
  A human approves by writing directly to `approvals` for now (the same
  row this node polls); nothing about that path needs the SSE push to be
  correct, only to be visible sooner.

  Step 5 (telemetry: `gate_wait_ms`, `blocked_by_backup_gate`) is skipped —
  no telemetry sink exists. `blocked_by_backup_gate` is actually
  `act_measure(node)`'s own metric (LLD §5.5 step 1), not gate's — named
  in gate's metrics list but computed one node later, once that node exists.

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
from agent.tools import recipe_renderer
from agent.tools.sql_probe import SqlProbe

DEFAULT_POLL_INTERVAL_S = 2.0        # LLD §5.4 step 3
DEFAULT_TIMEOUT_S = 600.0            # LLD §2 config contract: ENGRAM_APPROVAL_TIMEOUT_S default
RECIPE_VERSION = "v1"


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

    approval = await _poll_until_decided(db, approval_id, poll_interval_s, timeout_s)

    if approval["status"] == "approved":
        return {
            "approval": approval,
            "action": {"action_id": action_id, "status": "approved", "rendered_sql": rendered.sql},
            "phase": "gate",
        }

    # rejected / expired -> outcome row (skipped) + episode memory + done (LLD §5.4 step 4)
    await db.update_remediation_status(action_id, "skipped", outcome="skipped")
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
