"""Engram · unit tests for workers/approvals/handler.py -- the dashboard's ONLY mutation path
(design/02-low-level-design.md §11.2). Mocked `common.db` calls, no real cluster -- the real
cluster path is `scripts/smoke_test_approvals_lambda.py`, which passed 6/6 live.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workers"))

from approvals.handler import handler  # noqa: E402


FAKE_UUID = "11111111-1111-1111-1111-111111111111"


def _event(approval_id: str | None, body: dict | None, method: str = "POST") -> dict:
    return {
        "httpMethod": method,
        "pathParameters": {"approval_id": approval_id} if approval_id else None,
        "body": json.dumps(body) if body is not None else None,
    }


# --------------------------------------------------------------------- CORS

def test_options_preflight_returns_204_with_cors_headers():
    r = handler({"httpMethod": "OPTIONS"}, None)
    assert r["statusCode"] == 204
    assert r["headers"]["Access-Control-Allow-Methods"] == "POST,OPTIONS"


def test_every_response_has_cors_headers():
    r = handler(_event(None, {}), None)
    assert "Access-Control-Allow-Origin" in r["headers"]


# ---------------------------------------------------------- request validation

def test_non_uuid_approval_id_is_400_not_a_crash():
    """Caught live via scripts/local_approvals_api_shim.py: a non-UUID approval_id used to reach
    the UPDATE statement and CockroachDB raised a type-parse error there -- an unhandled crash
    for what should be an ordinary 400."""
    r = handler(_event("not-a-uuid-at-all", {"decision": "approve", "by": "alice"}), None)
    assert r["statusCode"] == 400
    assert "UUID" in json.loads(r["body"])["error"]


def test_missing_approval_id_is_400():
    r = handler(_event(None, {"decision": "approve", "by": "alice"}), None)
    assert r["statusCode"] == 400


def test_malformed_json_body_is_400():
    event = {"httpMethod": "POST", "pathParameters": {"approval_id": FAKE_UUID}, "body": "{not json"}
    r = handler(event, None)
    assert r["statusCode"] == 400


def test_invalid_decision_is_400():
    r = handler(_event(FAKE_UUID, {"decision": "maybe", "by": "alice"}), None)
    assert r["statusCode"] == 400
    assert "decision" in json.loads(r["body"])["error"]


def test_missing_by_is_400():
    r = handler(_event(FAKE_UUID, {"decision": "approve"}), None)
    assert r["statusCode"] == 400
    assert "by" in json.loads(r["body"])["error"]


def test_missing_body_is_400():
    event = {"httpMethod": "POST", "pathParameters": {"approval_id": FAKE_UUID}, "body": None}
    r = handler(event, None)
    assert r["statusCode"] == 400


# --------------------------------------------------------------- CAS decision

@patch("approvals.handler.get_connection", return_value="fake-conn")
@patch("approvals.handler.decide_approval", return_value=True)
def test_successful_approve_is_200(mock_decide, mock_conn):
    r = handler(_event(FAKE_UUID, {"decision": "approve", "by": "alice"}), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body == {"approval_id": FAKE_UUID, "status": "approved"}
    mock_decide.assert_called_once_with("fake-conn", FAKE_UUID, "approved", "alice", None)


@patch("approvals.handler.get_connection", return_value="fake-conn")
@patch("approvals.handler.decide_approval", return_value=True)
def test_successful_reject_maps_to_rejected_status(mock_decide, mock_conn):
    r = handler(_event(FAKE_UUID, {"decision": "reject", "by": "alice", "comment": "nope"}), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["status"] == "rejected"
    mock_decide.assert_called_once_with("fake-conn", FAKE_UUID, "rejected", "alice", "nope")


@patch("approvals.handler.get_connection", return_value="fake-conn")
@patch("approvals.handler.get_approval_status", return_value="approved")
@patch("approvals.handler.decide_approval", return_value=False)
def test_already_decided_is_409(mock_decide, mock_status, mock_conn):
    """CAS UPDATE matched 0 rows, but the approval DOES exist -- LLD §11.2's 409 case."""
    r = handler(_event(FAKE_UUID, {"decision": "approve", "by": "alice"}), None)
    assert r["statusCode"] == 409
    assert json.loads(r["body"])["status"] == "approved"


@patch("approvals.handler.get_connection", return_value="fake-conn")
@patch("approvals.handler.get_approval_status", return_value=None)
@patch("approvals.handler.decide_approval", return_value=False)
def test_unknown_approval_id_is_404(mock_decide, mock_status, mock_conn):
    """CAS UPDATE matched 0 rows AND the approval doesn't exist -- LLD §11.2's 404 case."""
    r = handler(_event(FAKE_UUID, {"decision": "approve", "by": "alice"}), None)
    assert r["statusCode"] == 404
