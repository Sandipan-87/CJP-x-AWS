"""Engram · workers/common/incident.py — the "one txn: tasks+observations+entities" insert,
reimplemented for Lambda.  [PLUMBER]

design/02-low-level-design.md §5.1 step 4 / §11.2 (`POST /webhooks/alerts` -> "observations +
incident task"). This is deliberately a SEPARATE implementation from `agent/memory/db.py`'s
`insert_incident_observation` — same SQL, same dedupe logic, same partial unique index
(`tasks_active_incident_idx`) — not a shared import, because `workers/` has no dependency on the
`agent` package (this project's own directory-tree convention: "common/ # shared DAO + config
(thin layer, no agent imports)"). A webhook alert is a genuinely different caller writing through
the same front door `observe(node)` already uses internally, not a shortcut around it.

Also carries `normalize_query_text`/`fingerprint`, copied (not imported) from
`agent/nodes/observe.py` for the same reason -- both are small, pure, dependency-free functions;
duplicating two pure helpers is cheaper and more honest here than reaching across the
`workers`/`agent` boundary this project has otherwise kept clean.
"""

from __future__ import annotations

import hashlib
import re

from pg8000.dbapi import Connection

_NUMBER_LITERAL = re.compile(r"\b\d+\b")
_STRING_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")

UNIQUE_VIOLATION = "23505"  # SQLSTATE, checked against pg8000's DatabaseError.args[0]["C"]


def normalize_text(raw: str) -> str:
    """Same normalization as `agent/nodes/observe.py`'s `normalize_query_text` -- collapses
    literals so semantically-identical alerts fingerprint identically.
    """
    text = raw.strip().lower()
    text = _STRING_LITERAL.sub("?", text)
    text = _NUMBER_LITERAL.sub("?", text)
    text = re.sub(r"\s+", " ", text)
    return text


def fingerprint(normalized_text: str) -> str:
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def _is_unique_violation(exc: Exception) -> bool:
    args = getattr(exc, "args", None)
    if not args or not isinstance(args[0], dict):
        return False
    return args[0].get("C") == UNIQUE_VIOLATION


def insert_incident_observation(
    conn: Connection,
    scope_id: str,
    *,
    target_cluster_id: str,
    incident_fingerprint: str,
    source: str,
    kind: str,
    payload_json: str,
    entity_kind: str,
    entity_name: str,
) -> tuple[str, str, str, bool]:
    """Mirrors `agent/memory/db.py`'s `insert_incident_observation` exactly: INSERT tasks
    (incident dedupe via `tasks_active_incident_idx`'s unique violation -> fall back to SELECT
    the existing active incident) + INSERT observations + upsert entities, all on one
    connection/transaction (pg8000's dbapi connection defaults to non-autocommit, so this
    function's caller must NOT set `conn.autocommit = True` -- unlike `workers/common/db.py`'s
    approvals connection, which only ever runs one statement per call and can afford to).

    Returns `(task_id, observation_id, entity_id, is_new_incident)` -- `is_new_incident=False`
    means this call deduped onto an already-active incident (LLD §11.2's "sha256 dedupe").
    """
    cur = conn.cursor()
    is_new_incident = True
    try:
        cur.execute(
            """
            INSERT INTO tasks (scope_id, task_type, trigger, target_cluster_id, incident_fingerprint)
            VALUES (%s, 'incident', 'webhook', %s, %s)
            RETURNING task_id
            """,
            (scope_id, target_cluster_id, incident_fingerprint),
        )
        task_id = str(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001
        if not _is_unique_violation(exc):
            raise
        conn.rollback()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT task_id FROM tasks
            WHERE target_cluster_id = %s AND incident_fingerprint = %s
              AND status IN ('pending','running','awaiting_approval','blocked')
            LIMIT 1
            """,
            (target_cluster_id, incident_fingerprint),
        )
        row = cur.fetchone()
        if row is None:  # pragma: no cover -- race resolved between insert and select
            raise
        task_id = str(row[0])
        is_new_incident = False

    cur.execute(
        """
        INSERT INTO observations (scope_id, task_id, target_cluster_id, source, kind, fingerprint, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING observation_id
        """,
        (scope_id, task_id, target_cluster_id, source, kind, incident_fingerprint, payload_json),
    )
    observation_id = str(cur.fetchone()[0])

    cur.execute(
        """
        INSERT INTO entities (scope_id, kind, name)
        VALUES (%s, %s, %s)
        ON CONFLICT (scope_id, kind, name) DO UPDATE
          SET last_seen_at = now(),
              version = entities.version + 1
        RETURNING entity_id
        """,
        (scope_id, entity_kind, entity_name),
    )
    entity_id = str(cur.fetchone()[0])

    conn.commit()
    return task_id, observation_id, entity_id, is_new_incident
