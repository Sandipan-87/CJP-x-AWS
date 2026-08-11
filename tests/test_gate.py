"""Engram · unit tests for agent/nodes/gate.py -- pure helper + control flow
via a scripted fake Database (approve/reject/expire), no real cluster/timers.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.nodes.gate import _compute_idempotency_key, gate


# ---------------------------------------------------------------- pure helper

def test_idempotency_key_is_deterministic():
    proposal = {"action_kind": "create_index", "parameters": {"table": "orders", "columns": ["customer_id"]}}
    assert _compute_idempotency_key("c1", proposal) == _compute_idempotency_key("c1", proposal)


def test_idempotency_key_independent_of_dict_key_order():
    p1 = {"action_kind": "create_index", "parameters": {"table": "orders", "columns": ["customer_id"]}}
    p2 = {"action_kind": "create_index", "parameters": {"columns": ["customer_id"], "table": "orders"}}
    assert _compute_idempotency_key("c1", p1) == _compute_idempotency_key("c1", p2)


def test_idempotency_key_differs_by_cluster():
    proposal = {"action_kind": "create_index", "parameters": {"table": "orders", "columns": ["customer_id"]}}
    assert _compute_idempotency_key("c1", proposal) != _compute_idempotency_key("c2", proposal)


def test_idempotency_key_differs_by_parameters():
    base = {"action_kind": "create_index", "parameters": {"table": "orders", "columns": ["customer_id"]}}
    other = {"action_kind": "create_index", "parameters": {"table": "orders", "columns": ["region"]}}
    assert _compute_idempotency_key("c1", base) != _compute_idempotency_key("c1", other)


# ---------------------------------------------------------------- control flow

class _FakeDb:
    def __init__(self, approval_statuses: list[str]) -> None:
        self._statuses = list(approval_statuses)
        self.action_id = "fake-action-id"
        self.approval_id = "fake-approval-id"
        self.gate_decisions: list[dict] = []
        self.remediation_updates: list[tuple] = []
        self.memory_items: list[tuple] = []
        self.decide_calls: list[tuple] = []

    async def insert_gate_decision(self, task_id, scope_id, target_cluster_id, **kwargs):
        self.gate_decisions.append(kwargs)
        return "fake-decision-id", self.action_id, self.approval_id

    async def poll_approval(self, approval_id):
        status = self._statuses.pop(0) if self._statuses else "pending"
        return {"approval_id": approval_id, "status": status}

    async def decide_approval(self, approval_id, decided_by, status, comment=None):
        self.decide_calls.append((approval_id, decided_by, status))
        return True

    async def update_remediation_status(self, action_id, status, **kwargs):
        self.remediation_updates.append((action_id, status, kwargs))

    async def insert_memory_item(self, scope_id, item_class, content, **kwargs):
        self.memory_items.append((scope_id, item_class, content, kwargs))
        return "fake-item-id"


def _base_state():
    return {
        "task_id": "t1", "scope_id": "s1", "target_cluster_id": "tc1",
        "proposal": {
            "action_kind": "create_index",
            "parameters": {"table": "orders", "columns": ["customer_id"]},
            "citations": [],
        },
    }


def test_gate_raises_value_error_without_a_proposal():
    db = _FakeDb([])
    state = _base_state()
    state["proposal"] = None

    async def run():
        await gate(state, db)

    with pytest.raises(ValueError, match="proposal"):
        asyncio.run(run())


def test_gate_approved_immediately():
    db = _FakeDb(["approved"])

    async def run():
        return await gate(_base_state(), db, poll_interval_s=0.01, timeout_s=5.0)

    update = asyncio.run(run())
    assert update["phase"] == "gate"
    assert update["approval"]["status"] == "approved"
    assert update["action"]["status"] == "approved"
    assert db.remediation_updates == []  # only touched on reject/expire
    assert db.memory_items == []


def test_gate_approved_after_a_few_pending_polls():
    db = _FakeDb(["pending", "pending", "approved"])

    async def run():
        return await gate(_base_state(), db, poll_interval_s=0.01, timeout_s=5.0)

    update = asyncio.run(run())
    assert update["phase"] == "gate"
    assert update["approval"]["status"] == "approved"


def test_gate_rejected_writes_outcome_and_episode_then_done():
    db = _FakeDb(["rejected"])

    async def run():
        return await gate(_base_state(), db, poll_interval_s=0.01, timeout_s=5.0)

    update = asyncio.run(run())
    assert update["phase"] == "done"
    assert update["action"]["status"] == "skipped"
    assert len(db.remediation_updates) == 1
    assert db.remediation_updates[0][1] == "skipped"
    assert len(db.memory_items) == 1
    assert db.memory_items[0][1] == "episode"
    assert db.decide_calls == []  # rejected by a real decision, not a timeout


def test_gate_expires_after_timeout_and_marks_it_itself():
    """No status ever arrives -- gate must give up at the deadline and mark
    the approval expired itself (LLD §5.4 step 4)."""
    db = _FakeDb([])  # always returns "pending"

    async def run():
        return await gate(_base_state(), db, poll_interval_s=0.02, timeout_s=0.05)

    update = asyncio.run(run())
    assert update["phase"] == "done"
    assert len(db.decide_calls) == 1
    assert db.decide_calls[0][2] == "expired"
    assert len(db.remediation_updates) == 1
    assert len(db.memory_items) == 1


def test_gate_persists_rendered_sql_in_the_ledger_write():
    db = _FakeDb(["approved"])

    async def run():
        return await gate(_base_state(), db, poll_interval_s=0.01, timeout_s=5.0)

    asyncio.run(run())
    assert len(db.gate_decisions) == 1
    assert "CREATE INDEX IF NOT EXISTS" in db.gate_decisions[0]["rendered_sql"]
