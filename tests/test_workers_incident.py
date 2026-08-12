"""Engram · unit tests for workers/common/incident.py -- the fingerprint helpers (mirrored from
agent/nodes/observe.py) and the one-txn tasks+observations+entities insert. The real-DB path is
already proven live (this session's manual verification: new incident, dedupe onto an existing
one, entity upsert, all against the real memory cluster) -- these tests cover the pure functions
and the dedupe control flow with a scripted fake connection, no real cluster needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workers"))

from common.incident import fingerprint, insert_incident_observation, normalize_text  # noqa: E402


# ------------------------------------------------------------- pure helpers

def test_normalize_collapses_literals():
    assert normalize_text("SELECT * FROM orders WHERE id = 42") == "select * from orders where id = ?"


def test_normalize_collapses_strings():
    assert normalize_text("status = 'high-latency'") == "status = ?"


def test_fingerprint_is_deterministic():
    a = fingerprint(normalize_text("SELECT * FROM t WHERE id = 1"))
    b = fingerprint(normalize_text("SELECT * FROM t WHERE id = 999"))
    assert a == b  # both normalize to the same "id = ?" shape


def test_fingerprint_differs_for_different_shapes():
    a = fingerprint(normalize_text("SELECT * FROM orders"))
    b = fingerprint(normalize_text("SELECT * FROM customers"))
    assert a != b


# -------------------------------------------------------- insert_incident_observation

class _FakeUniqueViolation(Exception):
    def __init__(self):
        super().__init__({"C": "23505", "M": "duplicate key"})


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    @property
    def connection(self):
        return self._conn

    def execute(self, sql, params):
        self._conn.calls.append((sql.strip().split("\n")[1].strip() if "\n" in sql else sql, params))
        if "INSERT INTO tasks" in sql and self._conn.raise_on_task_insert:
            self._conn.raise_on_task_insert = False
            raise _FakeUniqueViolation()
        if "SELECT task_id FROM tasks" in sql:
            self._row = (self._conn.existing_task_id,)
        elif "RETURNING task_id" in sql:
            self._row = (self._conn.new_task_id,)
        elif "RETURNING observation_id" in sql:
            self._row = (self._conn.new_observation_id,)
        elif "RETURNING entity_id" in sql:
            self._row = (self._conn.new_entity_id,)
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, *, raise_on_task_insert=False, existing_task_id="existing-task"):
        self.calls: list[tuple[str, tuple]] = []
        self.raise_on_task_insert = raise_on_task_insert
        self.existing_task_id = existing_task_id
        self.new_task_id = "new-task-id"
        self.new_observation_id = "obs-id"
        self.new_entity_id = "entity-id"
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _call(conn):
    return insert_incident_observation(
        conn,
        "scope-1",
        target_cluster_id="cluster-1",
        incident_fingerprint="fp-1",
        source="cloudwatch",
        kind="alert",
        payload_json="{}",
        entity_kind="table",
        entity_name="orders",
    )


def test_new_incident_commits_and_returns_new_ids():
    conn = _FakeConnection(raise_on_task_insert=False)
    task_id, obs_id, entity_id, is_new = _call(conn)
    assert (task_id, obs_id, entity_id, is_new) == ("new-task-id", "obs-id", "entity-id", True)
    assert conn.committed is True


def test_dedupe_onto_existing_incident_rolls_back_then_commits():
    conn = _FakeConnection(raise_on_task_insert=True, existing_task_id="existing-task")
    task_id, obs_id, entity_id, is_new = _call(conn)
    assert task_id == "existing-task"
    assert is_new is False
    assert conn.rolled_back is True  # the failed INSERT's transaction was rolled back
    assert conn.committed is True  # then the observation+entity writes committed cleanly
