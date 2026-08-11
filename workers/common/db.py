"""Engram · workers/common/db.py — lightweight DB access for Lambda workers.  [PLUMBER]

design/02-low-level-design.md directory tree: "common/ # shared DAO + config (thin layer, no
agent imports)". Deliberately NOT `agent/memory/db.py` -- that module is built for the long-lived
ECS agent process (a persistent `psycopg_pool.AsyncConnectionPool`), imports the full `agent`
package, and isn't meant to run inside a short-lived Lambda invocation. This is its own, much
smaller thing.

Uses `pg8000`, not `psycopg3` (the driver everywhere else in this repo), for one concrete reason:
`psycopg[binary]`'s C extension needs a manylinux wheel bundled for the Lambda runtime, which
CDK's Python Lambda bundling normally does via Docker -- and no Docker is available in this dev
environment (confirmed: `docker --version` -> command not found). `pg8000` is pure Python with
only pure-Python dependencies (`scramp`, `asn1crypto`) -- a plain `pip install --target` from any
platform produces a working Lambda package, no cross-compilation step needed. Measured, not
assumed: connected with it against the real memory cluster (CockroachDB speaks the Postgres wire
protocol) using `engram_approver`'s real credentials before writing anything that depends on it.

Credential resolution order: `ENGRAM_APPROVER_DSN` env var first (local testing --
`scripts/bootstrap_approver_role.py` writes this to the repo-root `.env`), else fetch from AWS
Secrets Manager via `ENGRAM_APPROVER_SECRET_NAME` (what the real deployed Lambda uses --
HLD's `secret/engram/*` convention, never a plaintext Lambda environment variable in production).
"""

from __future__ import annotations

import os
import pathlib
import ssl
from urllib.parse import urlparse

import pg8000.dbapi as dbapi

CERT_PATH = pathlib.Path(__file__).resolve().parent / "certs" / "memory-ca.crt"
DEFAULT_PORT = 26257

_connection: dbapi.Connection | None = None
_dsn_cache: str | None = None


def _fetch_dsn_from_secrets_manager(secret_name: str) -> str:
    import boto3  # imported lazily -- not needed at all for local ENGRAM_APPROVER_DSN testing

    client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    resp = client.get_secret_value(SecretId=secret_name)
    return resp["SecretString"]


def _resolve_dsn() -> str:
    global _dsn_cache
    if _dsn_cache:
        return _dsn_cache
    dsn = os.environ.get("ENGRAM_APPROVER_DSN")
    if dsn:
        _dsn_cache = dsn
        return dsn
    secret_name = os.environ.get("ENGRAM_APPROVER_SECRET_NAME")
    if not secret_name:
        raise RuntimeError(
            "neither ENGRAM_APPROVER_DSN nor ENGRAM_APPROVER_SECRET_NAME is set -- "
            "see workers/README.md"
        )
    _dsn_cache = _fetch_dsn_from_secrets_manager(secret_name)
    return _dsn_cache


def get_connection() -> dbapi.Connection:
    """Reused across warm Lambda invocations (module-level global, the standard Lambda
    connection-reuse pattern) -- pinged before reuse since CockroachDB Cloud can idle-close a
    connection between invocations, which a stale cached handle wouldn't otherwise reveal until
    the next real query fails.
    """
    global _connection
    if _connection is not None:
        try:
            cur = _connection.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            return _connection
        except Exception:  # noqa: BLE001
            _connection = None

    dsn = _resolve_dsn()
    p = urlparse(dsn)
    ctx = (
        ssl.create_default_context(cafile=str(CERT_PATH))
        if CERT_PATH.exists()
        else ssl.create_default_context()
    )
    _connection = dbapi.connect(
        user=p.username,
        password=p.password,
        host=p.hostname,
        port=p.port or DEFAULT_PORT,
        database=(p.path.lstrip("/") or "defaultdb"),
        ssl_context=ctx,
    )
    _connection.autocommit = True
    return _connection


def decide_approval(
    conn: dbapi.Connection, approval_id: str, status: str, decided_by: str, comment: str | None
) -> bool:
    """design/02-low-level-design.md §11.2's exact CAS statement: only a `pending` approval can
    be decided; `channel='dashboard'` distinguishes this from the agent's own
    `system:gate_timeout` expiry path (`agent/memory/db.py`'s `decide_approval`, a different
    method for a different caller -- that one never sets `channel` since the LLD only specifies
    `channel='dashboard'` for THIS endpoint). Returns whether this call's UPDATE actually matched
    a row -- False means either the approval doesn't exist or was already decided; the caller
    (`workers/approvals/handler.py`) tells those two apart with a follow-up SELECT.
    """
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE approvals
           SET status = %s, decided_by = %s, decided_at = now(), channel = 'dashboard', comment = %s
         WHERE approval_id = %s AND status = 'pending'
        """,
        (status, decided_by, comment, approval_id),
    )
    return cur.rowcount == 1


def get_approval_status(conn: dbapi.Connection, approval_id: str) -> str | None:
    """None means the approval_id doesn't exist at all -- the 404 case. A real status string
    (already-decided) is the 409 case. Both are distinguished by the caller.
    """
    cur = conn.cursor()
    cur.execute("SELECT status FROM approvals WHERE approval_id = %s", (approval_id,))
    row = cur.fetchone()
    return row[0] if row else None
