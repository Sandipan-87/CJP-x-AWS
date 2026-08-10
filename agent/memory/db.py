"""Engram · agent/memory/db.py — psycopg3 async pool + typed DAO.  [PLUMBER]

design/02-low-level-design.md §6.1. The first application code in this repo —
everything before this was DDL (db/migrations/) or verification scripts.

SCOPE, stated up front (coding-conduct rule 1 — never hide an assumption):

  - Pool size 5 (single task, low concurrency), connect_timeout 10s,
    statement_timeout 30s — exactly the numbers in §6.1.
  - Read-only retry only: an in-flight SELECT gets one retry with a fresh
    connection on OperationalError; a write is NEVER blindly retried (§6.1) —
    idempotency_key (invariant #4) and the ledger-first protocol (invariant
    #6) exist precisely so a write is either a provable no-op or already
    safe, and psycopg3's pool already replaces a dead connection on error.
  - Write-time fencing (§6.4's "every mutating DAO call accepts holder_id +
    fence_token") is implemented HERE only for the lease methods themselves
    (acquire/renew/release/takeover) — those are the only rows whose SQL
    fences on `fence_token` in this file. Threading fence_token through every
    OTHER write method (insert_observation, insert_decision, ...) was the LLD
    note's *alternative*, not its only option ("...or the caller verifies
    lease before write") — the higher-level retry/backoff lease policy in the
    not-yet-written `agent/memory/leases.py` is the intended caller that
    checks `renew_lease()` before issuing any other write. Db.py stays a flat
    DAO layer; the ordering discipline lives one level up, deliberately.
  - acquire_lease() and takeover_lease() share one implementation. §6.4's SQL
    block is a single transaction that both takes over an EXPIRED lease AND
    inserts a fresh one if the row doesn't exist yet — there's only one
    write-time behaviour here, "acquire" and "takeover" are the same call
    under two names the demo narrative distinguishes (first claim vs.
    reclaiming after `aws ecs stop-task`), not two different SQL paths.
  - No backoff/jitter loop lives here. §6.4's runbook comment ("if total
    affected rows == 1 -> we hold the lease. Else back off with jitter (1-3s)
    and retry") is retry POLICY, which belongs in leases.py, not in a single
    DAO call — db.py's acquire_lease is one attempt, returns whether it won.
  - dashboard_* methods are thin SELECT wrappers over the three views
    migration 001 already created (v_recent_tasks, v_action_feed,
    v_memory_inspector) — trivial, but included because the §6.1 DAO table
    names them explicitly; nothing speculative was added beyond that.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.errors import StaleLeaseError

DEFAULT_POOL_SIZE = 5                  # §6.1: single task, low concurrency
DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_LEASE_TTL_S = 60               # matches agent_leases.expires_at default
DEFAULT_RECALL_LIMIT = 20              # §6.5


def _vector_literal(vec: Sequence[float]) -> str:
    """CockroachDB VECTOR literal syntax: '[0.1,0.2,...]'.

    psycopg3 has no built-in adapter for CockroachDB's VECTOR type (it isn't
    pgvector), so every embedding param is passed as this string and cast
    with `%s::VECTOR(1024)` in the SQL — same pattern `scripts/run_sql.py`'s
    VEC_LITERAL regex already assumes when echoing statements.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _validate_as_of(as_of_ts: str) -> str:
    """§6.7: AS OF SYSTEM TIME takes no placeholder — CockroachDB only allows a
    constant literal there (issue #30955). Validate strictly before ever
    touching the SQL string, so raw input can never reach it.
    """
    dt = datetime.fromisoformat(as_of_ts)
    if dt.utcoffset() is None:
        raise ValueError(f"as_of_ts must carry a UTC offset, got {as_of_ts!r}")
    return as_of_ts


class Database:
    """Thin async DAO over the memory cluster. One instance per agent process."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ setup

    @classmethod
    async def connect(
        cls,
        dsn: str | None = None,
        *,
        pool_size: int = DEFAULT_POOL_SIZE,
        connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        sslrootcert: str | None = None,
    ) -> "Database":
        """Open the pool. Reads ENGRAM_MEMORY_DSN if `dsn` is not given.

        `sslrootcert`, same reasoning as `scripts/run_sql.py --sslrootcert`:
        a fresh machine (CI runner, new dev box) has no cluster CA at
        `~/.postgresql/root.crt`, and `sslrootcert=system` was measured
        2026-08-11 to fail against psycopg[binary]'s own bundled trust store.
        Pass the path from `https://cockroachlabs.cloud/clusters/<ID>/cert`.

        TODO(agent/config.py, LLD §2): once that pydantic-settings module
        exists this should take a Settings object instead of raw env/kwargs.
        Reading the env directly here keeps this module usable standalone
        until config.py lands — not a permanent design, an ordering fact.
        """
        dsn = dsn or os.environ.get("ENGRAM_MEMORY_DSN")
        if not dsn:
            raise RuntimeError("ENGRAM_MEMORY_DSN not set and no dsn provided")
        if sslrootcert and "sslrootcert=" not in dsn:
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}sslrootcert={sslrootcert}"

        async def configure(conn: psycopg.AsyncConnection) -> None:
            # Measured 2026-08-11: leaving this uncommitted parks the connection in
            # INTRANS, which the pool's own health check then discards as invalid —
            # pool.open() never completes, just times out with no clearer error.
            async with conn.cursor() as cur:
                await cur.execute(f"SET statement_timeout = {statement_timeout_ms}")
            await conn.commit()

        pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=pool_size,
            timeout=connect_timeout_s,
            configure=configure,
            open=False,
        )
        await pool.open(wait=True, timeout=connect_timeout_s)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def _write_cursor(self) -> AsyncIterator[psycopg.AsyncCursor]:
        """One connection, one transaction, autocommit off — writes are never
        retried on failure (see module docstring): the pool replaces a dead
        connection for the NEXT call, this one just surfaces the error.
        """
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                yield cur

    async def _read(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        """Read-only helper: retried once with a fresh connection on OperationalError.

        Safe only because it never mutates (§6.1: retry is read-only-only).
        """
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                async with self._pool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cur:
                        await cur.execute(sql, params)
                        return await cur.fetchall()
            except psycopg.OperationalError as exc:
                last_exc = exc
                if attempt == 2:
                    raise
        raise last_exc  # pragma: no cover — unreachable, satisfies type-checkers

    # ------------------------------------------------------------- tasks

    async def insert_task(
        self,
        scope_id: str,
        task_type: str,
        trigger: str,
        *,
        target_cluster_id: str | None = None,
        incident_fingerprint: str | None = None,
        parent_task_id: str | None = None,
    ) -> str:
        """A UniqueViolation on `tasks_active_incident_idx` is NOT an error
        (LLD §6.1's DAO table): another in-flight incident with the same
        (cluster, fingerprint) already exists, so return ITS task_id — the
        caller attaches its observation to that task instead of spawning a
        second agent. Never work around the uniqueness violation; reconcile.
        """
        async with self._write_cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO tasks (scope_id, task_type, trigger, target_cluster_id,
                                       incident_fingerprint, parent_task_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING task_id
                    """,
                    (scope_id, task_type, trigger, target_cluster_id,
                     incident_fingerprint, parent_task_id),
                )
                row = await cur.fetchone()
                return str(row["task_id"])
            except psycopg.errors.UniqueViolation:
                # Measured 2026-08-11: a caught error leaves the transaction
                # ABORTED — the next statement on the same connection fails
                # InFailedSqlTransaction unless rolled back first.
                await cur.connection.rollback()
                await cur.execute(
                    """
                    SELECT task_id FROM tasks
                    WHERE target_cluster_id = %s AND incident_fingerprint = %s
                      AND status IN ('pending','running','awaiting_approval','blocked')
                    LIMIT 1
                    """,
                    (target_cluster_id, incident_fingerprint),
                )
                row = await cur.fetchone()
                if row is None:  # pragma: no cover — race resolved between insert and select
                    raise
                return str(row["task_id"])

    async def update_task_status(self, task_id: str, status: str) -> None:
        async with self._write_cursor() as cur:
            await cur.execute(
                "UPDATE tasks SET status = %s, updated_at = now() WHERE task_id = %s",
                (status, task_id),
            )

    # ------------------------------------------------------ observations / entities

    async def insert_observation(
        self,
        scope_id: str,
        source: str,
        kind: str,
        payload: dict,
        *,
        task_id: str | None = None,
        target_cluster_id: str | None = None,
        fingerprint: str | None = None,
    ) -> str:
        async with self._write_cursor() as cur:
            await cur.execute(
                """
                INSERT INTO observations (scope_id, task_id, target_cluster_id,
                                           source, kind, fingerprint, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING observation_id
                """,
                (scope_id, task_id, target_cluster_id, source, kind, fingerprint, Jsonb(payload)),
            )
            row = await cur.fetchone()
            return str(row["observation_id"])

    async def upsert_entity(
        self, scope_id: str, kind: str, name: str, attributes: dict
    ) -> str:
        async with self._write_cursor() as cur:
            await cur.execute(
                """
                INSERT INTO entities (scope_id, kind, name, attributes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (scope_id, kind, name) DO UPDATE
                  SET attributes = excluded.attributes,
                      last_seen_at = now(),
                      version = entities.version + 1
                RETURNING entity_id
                """,
                (scope_id, kind, name, Jsonb(attributes)),
            )
            row = await cur.fetchone()
            return str(row["entity_id"])

    # ------------------------------------------------------------- memory_items

    async def insert_memory_item(
        self,
        scope_id: str,
        item_class: str,
        content: str,
        *,
        embedding: Sequence[float] | None = None,
        provenance: dict | None = None,
        entity_id: str | None = None,
        source_row_id: str | None = None,
    ) -> str:
        """`embedding=None` is the seed-then-backfill path (LLD §6.3 runbook,
        invariant #1): rows are written first, the backfill worker embeds
        them, migration 003 creates the vector index only after that.
        """
        vec_sql = "%s::VECTOR(1024)" if embedding is not None else "NULL"
        params: list[Any] = [scope_id, item_class, entity_id, source_row_id, content]
        if embedding is not None:
            params.append(_vector_literal(embedding))
        params.append(Jsonb(provenance or {}))
        async with self._write_cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO memory_items (scope_id, class, entity_id, source_row_id,
                                           content, embedding, provenance)
                VALUES (%s, %s, %s, %s, %s, {vec_sql}, %s)
                RETURNING item_id
                """,
                params,
            )
            row = await cur.fetchone()
            return str(row["item_id"])

    async def recall_ann(
        self,
        scope_id: str,
        vec: Sequence[float],
        *,
        limit: int = DEFAULT_RECALL_LIMIT,
        beam: int | None = None,
    ) -> list[dict]:
        """THE only ANN path (invariant #3) — every call equality-constrains
        scope_id AND orders by `embedding <=> $vec`. `memory/recall.py` (not
        yet written) is meant to be the only caller; a repo-wide grep CI
        check for stray `<=>` usage is the enforcement LLD §6.5 asks for,
        also not yet written — recorded here so it isn't forgotten.
        """
        literal = _vector_literal(vec)  # bound 3x below — one %s per occurrence, not string-embedded
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                if beam is not None:
                    # connection-local, reset by the pool handing back a
                    # plain connection on release — never leaks to reuse.
                    await cur.execute("SET vector_search_beam_size = %s", (beam,))
                await cur.execute(
                    """
                    SELECT item_id, class, content, provenance,
                           1 - (embedding <=> %s::VECTOR(1024)) AS similarity,
                           (embedding <=> %s::VECTOR(1024)) AS distance
                    FROM memory_items
                    WHERE scope_id = %s AND status = 'active'
                    ORDER BY embedding <=> %s::VECTOR(1024)
                    LIMIT %s
                    """,
                    (literal, literal, scope_id, literal, limit),
                )
                return await cur.fetchall()

    async def get_candidate_details(self, item_ids: Sequence[str]) -> list[dict]:
        """Enrich re-rank candidates (LLD §6.6's `hybrid()` needs confidence,
        status, entities beyond what recall_ann's SELECT already returns).
        """
        if not item_ids:
            return []
        return await self._read(
            """
            SELECT i.item_id, i.class, i.content, i.provenance, i.status,
                   i.created_at, i.updated_at,
                   p.confidence, p.status AS procedure_status,
                   p.outcome_stats
            FROM memory_items i
            LEFT JOIN procedures p ON p.procedure_id = i.source_row_id
            WHERE i.item_id = ANY(%s)
            """,
            ([str(x) for x in item_ids],),
        )

    # --------------------------------------------------------------- leases

    async def _acquire_or_takeover(self, task_id: str, holder_id: str) -> tuple[bool, int]:
        """§6.4's exact transaction: take over an EXPIRED lease, or insert a
        fresh one if the row doesn't exist. ONE attempt — no retry/jitter
        here, that policy belongs in the not-yet-written leases.py.

        Returns (won, fence_token). `won` is False if a live holder already
        exists — the caller backs off, this method does not loop.
        """
        # DEFAULT_LEASE_TTL_S is a trusted internal int constant, never user input —
        # f-strung directly into the literal. psycopg's %s substitution cannot be
        # used HERE: it would insert its own quoting *inside* the existing
        # INTERVAL '...' quotes and produce invalid SQL, not a parameterized one.
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                async with conn.transaction():
                    await cur.execute(
                        f"""
                        UPDATE agent_leases
                           SET holder_id = %s, fence_token = fence_token + 1,
                               acquired_at = now(), renewed_at = now(),
                               expires_at = now() + INTERVAL '{DEFAULT_LEASE_TTL_S} seconds'
                         WHERE task_id = %s AND expires_at < now()
                        RETURNING fence_token
                        """,
                        (holder_id, task_id),
                    )
                    updated = await cur.fetchone()
                    if updated is not None:
                        return True, int(updated["fence_token"])

                    await cur.execute(
                        f"""
                        INSERT INTO agent_leases (task_id, holder_id, fence_token,
                                                  acquired_at, renewed_at, expires_at)
                        VALUES (%s, %s, 1, now(), now(), now() + INTERVAL '{DEFAULT_LEASE_TTL_S} seconds')
                        ON CONFLICT (task_id) DO NOTHING
                        RETURNING fence_token
                        """,
                        (task_id, holder_id),
                    )
                    inserted = await cur.fetchone()
                    if inserted is not None:
                        return True, int(inserted["fence_token"])

                    return False, 0  # live holder exists; caller backs off + retries

    async def acquire_lease(self, task_id: str, holder_id: str) -> tuple[bool, int]:
        """First claim of a task. Same implementation as `takeover_lease` —
        see the module docstring for why that's one code path, not two."""
        return await self._acquire_or_takeover(task_id, holder_id)

    async def takeover_lease(self, task_id: str, holder_id: str) -> tuple[bool, int]:
        """Reclaiming a lease after the previous holder died (the kill-and-
        resume demo beat: `aws ecs stop-task` mid-remediation). Same
        implementation as `acquire_lease` — see the module docstring."""
        return await self._acquire_or_takeover(task_id, holder_id)

    async def renew_lease(self, task_id: str, holder_id: str, fence_token: int) -> None:
        """CAS renew. Raises StaleLeaseError (not a bool) on rowcount 0 —
        LLD §6.4/§16: a lost lease is parked, not silently reported false and
        possibly ignored by a caller that forgets to check a return value.
        """
        async with self._write_cursor() as cur:
            await cur.execute(
                f"""
                UPDATE agent_leases
                   SET renewed_at = now(), expires_at = now() + INTERVAL '{DEFAULT_LEASE_TTL_S} seconds'
                 WHERE task_id = %s AND holder_id = %s AND fence_token = %s
                """,
                (task_id, holder_id, fence_token),
            )
            if cur.rowcount == 0:
                raise StaleLeaseError(task_id, holder_id, fence_token)

    async def release_lease(self, task_id: str, holder_id: str) -> None:
        """SIGTERM path — a real DELETE (migration 002 grants engram_agent
        table-scoped DELETE on agent_leases specifically for this; every
        other table relies on Row-Level TTL instead, LLD §6.2 note (c)).
        """
        async with self._write_cursor() as cur:
            await cur.execute(
                "DELETE FROM agent_leases WHERE task_id = %s AND holder_id = %s",
                (task_id, holder_id),
            )

    # ------------------------------------------------------------- audit trail

    async def insert_decision(
        self,
        task_id: str,
        scope_id: str,
        node: str,
        model_id: str,
        reasoning: dict,
        *,
        citations: list[dict] | None = None,
        model_version: str | None = None,
        input_fingerprint: str | None = None,
    ) -> str:
        async with self._write_cursor() as cur:
            await cur.execute(
                """
                INSERT INTO decisions (task_id, scope_id, node, model_id, model_version,
                                        input_fingerprint, reasoning, citations)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING decision_id
                """,
                (task_id, scope_id, node, model_id, model_version, input_fingerprint,
                 Jsonb(reasoning), Jsonb(citations or [])),
            )
            row = await cur.fetchone()
            return str(row["decision_id"])

    async def insert_tool_call(
        self,
        task_id: str,
        tool: str,
        operation: str,
        arguments: dict,
        status: str,
        *,
        decision_id: str | None = None,
        result_summary: str | None = None,
        result_uri: str | None = None,
        content_sha256: str | None = None,
        error_code: str | None = None,
        latency_ms: int | None = None,
        finished_at: datetime | None = None,
    ) -> str:
        async with self._write_cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tool_calls (task_id, decision_id, tool, operation, arguments,
                                         result_summary, result_uri, content_sha256,
                                         status, error_code, latency_ms, finished_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING tool_call_id
                """,
                (task_id, decision_id, tool, operation, Jsonb(arguments), result_summary,
                 result_uri, content_sha256, status, error_code, latency_ms, finished_at),
            )
            row = await cur.fetchone()
            return str(row["tool_call_id"])

    # ------------------------------------------------------- remediation ledger

    async def insert_remediation_action(
        self,
        task_id: str,
        scope_id: str,
        target_cluster_id: str,
        action_kind: str,
        recipe_version: str,
        parameters: dict,
        rendered_sql: str,
        idempotency_key: str,
        status: str,
    ) -> str:
        """`idempotency_key UNIQUE` IS invariant #4's exactly-once guarantee.
        Same shape as `insert_task`'s incident dedupe: a UniqueViolation here
        means this exact change was already proposed — return the EXISTING
        row rather than erroring, and let the caller reconcile against it
        instead of retrying the write. Never work around the violation.
        """
        async with self._write_cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO remediation_actions
                        (task_id, scope_id, target_cluster_id, action_kind, recipe_version,
                         parameters, rendered_sql, idempotency_key, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING action_id
                    """,
                    (task_id, scope_id, target_cluster_id, action_kind, recipe_version,
                     Jsonb(parameters), rendered_sql, idempotency_key, status),
                )
                row = await cur.fetchone()
                return str(row["action_id"])
            except psycopg.errors.UniqueViolation:
                await cur.connection.rollback()  # see insert_task's identical fix, same cause
                await cur.execute(
                    "SELECT action_id FROM remediation_actions WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                row = await cur.fetchone()
                if row is None:  # pragma: no cover — race resolved between insert and select
                    raise
                return str(row["action_id"])

    async def update_remediation_status(
        self,
        action_id: str,
        status: str,
        *,
        outcome: str | None = None,
        measured_before: dict | None = None,
        measured_after: dict | None = None,
        applied_by: str | None = None,
        applied_at: datetime | None = None,
    ) -> None:
        async with self._write_cursor() as cur:
            await cur.execute(
                """
                UPDATE remediation_actions
                   SET status = %s, outcome = %s,
                       measured_before = COALESCE(%s, measured_before),
                       measured_after = COALESCE(%s, measured_after),
                       applied_by = COALESCE(%s, applied_by),
                       applied_at = COALESCE(%s, applied_at)
                 WHERE action_id = %s
                """,
                (status, outcome,
                 Jsonb(measured_before) if measured_before is not None else None,
                 Jsonb(measured_after) if measured_after is not None else None,
                 applied_by, applied_at, action_id),
            )

    async def get_by_idempotency_key(self, idempotency_key: str) -> dict | None:
        rows = await self._read(
            "SELECT * FROM remediation_actions WHERE idempotency_key = %s",
            (idempotency_key,),
        )
        return rows[0] if rows else None

    # -------------------------------------------------------------- approvals

    async def insert_approval(
        self, task_id: str, action_id: str, *, channel: str | None = None
    ) -> str:
        async with self._write_cursor() as cur:
            await cur.execute(
                """
                INSERT INTO approvals (task_id, action_id, channel)
                VALUES (%s, %s, %s)
                RETURNING approval_id
                """,
                (task_id, action_id, channel),
            )
            row = await cur.fetchone()
            return str(row["approval_id"])

    async def decide_approval(
        self, approval_id: str, decided_by: str, status: str, *, comment: str | None = None
    ) -> bool:
        """CAS: only a `pending` approval can be decided. Returns whether
        THIS call won the race — a concurrent decision (dashboard click vs.
        an expiry sweep) leaves rowcount 0, which is not an error, just a loss.
        """
        async with self._write_cursor() as cur:
            await cur.execute(
                """
                UPDATE approvals
                   SET status = %s, decided_by = %s, decided_at = now(), comment = %s
                 WHERE approval_id = %s AND status = 'pending'
                """,
                (status, decided_by, comment, approval_id),
            )
            return cur.rowcount == 1

    async def poll_approval(self, approval_id: str) -> dict | None:
        rows = await self._read(
            "SELECT * FROM approvals WHERE approval_id = %s", (approval_id,)
        )
        return rows[0] if rows else None

    # -------------------------------------------------------------- procedures

    async def update_procedure_stats(self, procedure_id: str, success: bool) -> None:
        async with self._write_cursor() as cur:
            await cur.execute(
                """
                UPDATE procedures
                   SET outcome_stats = jsonb_set(
                         jsonb_set(outcome_stats, '{attempts}',
                                   to_jsonb((outcome_stats->>'attempts')::INT + 1)),
                         '{successes}',
                         to_jsonb((outcome_stats->>'successes')::INT + %s)
                       ),
                       updated_at = now()
                 WHERE procedure_id = %s
                """,
                (1 if success else 0, procedure_id),
            )

    async def recompute_confidence(self, procedure_id: str, confidence: float) -> None:
        """Persists a confidence already computed by `memory/scoring.py`'s
        `wilson_lb()` (pure function, not this module's job to compute)."""
        async with self._write_cursor() as cur:
            await cur.execute(
                "UPDATE procedures SET confidence = %s, updated_at = now() WHERE procedure_id = %s",
                (confidence, procedure_id),
            )

    # ------------------------------------------------------------ audit replay

    async def audit_replay(self, task_id: str, as_of_ts: str) -> dict[str, list[dict]]:
        """Invariant #8: belief-state replay. §6.7's injection-safe pattern —
        validate first, only then interpolate the now-trusted literal.

        Measured 2026-08-11: putting a separate `AS OF SYSTEM TIME` clause on
        each of the three SELECTs raised `FeatureNotSupported: inconsistent
        AS OF SYSTEM TIME timestamp` on a pooled connection reused across
        statements — CockroachDB's own hint pointed at the actual fix: pin
        the timestamp ONCE for the whole transaction via `SET TRANSACTION AS
        OF SYSTEM TIME`, then run plain reads inside it.
        """
        validated = _validate_as_of(as_of_ts)
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                async with conn.transaction():
                    await cur.execute(f"SET TRANSACTION AS OF SYSTEM TIME '{validated}'")
                    await cur.execute(
                        "SELECT * FROM decisions WHERE task_id = %s ORDER BY created_at",
                        (task_id,),
                    )
                    decisions = await cur.fetchall()
                    await cur.execute(
                        "SELECT * FROM tool_calls WHERE task_id = %s ORDER BY started_at",
                        (task_id,),
                    )
                    tool_calls = await cur.fetchall()
                    await cur.execute(
                        "SELECT * FROM working_memory WHERE task_id = %s",
                        (task_id,),
                    )
                    working_memory = await cur.fetchall()
        return {
            "decisions": decisions,
            "tool_calls": tool_calls,
            "working_memory": working_memory,
        }

    # -------------------------------------------------------------- dashboard

    async def dashboard_recent_tasks(self) -> list[dict]:
        return await self._read("SELECT * FROM v_recent_tasks")

    async def dashboard_action_feed(self) -> list[dict]:
        return await self._read("SELECT * FROM v_action_feed")

    async def dashboard_memory_inspector(self) -> list[dict]:
        return await self._read("SELECT * FROM v_memory_inspector")
