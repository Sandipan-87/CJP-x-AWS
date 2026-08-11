"""Engram · workers/approvals/handler.py — POST /approvals/{approval_id}.  [PLUMBER]

design/02-low-level-design.md §11.2, the dashboard's ONLY mutation path (HLD §5.6: "Mutations
(approve/reject) -> API Gateway -> Lambda -> memory cluster" -- never a direct DB write from the
browser or the dashboard's own serverless function, which only ever holds `engram_reader`, a
SELECT-only credential).

Request: `{"decision": "approve"|"reject", "by": "<name>", "comment": "<optional>"}`.
Response: 200 on success; 400 on a malformed request; 404 if `approval_id` doesn't exist; 409 if
it exists but isn't `pending` anymore (LLD's own CAS semantics: "rowcount 0 = 409 (already
decided)"). CORS handled here directly (Lambda proxy integration, not a gateway-level mock) since
API Gateway REST API's own CORS support only covers the OPTIONS preflight, not the actual
response headers on POST/error responses.

API-key auth is enforced by API Gateway itself (`apiKeyRequired=True` + a UsagePlan, wired in
`infra/`) -- this Lambda is never invoked at all for an unauthenticated request, so there's no
auth check to write here.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from common.db import decide_approval, get_approval_status, get_connection

DECISION_TO_STATUS = {"approve": "approved", "reject": "rejected"}
ALLOWED_ORIGIN = os.environ.get("ENGRAM_DASHBOARD_ORIGIN", "*")


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Headers": "Content-Type,X-Api-Key",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
        },
        "body": json.dumps(body),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = event.get("httpMethod", "POST")
    if method == "OPTIONS":
        return _response(204, {})

    approval_id = (event.get("pathParameters") or {}).get("approval_id")
    if not approval_id:
        return _response(400, {"error": "approval_id path parameter is required"})
    try:
        uuid.UUID(approval_id)
    except ValueError:
        # Caught live via scripts/local_approvals_api_shim.py during dashboard testing: a
        # non-UUID approval_id reached the UPDATE statement and CockroachDB raised a type-parse
        # error there instead of the query just matching 0 rows -- an unhandled 500-class crash
        # for what should be an ordinary 400 (a malformed client request, not a server error).
        return _response(400, {"error": "approval_id must be a valid UUID"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be valid JSON"})

    decision = body.get("decision")
    by = body.get("by")
    comment = body.get("comment")

    if decision not in DECISION_TO_STATUS:
        return _response(
            400, {"error": f"decision must be one of {sorted(DECISION_TO_STATUS)}"}
        )
    if not by:
        return _response(400, {"error": "'by' is required"})

    status = DECISION_TO_STATUS[decision]
    conn = get_connection()

    matched = decide_approval(conn, approval_id, status, by, comment)
    if matched:
        return _response(200, {"approval_id": approval_id, "status": status})

    current = get_approval_status(conn, approval_id)
    if current is None:
        return _response(404, {"error": "approval not found"})
    return _response(409, {"error": "already decided", "status": current})
