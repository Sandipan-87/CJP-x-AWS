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

Three separate connection factories, one per Lambda/role, each with its own credential resolution
(env var first for local testing, else AWS Secrets Manager -- HLD's `secret/engram/*` convention,
never a plaintext Lambda environment variable in production):
  - `get_connection()` -- `engram_approver`, autocommit (every call is exactly one UPDATE/SELECT,
    `workers/approvals/handler.py`).
  - `get_webhook_connection()` -- `engram_webhook`, NOT autocommit: `workers/common/incident.py`'s
    `insert_incident_observation` needs explicit `commit()`/`rollback()` control across its
    multi-statement tasks+observations+entities transaction, the same way
    `agent/memory/db.py`'s version does over a single `pool.connection()` checkout.
  - `get_sweep_connection()` -- `engram_sweep_enumerator`, autocommit (read-only, one SELECT per
    invocation, `workers/sweep_enumerator/handler.py`).
  - `get_embedding_backfill_connection()` -- `engram_embedding_backfill`, autocommit (each row is
    its own independent SELECT-cache-check-then-UPDATE; no multi-statement transaction spans rows,
    `workers/embedding_backfill/handler.py`).
  - `get_decayer_connection()` -- `engram_decayer`, autocommit (same shape: each procedure is
    decayed independently, `workers/decayer/handler.py`).
  - `get_consolidator_connection()` -- `engram_consolidator`, NOT autocommit: a draft-to-active
    promotion writes both `procedures` and `memory_items` together and must not be torn between
    them, same reasoning as `get_webhook_connection()` above (`workers/consolidator/handler.py`).

`_vector_literal`/`_parse_vector_literal` are duplicated (not imported) from
`agent/memory/db.py` for the same reason `common/incident.py` duplicates
`normalize_query_text`/`fingerprint` -- small, pure, dependency-free functions are cheaper to
copy than to reach across the `workers`/`agent` boundary this project has otherwise kept clean.
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
_sweep_connection: dbapi.Connection | None = None
_embedding_backfill_connection: dbapi.Connection | None = None
_decayer_connection: dbapi.Connection | None = None
_consolidator_connection: dbapi.Connection | None = None


def _vector_literal(vec) -> str:
    """CockroachDB VECTOR literal syntax: '[0.1,0.2,...]' -- see agent/memory/db.py's own
    `_vector_literal` docstring; pg8000 has no adapter for VECTOR either."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _parse_vector_literal(s: str) -> list[float]:
    """Inverse of `_vector_literal` -- a raw VECTOR column reads back as this bracket-literal
    string, not a list, under pg8000 too (same measured behavior as psycopg3)."""
    return [float(x) for x in s.strip("[]").split(",")]


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


def get_sweep_connection() -> dbapi.Connection:
    """`engram_sweep_enumerator`, autocommit (read-only) -- see module docstring."""
    global _sweep_connection
    if _sweep_connection is not None and _is_alive(_sweep_connection):
        return _sweep_connection
    dsn = resolve_secret("ENGRAM_SWEEP_DSN", "ENGRAM_SWEEP_SECRET_NAME")
    _sweep_connection = _connect(dsn, autocommit=True)
    return _sweep_connection


def get_embedding_backfill_connection() -> dbapi.Connection:
    """`engram_embedding_backfill`, autocommit -- see module docstring."""
    global _embedding_backfill_connection
    if _embedding_backfill_connection is not None and _is_alive(_embedding_backfill_connection):
        return _embedding_backfill_connection
    dsn = resolve_secret("ENGRAM_EMBEDDING_BACKFILL_DSN", "ENGRAM_EMBEDDING_BACKFILL_SECRET_NAME")
    _embedding_backfill_connection = _connect(dsn, autocommit=True)
    return _embedding_backfill_connection


def get_decayer_connection() -> dbapi.Connection:
    """`engram_decayer`, autocommit -- see module docstring."""
    global _decayer_connection
    if _decayer_connection is not None and _is_alive(_decayer_connection):
        return _decayer_connection
    dsn = resolve_secret("ENGRAM_DECAYER_DSN", "ENGRAM_DECAYER_SECRET_NAME")
    _decayer_connection = _connect(dsn, autocommit=True)
    return _decayer_connection


def get_consolidator_connection() -> dbapi.Connection:
    """`engram_consolidator`, NOT autocommit -- see module docstring."""
    global _consolidator_connection
    if _consolidator_connection is not None and _is_alive(_consolidator_connection):
        return _consolidator_connection
    dsn = resolve_secret("ENGRAM_CONSOLIDATOR_DSN", "ENGRAM_CONSOLIDATOR_SECRET_NAME")
    _consolidator_connection = _connect(dsn, autocommit=False)
    return _consolidator_connection


def list_enabled_watched_queries(conn: dbapi.Connection) -> list[dict]:
    """`db/migrations/008_watched_queries.sql`'s registry -- the whole point of the sweep
    enumerator: WHICH (scope_id, target_cluster_id, table_name, query_text) combos are worth
    probing this tick. Returns plain dicts (not `agent/main.py`'s own `ProbeResult`/state
    TypedDicts -- `workers/` never imports `agent/`, same split as everywhere else in this file).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT watched_query_id, scope_id, target_cluster_id, table_name, query_text "
        "FROM watched_queries WHERE enabled = true"
    )
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


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


# ------------------------------------------------------------------ embedding_backfill

def list_memory_items_missing_embedding(conn: dbapi.Connection, limit: int = 500) -> list[dict]:
    """LLD §9's own idempotency mechanism: `WHERE embedding IS NULL LIMIT 500` -- a plain cursor,
    not an offset, since a successful UPDATE removes a row from this predicate's result set on
    the very next call; a crash mid-run just means the next invocation picks up where this one
    left off, no bookmark needed.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT item_id, content FROM memory_items WHERE embedding IS NULL LIMIT %s",
        (limit,),
    )
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def get_cached_embedding(conn: dbapi.Connection, content_hash: str, model_id: str) -> list[float] | None:
    """D9: a hit under a DIFFERENT model_id is not a hit (invariant #2) -- mirrors
    `agent/memory/embeddings.py`'s `embed_and_cache` exactly, reimplemented for pg8000."""
    cur = conn.cursor()
    cur.execute(
        "SELECT embedding FROM embedding_cache WHERE content_sha256 = %s AND model_id = %s",
        (content_hash, model_id),
    )
    row = cur.fetchone()
    return _parse_vector_literal(row[0]) if row else None


def insert_embedding_cache(conn: dbapi.Connection, content_hash: str, embedding, model_id: str) -> None:
    """`ON CONFLICT DO NOTHING` -- a cache keyed by content, not a ledger (same reasoning as
    `agent/memory/db.py`'s version): a concurrent writer embedding identical content is a
    harmless race, not an error."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO embedding_cache (content_sha256, embedding, model_id) "
        "VALUES (%s, %s::VECTOR(1024), %s) ON CONFLICT (content_sha256) DO NOTHING",
        (content_hash, _vector_literal(embedding), model_id),
    )


def update_memory_item_embedding(conn: dbapi.Connection, item_id: str, embedding) -> None:
    """The actual backfill write. Only touches `embedding` -- `updated_at` is deliberately left
    alone: this is filling in a value that was always meant to be there (seed-then-backfill,
    invariant #1), not a content change, and bumping it would confuse `decayer`'s age-based
    decay for any procedure this item happens to belong to.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE memory_items SET embedding = %s::VECTOR(1024) WHERE item_id = %s",
        (_vector_literal(embedding), item_id),
    )


# ------------------------------------------------------------------------------- decayer

def list_decaying_procedures(conn: dbapi.Connection) -> list[dict]:
    """Every non-retired procedure -- a retired one's confidence has already reached its floor
    and recomputing it forever would be pure waste, not a correctness issue (the formula is
    monotonically bounded, not oscillating).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT procedure_id, outcome_stats, updated_at, status FROM procedures WHERE status != 'retired'"
    )
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def update_procedure_confidence(conn: dbapi.Connection, procedure_id: str, confidence: float, retire: bool) -> None:
    """LLD §9's exact statement, split so `status` is only ever written when this run actually
    decided to retire the row -- reruns of an already-retired procedure never reach this function
    at all (filtered out by `list_decaying_procedures`), so there is no risk of a rerun trying to
    "un-retire" anything. `updated_at` is deliberately NOT touched -- see module note on
    `list_decaying_procedures`'s own reasoning: decaying a row must not reset the clock the decay
    formula itself reads (`now() - updated_at`), or every procedure would measure `age_days=0`
    forever and never actually decay.
    """
    cur = conn.cursor()
    if retire:
        cur.execute(
            "UPDATE procedures SET confidence = %s, status = 'retired' WHERE procedure_id = %s",
            (confidence, procedure_id),
        )
    else:
        cur.execute(
            "UPDATE procedures SET confidence = %s WHERE procedure_id = %s",
            (confidence, procedure_id),
        )


def retire_orphaned_memory_items(conn: dbapi.Connection, procedure_id: str) -> int:
    """LLD §9: "`memory_items.status='retired'` for orphaned embeddings" -- the `class='procedure'`
    memory item(s) that point at a procedure this run just retired. Returns the row count purely
    for the handler's own summary/logging.
    """
    cur = conn.cursor()
    cur.execute(
        "UPDATE memory_items SET status = 'retired' "
        "WHERE class = 'procedure' AND source_row_id = %s AND status != 'retired'",
        (procedure_id,),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- consolidator

def list_scopes_with_episodes(conn: dbapi.Connection) -> list[str]:
    """No `scopes` table exists anywhere in this schema -- the set of scopes worth considering is
    derived from whichever ones actually have episode memory, the same "derive it from real data,
    don't invent a registry that doesn't need to exist" judgment call as everywhere else in this
    file.
    """
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT scope_id FROM memory_items WHERE class = 'episode'")
    return [str(row[0]) for row in cur.fetchall()]


def list_unconsolidated_episodes(conn: dbapi.Connection, scope_id: str) -> list[dict]:
    """Episodes not yet claimed by any existing procedure's `sources` array -- the idempotency
    layer that keeps a rerun from re-clustering the same episodes into a second procedure. Joins
    to `remediation_actions` via `provenance->>'action_id'` to read the REAL action_kind/outcome/
    parameters a cluster would share, rather than parsing the human-readable episode `content`
    string `act_measure(node)`/`gate(node)` write it as (LLD's own "share outcome" is grounded
    here in structured data, not text matching).
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT i.item_id, i.embedding, ra.action_kind, ra.outcome, ra.parameters
          FROM memory_items i
          JOIN remediation_actions ra ON ra.action_id = (i.provenance->>'action_id')::UUID
         WHERE i.scope_id = %s AND i.class = 'episode' AND i.status = 'active'
           AND i.embedding IS NOT NULL
           AND NOT EXISTS (
             SELECT 1 FROM procedures p
              WHERE p.scope_id = %s AND p.sources @> to_jsonb(i.item_id::STRING)
           )
        """,
        (scope_id, scope_id),
    )
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def ann_neighbors_within_distance(
    conn: dbapi.Connection, scope_id: str, item_ids: list[str], seed_embedding, max_distance: float, limit: int
) -> list[tuple[str, float]]:
    """Invariant #3: every ANN query equality-constrains scope_id. Restricted to the candidate
    pool (`item_ids`, this call's own not-yet-consolidated episode set) via `= ANY(%s)` rather
    than a bare scoped scan, so a large scope's history doesn't get re-ranked on every seed.
    Returns `(item_id, distance)` pairs -- `distance` is cosine distance (`<=>`), so
    `distance <= max_distance` is the caller's similarity-threshold check (LLD's "sim >= 0.9" is
    `distance <= 0.1` for Cohere's unit-norm vectors).
    """
    literal = _vector_literal(seed_embedding)  # bound twice below, one %s per occurrence --
    # see agent/memory/db.py's own recall_ann for why this must never be string-embedded.
    cur = conn.cursor()
    cur.execute(
        """
        SELECT item_id, embedding <=> %s::VECTOR(1024) AS distance
          FROM memory_items
         WHERE scope_id = %s AND item_id = ANY(%s) AND embedding IS NOT NULL
         ORDER BY embedding <=> %s::VECTOR(1024)
         LIMIT %s
        """,
        (literal, scope_id, item_ids, literal, limit),
    )
    return [(str(row[0]), float(row[1])) for row in cur.fetchall() if float(row[1]) <= max_distance]


def count_procedures_for_scope(conn: dbapi.Connection, scope_id: str) -> int:
    """LLD §9: "first induction per scope requires human confirm" -- 0 means this scope has never
    had a procedure induced before, the exact condition this function exists to check.
    """
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM procedures WHERE scope_id = %s", (scope_id,))
    return int(cur.fetchone()[0])


def insert_draft_procedure(
    conn: dbapi.Connection, scope_id: str, name: str, description: str, steps: str,
    outcome_stats: str, confidence: float, sources_json: str, status: str,
) -> str | None:
    """`ON CONFLICT (scope_id, name) DO NOTHING` -- LLD §9's own stated idempotency: a rerun that
    reconstructs the identical cluster derives the identical deterministic `name` and simply no-ops
    here instead of creating a duplicate. Returns None on that no-op path (nothing was inserted),
    the caller-visible signal that this cluster was already consolidated under this name.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO procedures (scope_id, name, description, steps, outcome_stats, confidence, sources, status)
        VALUES (%s, %s, %s, %s::JSONB, %s::JSONB, %s, %s::JSONB, %s)
        ON CONFLICT (scope_id, name) DO NOTHING
        RETURNING procedure_id
        """,
        (scope_id, name, description, steps, outcome_stats, confidence, sources_json, status),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def promote_procedure_active(conn: dbapi.Connection, procedure_id: str, scope_id: str, content: str, provenance_json: str) -> str:
    """The 'draft' -> 'active' + `memory_items(class='procedure')` step LLD §9 describes for
    every NON-first induction in a scope. One transaction (this connection is opened
    `autocommit=False`, see `get_consolidator_connection`'s docstring): a procedure that's
    'active' but has no matching memory item would be unrecallable dead weight; a memory item
    with no active procedure behind it would cite something that doesn't really exist.
    """
    cur = conn.cursor()
    try:
        cur.execute("UPDATE procedures SET status = 'active' WHERE procedure_id = %s", (procedure_id,))
        cur.execute(
            """
            INSERT INTO memory_items (scope_id, class, source_row_id, content, provenance)
            VALUES (%s, 'procedure', %s, %s, %s::JSONB)
            RETURNING item_id
            """,
            (scope_id, procedure_id, content, provenance_json),
        )
        item_id = str(cur.fetchone()[0])
        conn.commit()
        return item_id
    except Exception:
        conn.rollback()
        raise
