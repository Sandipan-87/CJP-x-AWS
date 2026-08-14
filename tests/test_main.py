"""Engram · unit tests for agent/main.py -- pure helpers + process_message()'s
control flow via scripted fakes (no real cluster/graph/AWS). Mirrors the
established pattern in tests/test_gate.py / tests/test_act_measure.py: hand-
rolled fake dependencies, `asyncio.run(run())`, no real network or DB.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.errors import BackupGateBlocked, EngramError
from agent.main import Runtime, _classify_exception, _initial_state, _thread_id_for_fingerprint, consume_loop, process_message
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
        # `state` is `None` on a checkpoint-resumed call (see `_should_resume`) --
        # recorded as `None` rather than `dict(None)`, which would raise.
        self.calls.append((dict(state) if state is not None else None, config))
        if self.exc is not None:
            raise self.exc
        return {**(state or {}), "phase": "done"}


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
    """`existing_task_id`/`existing_status` simulate `insert_task`'s dedupe
    path landing on an already-in-flight task -- the redelivery-after-crash
    scenario `_should_resume` cares about -- without needing a real
    `tasks_active_incident_idx` unique-violation round-trip.
    """

    def __init__(self, *, existing_task_id: str | None = None, existing_status: str = "pending") -> None:
        self.tasks: dict[str, dict] = {}
        self._next_id = 0
        self.status_updates: list[tuple[str, str]] = []
        self.thread_id_writes: list[tuple[str, str]] = []
        self._dedupe_onto = existing_task_id
        if existing_task_id is not None:
            self.tasks[existing_task_id] = {"status": existing_status}

    async def insert_task(self, scope_id, task_type, trigger, *, target_cluster_id=None, incident_fingerprint=None, parent_task_id=None):
        if self._dedupe_onto is not None:
            return self._dedupe_onto
        self._next_id += 1
        task_id = f"task-{self._next_id}"
        self.tasks[task_id] = {
            "scope_id": scope_id, "task_type": task_type, "trigger": trigger,
            "target_cluster_id": target_cluster_id, "incident_fingerprint": incident_fingerprint,
            "status": "pending",
        }
        return task_id

    async def get_task_status(self, task_id):
        return self.tasks.get(task_id, {}).get("status")

    async def set_checkpoint_thread_id(self, task_id, thread_id):
        self.thread_id_writes.append((task_id, thread_id))

    async def update_task_status(self, task_id, status):
        self.status_updates.append((task_id, status))
        if task_id in self.tasks:
            self.tasks[task_id]["status"] = status


class _FakeCheckpointer:
    """`has_progress=True` simulates a real checkpoint with at least one
    completed node (`channel_versions` non-empty); `False` simulates either
    no checkpoint at all or one from a run that never got past `START`.
    """

    def __init__(self, *, has_progress: bool) -> None:
        self.has_progress = has_progress
        self.calls: list[dict] = []

    async def aget_tuple(self, config):
        self.calls.append(config)
        if not self.has_progress:
            return None
        return SimpleNamespace(checkpoint={"channel_versions": {"phase": 3}})


def _make_runtime(*, graph, sql_probe, db, lease_acquire, checkpointer=None) -> tuple[Runtime, object]:
    runtime = Runtime(
        db=db, embed_provider=None, llm=None, sql_probe=sql_probe, sql_operator=None,
        backup_gate=None, telemetry=None, graph=graph,
        holder_id="test-holder", lease_renew_s=15.0, latency_threshold_ms=1000.0,
        checkpointer=checkpointer,
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


# ---------------------------------------------------------------- checkpoint-resume (_should_resume)

def test_incident_resumes_via_checkpoint_when_redelivered_mid_run(monkeypatch):
    """The actual fix: a dedupe hit onto an already-`running` task, backed by
    a real checkpoint with progress, must call `ainvoke(None, ...)` -- not
    replay a fresh `_initial_state()` -- so LangGraph's own checkpoint-resume
    skips whichever nodes already completed before the crash.
    """
    db = _FakeDb(existing_task_id="task-resume-1", existing_status="running")
    graph = _FakeGraph()
    probe = _FakeSqlProbe(ANOMALOUS)
    checkpointer = _FakeCheckpointer(has_progress=True)
    handle = _FakeLeaseHandle()

    async def fake_acquire(*args, **kwargs):
        return handle

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire, checkpointer=checkpointer)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "completed"
    assert len(checkpointer.calls) == 1  # aget_tuple was actually consulted
    resumed_state, _config = graph.calls[0]
    assert resumed_state is None  # None input -> LangGraph resumes, doesn't replay from observe
    assert db.thread_id_writes[0][0] == "task-resume-1"  # dedup'd onto the existing task, not a new one


def test_incident_does_not_resume_when_dedup_task_is_not_running(monkeypatch):
    """Status alone gates resume: a dedupe hit onto a task that isn't
    'running' (e.g. still 'pending' in some narrow race window) must run the
    full graph, even if a checkpoint with progress happens to exist for the
    shared thread_id.
    """
    db = _FakeDb(existing_task_id="task-2", existing_status="pending")
    graph = _FakeGraph()
    probe = _FakeSqlProbe(ANOMALOUS)
    checkpointer = _FakeCheckpointer(has_progress=True)
    handle = _FakeLeaseHandle()

    async def fake_acquire(*args, **kwargs):
        return handle

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire, checkpointer=checkpointer)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "completed"
    resumed_state, _config = graph.calls[0]
    assert resumed_state is not None  # fresh state, not a resume


def test_incident_falls_back_to_fresh_state_when_running_but_no_checkpoint_progress(monkeypatch):
    """The narrow race `_should_resume`'s docstring names: status is already
    'running' but no checkpoint was ever written (died before the first
    `ainvoke`). Must fall back to a fresh `_initial_state()`, not
    `ainvoke(None, ...)`, which would silently do nothing.
    """
    db = _FakeDb(existing_task_id="task-3", existing_status="running")
    graph = _FakeGraph()
    probe = _FakeSqlProbe(ANOMALOUS)
    checkpointer = _FakeCheckpointer(has_progress=False)
    handle = _FakeLeaseHandle()

    async def fake_acquire(*args, **kwargs):
        return handle

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire, checkpointer=checkpointer)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "completed"
    resumed_state, _config = graph.calls[0]
    assert resumed_state is not None  # no real checkpoint progress -> fresh state, not None


def test_incident_without_checkpointer_never_resumes(monkeypatch):
    """Backward compatibility: `Runtime.checkpointer=None` (every pre-existing
    caller/test) must never attempt a resume, even if status is 'running'.
    """
    db = _FakeDb(existing_task_id="task-4", existing_status="running")
    graph = _FakeGraph()
    probe = _FakeSqlProbe(ANOMALOUS)
    handle = _FakeLeaseHandle()

    async def fake_acquire(*args, **kwargs):
        return handle

    monkeypatch.setattr("agent.main.leases.acquire", fake_acquire)
    runtime, _ = _make_runtime(graph=graph, sql_probe=probe, db=db, lease_acquire=fake_acquire, checkpointer=None)

    outcome = asyncio.run(process_message(runtime, MESSAGE))

    assert outcome == "completed"
    resumed_state, _config = graph.calls[0]
    assert resumed_state is not None


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


# ---------------------------------------------------------------- consume_loop


def test_consume_loop_survives_an_exception_and_keeps_polling(monkeypatch):
    """Regression test for a real bug found live: `consume_loop()` previously had NO
    exception handling around `receive_message()`/`process_message()` at all -- a single
    unhandled exception (from either call) silently killed the whole consumer task
    forever, with no log line, since `main()` never inspects `running` for exceptions
    until shutdown and the health endpoint (a separate coroutine) kept reporting healthy
    regardless. A real deployed task was found zombied exactly this way -- 2+ real days
    with zero log activity, reproduced again within minutes on a fresh replacement task,
    diagnosed only by reproducing `process_message()` directly outside the container.
    This test asserts the loop now logs and CONTINUES past a raised exception instead of
    dying silently.
    """
    shutdown = asyncio.Event()
    call_count = {"n": 0}

    class _FakeSqsClient:
        def receive_message(self, **kwargs):  # pragma: no cover -- never actually invoked,
            return {"Messages": []}            # fake_to_thread intercepts the call entirely

    async def fake_to_thread(fn, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient SQS failure")
        shutdown.set()  # stop the loop cleanly after proving it survived the first call
        return {"Messages": []}

    async def fast_sleep(*_a, **_k):
        return

    monkeypatch.setattr("agent.main.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr("agent.main.asyncio.sleep", fast_sleep)  # skip the real backoff delay

    runtime = SimpleNamespace()  # never touched -- the exception fires before any real use
    asyncio.run(consume_loop(runtime, sqs_client=_FakeSqsClient(), queue_url="fake", shutdown=shutdown))

    assert call_count["n"] == 2  # proves the loop iterated again after the first call raised
