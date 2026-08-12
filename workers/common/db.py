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

Two separate connection factories, one per Lambda/role, each with its own credential resolution
(env var first for local testing, else AWS Secrets Manager -- HLD's `secret/engram/*` convention,
never a plaintext Lambda environment variable in production):
  - `get_connection()` -- `engram_approver`, autocommit (every call is exactly one UPDATE/SELECT,
    `workers/approvals/handler.py`).
  - `get_webhook_connection()` -- `engram_webhook`, NOT autocommit: `workers/common/incident.py`'s
    `insert_incident_observation` needs explicit `commit()`/`rollback()` control across its
    multi-statement tasks+observations+entities transaction, the same way
    `agent/memory/db.py`'s version does over a single `pool.connection()` checkout.
"""

from __future__ import annotations

import pathlib
import ssl
from urllib.parse import urlparse

import pg8000.dbapi as dbapi

from common.config import resolve_secret

CERT_PATH = pathlib.Path(__file__).resolve().parent / "certs" / "memory-ca.crt"
DEFAULT_PORT = 26257

_connection: dbapi.Connection | None = None
_webhook_connection: dbapi.Connection | None = None


def _connect(dsn: str, *, autocommit: bool) -> dbapi.Connection:
    p = urlparse(dsn)
    ctx = (
        ssl.create_default_context(cafile=str(CERT_PATH))
        if CERT_PATH.exists()
        else ssl.create_default_context()
    )
    conn = dbapi.connect(
        user=p.username,
        password=p.password,
        host=p.hostname,
        port=p.port or DEFAULT_PORT,
        database=(p.path.lstrip("/") or "defaultdb"),
        ssl_context=ctx,
    )
    conn.autocommit = autocommit
    return conn


def _is_alive(conn: dbapi.Connection) -> bool:
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchall()
        return True
    except Exception:  # noqa: BLE001
        return False


def get_connection() -> dbapi.Connection:
    """`engram_approver`, autocommit. Reused across warm Lambda invocations (module-level
    global, the standard Lambda connection-reuse pattern) -- pinged before reuse since
    CockroachDB Cloud can idle-close a connection between invocations, which a stale cached
    handle wouldn't otherwise reveal until the next real query fails.
    """
    global _connection
    if _connection is not None and _is_alive(_connection):
        return _connection
    dsn = resolve_secret("ENGRAM_APPROVER_DSN", "ENGRAM_APPROVER_SECRET_NAME")
    _connection = _connect(dsn, autocommit=True)
    return _connection


def get_webhook_connection() -> dbapi.Connection:
    """`engram_webhook`, NOT autocommit -- see module docstring."""
    global _webhook_connection
    if _webhook_connection is not None and _is_alive(_webhook_connection):
        return _webhook_connection
    dsn = resolve_secret("ENGRAM_WEBHOOK_DSN", "ENGRAM_WEBHOOK_SECRET_NAME")
    _webhook_connection = _connect(dsn, autocommit=False)
    return _webhook_connection


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
