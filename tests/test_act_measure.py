"""Engram · unit tests for agent/nodes/act_measure.py -- control flow via
scripted fake SqlProbe/SqlOperator/CloudApiAdapter/Database. No real
cluster/network needed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.errors import BackupGateBlocked
from agent.nodes.act_measure import _extract_raw_query, _trim_explain, act_measure
from agent.tools.sql_probe import ExplainResult


def _explain(latency_ms: float, has_full_scan: bool = False) -> ExplainResult:
    return ExplainResult(latency_ms=latency_ms, has_full_scan=has_full_scan,
                          index_candidate=None, raw_analyze_plan="...", raw_explain_plan="...")


# ------------------------------------------------------------------ helpers

def test_extract_raw_query_finds_the_most_recent():
    obs = [
        {"payload": {"raw_text": "SELECT 1"}},
        {"payload": {"raw_text": "SELECT 2"}},
    ]
    assert _extract_raw_query(obs) == "SELECT 2"


def test_extract_raw_query_none_when_absent():
    assert _extract_raw_query([{"payload": {"text": "normalized only"}}]) is None
    assert _extract_raw_query([]) is None


def test_trim_explain_drops_raw_plan_text():
    result = _explain(100.0)
    trimmed = _trim_explain(result)
    assert "raw_analyze_plan" not in trimmed
    assert "raw_explain_plan" not in trimmed
    assert trimmed["latency_ms"] == 100.0


# -------------------------------------------------------------- fakes + flow

class _FakeSqlProbe:
    def __init__(self, latencies: list[float]) -> None:
        self._latencies = list(latencies)
        self.calls: list[str] = []

    async def explain_analyze(self, sql: str) -> ExplainResult:
        self.calls.append(sql)
        return _explain(self._latencies.pop(0))


class _FakeSqlOperator:
    def __init__(self) -> None:
        self.applied: list[str] = []

    async def apply(self, rendered_sql: str) -> None:
        self.applied.append(rendered_sql)


class _FakeBackupGate:
    def __init__(self, proceed: bool, reason: str = "fake reason") -> None:
        self._proceed = proceed
        self._reason = reason
        self.calls = 0

    async def check_backup_gate(self, cluster_id: str, *, window_hours: float = 24.0):
        self.calls += 1
        return self._proceed, self._reason


class _FakeDb:
    def __init__(self) -> None:
        self.act_decisions: list[dict] = []
        self.outcome_decisions: list[dict] = []
        self.plain_decisions: list[tuple] = []

    async def insert_act_decision(self, task_id, scope_id, action_id, **kwargs):
        self.act_decisions.append(kwargs)
        return "fake-act-decision-id"

    async def insert_outcome_decision(self, action_id, scope_id, **kwargs):
        self.outcome_decisions.append(kwargs)
        return "fake-item-id"

    async def insert_decision(self, task_id, scope_id, node, **kwargs):
        self.plain_decisions.append((node, kwargs))
        return "fake-decision-id"


def _base_state(**overrides) -> dict:
    state = {
        "task_id": "t1", "scope_id": "s1", "target_cluster_id": "tc1",
        "observations": [{"payload": {"raw_text": "SELECT * FROM orders WHERE customer_id = 42"}}],
        "action": {"action_id": "a1", "status": "approved",
                    "rendered_sql": "CREATE INDEX IF NOT EXISTS orders_idx ON public.orders (customer_id)"},
        "proposal": {"action_kind": "create_index", "parameters": {"table": "orders"}},
    }
    state.update(overrides)
    return state


def test_raises_without_an_approved_action():
    db, probe, op = _FakeDb(), _FakeSqlProbe([]), _FakeSqlOperator()
    state = _base_state(action=None)

    async def run():
        await act_measure(state, db, probe, op, backup_gate=_FakeBackupGate(True))

    with pytest.raises(ValueError, match="approved"):
        asyncio.run(run())


def test_raises_without_raw_query_text():
    db, probe, op = _FakeDb(), _FakeSqlProbe([]), _FakeSqlOperator()
    state = _base_state(observations=[{"payload": {"text": "normalized"}}])

    async def run():
        await act_measure(state, db, probe, op, backup_gate=_FakeBackupGate(True))

    with pytest.raises(ValueError, match="raw_text"):
        asyncio.run(run())


def test_backup_gate_blocked_with_no_adapter_configured():
    db, probe, op = _FakeDb(), _FakeSqlProbe([]), _FakeSqlOperator()

    async def run():
        await act_measure(_base_state(), db, probe, op)  # no backup_gate at all

    with pytest.raises(BackupGateBlocked, match="no backup-gate adapter"):
        asyncio.run(run())


def test_backup_gate_blocked_when_adapter_refuses():
    db, probe, op = _FakeDb(), _FakeSqlProbe([]), _FakeSqlOperator()
    gate = _FakeBackupGate(False, "no backups exist yet")

    async def run():
        await act_measure(_base_state(), db, probe, op, backup_gate=gate)

    with pytest.raises(BackupGateBlocked, match="no backups exist"):
        asyncio.run(run())
    assert gate.calls == 1
    assert probe.calls == []  # never got past the gate to measure anything
    assert op.applied == []


def test_override_skips_the_gate_and_records_an_auditable_decision():
    db = _FakeDb()
    probe = _FakeSqlProbe([100.0, 10.0])
    op = _FakeSqlOperator()

    async def run():
        return await act_measure(_base_state(), db, probe, op, override_backup_gate=True)

    update = asyncio.run(run())
    assert update["measurement"]["outcome"] == "success"
    assert len(db.plain_decisions) == 1
    assert db.plain_decisions[0][0] == "act"
    assert db.plain_decisions[0][1]["reasoning"]["override_backup_gate"] is True


def test_success_when_latency_improves():
    db = _FakeDb()
    probe = _FakeSqlProbe([100.0, 10.0])  # before=100ms, after=10ms
    op = _FakeSqlOperator()

    async def run():
        return await act_measure(_base_state(), db, probe, op, backup_gate=_FakeBackupGate(True))

    update = asyncio.run(run())
    assert update["measurement"]["outcome"] == "success"
    assert update["measurement"]["measured_before"]["latency_ms"] == 100.0
    assert update["measurement"]["measured_after"]["latency_ms"] == 10.0
    assert update["action"]["status"] == "applied"
    assert update["action"]["outcome"] == "success"
    assert update["phase"] == "done"
    assert op.applied == ["CREATE INDEX IF NOT EXISTS orders_idx ON public.orders (customer_id)"]
    assert probe.calls == [
        "SELECT * FROM orders WHERE customer_id = 42",
        "SELECT * FROM orders WHERE customer_id = 42",
    ]
    assert len(db.act_decisions) == 1
    assert len(db.outcome_decisions) == 1


def test_failure_when_latency_does_not_improve():
    db = _FakeDb()
    probe = _FakeSqlProbe([10.0, 15.0])  # got WORSE
    op = _FakeSqlOperator()

    async def run():
        return await act_measure(_base_state(), db, probe, op, backup_gate=_FakeBackupGate(True))

    update = asyncio.run(run())
    assert update["measurement"]["outcome"] == "failure"
    assert update["action"]["outcome"] == "failure"


def test_measured_dicts_never_contain_raw_plan_text():
    db = _FakeDb()
    probe = _FakeSqlProbe([100.0, 10.0])
    op = _FakeSqlOperator()

    async def run():
        return await act_measure(_base_state(), db, probe, op, backup_gate=_FakeBackupGate(True))

    update = asyncio.run(run())
    for key in ("measured_before", "measured_after"):
        assert "raw_analyze_plan" not in update["measurement"][key]
        assert "raw_explain_plan" not in update["measurement"][key]
