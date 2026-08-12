"""Engram · unit tests for agent/main.py -- pure helpers + process_message()'s
control flow via scripted fakes (no real cluster/graph/AWS). Mirrors the
established pattern in tests/test_gate.py / tests/test_act_measure.py: hand-
rolled fake dependencies, `asyncio.run(run())`, no real network or DB.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.errors import BackupGateBlocked, EngramError
from agent.main import Runtime, _classify_exception, _initial_state, _thread_id_for_fingerprint, process_message
from agent.tools.sql_probe import ExplainResult


# ---------------------------------------------------------------- pure helpers

def test_thread_id_is_deterministic_per_fingerprint():
    assert _thread_id_for_fingerprint("abc123") == _thread_id_for_fingerprint("abc123")
    assert _thread_id_for_fingerprint("abc123") != _thread_id_for_fingerprint("xyz789")


def test_classify_exception_engram_error_is_parked():
    assert _classify_exception(BackupGateBlocked("no backup")) == "parked"


def test_classify_exception_other_is_failed():
    assert _classify_exception(RuntimeError("boom")) == "failed"
    assert _classify_exception(ValueError("bad")) == "failed"


def test_initial_state_shape():
    state = _initial_state("scope-1", "cluster-1", "manual", {"query_text": "select 1"})
    assert state["scope_id"] == "scope-1"
    assert state["target_cluster_id"] == "cluster-1"
    assert state["trigger"] == "manual"
    assert state["initial_probe"] == {"query_text": "select 1"}
    assert state["observations"] == []
    assert state["phase"] == "pending"


# ---------------------------------------------------------------- process_message fakes

class _FakeSqlProbe:
    def __init__(self, result: ExplainResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def explain_analyze(self, query_text: str) -> ExplainResult:
        self.calls.append(query_text)
        return self.result


class _FakeGraph:
    def __init__(self, outcome: str = "success", exc: Exception | None = None) -> None:
        self.outcome = outcome
        self.exc = exc
        self.calls: list[tuple] = []

    async def ainvoke(self, state, config=None):
        self.calls.append((dict(state), config))
        if self.exc is not None:
            raise self.exc
        return {**state, "phase": "done"}


class _FakeLeaseHandle:
    def __init__(self, *, lose_immediately: bool = False) -> None:
        self._lost = asyncio.Event()
        self.released = False
        if lose_immediately:
            self._lost.set()

    async def wait_until_lost(self) -> None:
        await self._lost.wait()

    async def release(self) -> None:
        self.released = True


class _FakeDb:
    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self._next_id = 0
        self.status_updates: list[tuple[str, str]] = []
        self.thread_id_writes: list[tuple[str, str]] = []

    async def insert_task(self, scope_id, task_type, trigger, *, target_cluster_id=None, incident_fingerprint=None, parent_task_id=None):
        self._next_id += 1
        task_id = f"task-{self._next_id}"
        self.tasks[task_id] = {
            "scope_id": scope_id, "task_type": task_type, "trigger": trigger,
            "target_cluster_id": target_cluster_id, "incident_fingerprint": incident_fingerprint,
        }
        return task_id

    async def set_checkpoint_thread_id(self, task_id, thread_id):
        self.thread_id_writes.append((task_id, thread_id))

    async def update_task_status(self, task_id, status):
        self.status_updates.append((task_id, status))


def _make_runtime(*, graph, sql_probe, db, lease_acquire) -> tuple[Runtime, object]:
    runtime = Runtime(
        db=db, embed_provider=None, llm=None, sql_probe=sql_probe, sql_operator=None,
        backup_gate=None, telemetry=None, graph=graph,
        holder_id="test-holder", lease_renew_s=15.0, latency_threshold_ms=1000.0,
    )
    return runtime, lease_acquire


NON_ANOMALOUS = ExplainResult(latency_ms=5.0, has_full_scan=False, index_candidate=None, raw_analyze_plan="", raw_explain_plan="")
ANOMALOUS = ExplainResult(latency_ms=5000.0, has_full_scan=True, index_candidate="customer_id", raw_analyze_plan="", raw_explain_plan="")

MESSAGE = {
    "scope_id": "scope-1", "target_cluster_id": "cluster-1",
    "table_name": "orders", "query_text": "SELECT * FROM orders WHERE customer_id = 1",
}


def test_sweep_skips_pre_insert_and_lease(monkeypatch):
    db = _FakeDb()
    graph = _FakeGraph()
    probe = _FakeSqlProbe(NON_ANOMALOUS)

    async def fake_acquire(*args, **kwargs):
        raise AssertionError("a non-anomalous (sweep) message must never acquire a lease")

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "completed"
    assert db.tasks == {}  # no pre-insert
    assert db.status_updates == []
    assert len(graph.calls) == 1


def test_incident_pre_inserts_task_and_reconciles_thread_id(monkeypatch):
    db = _FakeDb()
    graph = _FakeGraph()
    probe = _FakeSqlProbe(ANOMALOUS)
    handle = _FakeLeaseHandle()

    async def fake_acquire(db_arg, task_id, holder_id, *, renew_interval_s=15.0):
        assert task_id in db_arg.tasks  # task must exist BEFORE the lease is acquired
        return handle

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "completed"
    assert len(db.tasks) == 1
    (task_id,) = db.tasks.keys()
    assert db.tasks[task_id]["task_type"] == "incident"
    assert db.tasks[task_id]["incident_fingerprint"] is not None
    assert db.thread_id_writes == [(task_id, db.thread_id_writes[0][1])]
    assert db.thread_id_writes[0][1].startswith("tid-")
    assert ("running" in [s for _, s in db.status_updates])
    assert ("completed" in [s for _, s in db.status_updates])
    assert handle.released is True

    # config passed to ainvoke must carry the SAME thread_id that got reconciled
    _, config = graph.calls[0]
    assert config["configurable"]["thread_id"] == db.thread_id_writes[0][1]


def test_incident_engram_error_parks_and_deletes_lease(monkeypatch):
    db = _FakeDb()
    graph = _FakeGraph(exc=BackupGateBlocked("no recent backup"))
    probe = _FakeSqlProbe(ANOMALOUS)
    handle = _FakeLeaseHandle()

    async def fake_acquire(*args, **kwargs):
        return handle

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "parked"
    assert "parked" in [s for _, s in db.status_updates]
    assert handle.released is True


def test_incident_unexpected_exception_is_failed(monkeypatch):
    db = _FakeDb()
    graph = _FakeGraph(exc=RuntimeError("unexpected bug"))
    probe = _FakeSqlProbe(ANOMALOUS)
    handle = _FakeLeaseHandle()

    async def fake_acquire(*args, **kwargs):
        return handle

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "failed"
    assert "failed" in [s for _, s in db.status_updates]
    assert handle.released is True


def test_incident_lease_lost_mid_run_parks(monkeypatch):
    db = _FakeDb()

    class _HangingGraph:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, state, config=None):
            self.calls.append((state, config))
            await asyncio.sleep(3600)  # never completes on its own -- lease loss must win the race

    graph = _HangingGraph()
    probe = _FakeSqlProbe(ANOMALOUS)
    handle = _FakeLeaseHandle(lose_immediately=True)

    async def fake_acquire(*args, **kwargs):
        return handle

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "parked"
    assert "parked" in [s for _, s in db.status_updates]
    assert handle.released is True
