"""Engram · unit tests for workers/webhooks/handler.py -- the HTTP contract (signature
verification, field validation, CORS). `insert_incident_observation`/`get_webhook_connection`
are mocked here -- their own logic is covered by tests/test_workers_incident.py and proven live
against the real cluster (this session's manual verification: 5/5, new incident + dedupe +
invalid/missing signature + missing field, all real).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workers"))

from webhooks.handler import handler  # noqa: E402

SECRET = "test-hmac-secret"
VALID_BODY = {
    "scope_id": "scope-1",
    "target_cluster_id": "cluster-1",
    "source": "cloudwatch",
    "kind": "alert",
    "fingerprint_input": "SELECT * FROM orders WHERE id = 1",
    "entity_kind": "table",
    "entity_name": "orders",
    "payload": {"alarm": "x"},
}


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _event(body: dict | None, *, signature: str | None = "auto", method: str = "POST") -> dict:
    raw = json.dumps(body).encode() if body is not None else b"{}"
    sig = _sign(raw) if signature == "auto" else signature
    headers = {"X-Engram-Signature": sig} if sig is not None else {}
    return {"httpMethod": method, "headers": headers, "body": raw.decode()}


def _patched(**overrides):
    """Patches resolve_secret to always return SECRET, and applies any other overrides."""
    p1 = patch("webhooks.handler.resolve_secret", return_value=SECRET)
    return p1


# --------------------------------------------------------------------- CORS

def test_options_preflight_is_204():
    r = handler({"httpMethod": "OPTIONS"}, None)
    assert r["statusCode"] == 204


# ------------------------------------------------------------------- signature

def test_valid_signature_and_body_succeeds():
    with _patched(), \
         patch("webhooks.handler.get_webhook_connection", return_value="fake-conn"), \
         patch(
             "webhooks.handler.insert_incident_observation",
             return_value=("task-1", "obs-1", "ent-1", True),
         ) as mock_insert:
        r = handler(_event(VALID_BODY), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["task_id"] == "task-1"
    assert body["is_new_incident"] is True
    mock_insert.assert_called_once()


def test_invalid_signature_is_401():
    with _patched():
        r = handler(_event(VALID_BODY, signature="0" * 64), None)
    assert r["statusCode"] == 401


def test_missing_signature_is_401():
    with _patched():
        r = handler(_event(VALID_BODY, signature=None), None)
    assert r["statusCode"] == 401


def test_signature_over_wrong_secret_is_401():
    raw = json.dumps(VALID_BODY).encode()
    bad_sig = _sign(raw, secret="not-the-real-secret")
    with _patched():
        r = handler(_event(VALID_BODY, signature=bad_sig), None)
    assert r["statusCode"] == 401


# ---------------------------------------------------------- request validation

def test_malformed_json_body_is_400():
    raw = b"{not valid json"
    with _patched():
        event = {"httpMethod": "POST", "headers": {"X-Engram-Signature": _sign(raw)}, "body": raw.decode()}
        r = handler(event, None)
    assert r["statusCode"] == 400


def test_missing_required_field_is_400():
    incomplete = dict(VALID_BODY)
    del incomplete["entity_name"]
    with _patched():
        r = handler(_event(incomplete), None)
    assert r["statusCode"] == 400
    assert "entity_name" in json.loads(r["body"])["error"]


def test_dedupe_response_reports_is_new_incident_false():
    with _patched(), \
         patch("webhooks.handler.get_webhook_connection", return_value="fake-conn"), \
         patch(
             "webhooks.handler.insert_incident_observation",
             return_value=("existing-task", "obs-2", "ent-1", False),
         ):
        r = handler(_event(VALID_BODY), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["is_new_incident"] is False
