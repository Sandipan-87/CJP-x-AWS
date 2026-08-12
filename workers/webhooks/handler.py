"""Engram · workers/webhooks/handler.py — POST /webhooks/alerts.  [PLUMBER]

design/02-low-level-design.md §11.2: `POST /webhooks/alerts`, auth "HMAC signature", behavior
"-> observations + incident task", idempotency "sha256 dedupe". An external alert source (a
CloudWatch alarm, a third-party monitor, anything that can compute an HMAC and POST JSON) feeds
an incident into the exact same `tasks`/`observations`/`entities` front door
`agent/nodes/observe.py`'s internal sweep path already writes through
(`workers/common/incident.py`, §5.1 step 4's one-txn insert, reimplemented independently -- see
that module's docstring for why).

**Request body shape is this module's own design choice, stated plainly: the LLD names the
auth/behavior/idempotency contract but not an exact JSON schema for what an alert payload looks
like.** Chosen to carry exactly what `insert_incident_observation` needs and nothing more:

    {
      "scope_id": "...",
      "target_cluster_id": "...",
      "source": "cloudwatch|manual|external",   # observations.source
      "kind": "metric|schema|query_stats|alert", # observations.kind
      "fingerprint_input": "<text the incident normalizes+hashes on>",
      "entity_kind": "table|cluster|...",
      "entity_name": "...",
      "payload": { ...arbitrary detail, stored as-is in observations.payload... }
    }

**Auth: HMAC-SHA256 over the raw request body**, hex-encoded, in the `X-Engram-Signature` header,
verified with `hmac.compare_digest` (constant-time, avoids a timing side-channel on the
comparison itself) against a shared secret. Must be computed over the RAW bytes API Gateway
handed this Lambda, before any JSON parsing -- computing it over a re-serialized body would
silently accept requests whose signature covered different bytes than what was actually sent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from common.config import resolve_secret
from common.db import get_webhook_connection
from common.incident import fingerprint, insert_incident_observation, normalize_text

ALLOWED_ORIGIN = os.environ.get("ENGRAM_DASHBOARD_ORIGIN", "*")
REQUIRED_FIELDS = (
    "scope_id",
    "target_cluster_id",
    "source",
    "kind",
    "fingerprint_input",
    "entity_kind",
    "entity_name",
)


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Headers": "Content-Type,X-Engram-Signature",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _get_header(event: dict[str, Any], name: str) -> str | None:
    headers = event.get("headers") or {}
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


def _raw_body(event: dict[str, Any]) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = event.get("httpMethod", "POST")
    if method == "OPTIONS":
        return _response(204, {})

    raw_body = _raw_body(event)
    secret = resolve_secret("ENGRAM_WEBHOOK_HMAC_SECRET", "ENGRAM_WEBHOOK_HMAC_SECRET_NAME")
    signature = _get_header(event, "X-Engram-Signature")
    if not _verify_signature(raw_body, signature, secret):
        # Deliberately the same generic message whether the header was missing or just wrong --
        # distinguishing them would tell an attacker which failure mode they hit.
        return _response(401, {"error": "invalid or missing signature"})

    try:
        body = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be valid JSON"})

    missing = [f for f in REQUIRED_FIELDS if not body.get(f)]
    if missing:
        return _response(400, {"error": f"missing required field(s): {missing}"})

    normalized = normalize_text(body["fingerprint_input"])
    incident_fingerprint = fingerprint(normalized)
    payload_json = json.dumps(body.get("payload") or {})

    conn = get_webhook_connection()
    try:
        task_id, observation_id, entity_id, is_new_incident = insert_incident_observation(
            conn,
            body["scope_id"],
            target_cluster_id=body["target_cluster_id"],
            incident_fingerprint=incident_fingerprint,
            source=body["source"],
            kind=body["kind"],
            payload_json=payload_json,
            entity_kind=body["entity_kind"],
            entity_name=body["entity_name"],
        )
    except Exception:  # noqa: BLE001
        conn.rollback()
        raise

    # 200 either way (LLD's "sha256 dedupe" is an idempotency guarantee, not a different status
    # code) -- `is_new_incident` in the body is how a caller tells a fresh incident from a
    # deduped-onto-existing one.
    return _response(
        200,
        {
            "task_id": task_id,
            "observation_id": observation_id,
            "entity_id": entity_id,
            "is_new_incident": is_new_incident,
            "incident_fingerprint": incident_fingerprint,
        },
    )
