"""Engram · agent/tools/sql_probe.py — SqlProbe: EXPLAIN (ANALYZE) against the TARGET cluster.  [BRAINS]

design/02-low-level-design.md §7 adapter table: `SqlProbe | explain_analyze(sql)
-> plan_json, latency_ms | read-only role; statement timeout 10s | target cluster.`

The first tool adapter in this repo, and the first module that talks to the
TARGET cluster at all — everything before this (`agent/memory/*`) only ever
touched the MEMORY cluster. Two clusters, two roles, never conflate them
(CLAUDE.md §2) — this file exists specifically because that boundary is real.

MEASURED 2026-08-11 against the live target cluster, not assumed from the
LLD's prose alone: `EXPLAIN ANALYZE` gives real execution time and a
`spans: FULL SCAN` annotation on scan nodes, but **never** an "index
recommendations" section. Plain `EXPLAIN` (no `ANALYZE`) gives the
recommendations section but no real timing. Confirmed by hand against a
throwaway 20k-row scenario table before writing any parsing logic:

    EXPLAIN ANALYZE ...   -> "execution time: 23ms" ... "spans: FULL SCAN"
                             (no recommendations section at all)
    EXPLAIN ...           -> "spans: FULL SCAN" ... "index recommendations: 1
                             ... SQL command: CREATE INDEX ON t (customer_id) ..."
                             (no execution time)

`explain_analyze()` below runs BOTH and combines them — the LLD's own §5.3
text ("MCP `explain_query`, or `EXPLAIN ANALYZE` via the probe role")
doesn't name this specific gap because MCP's `explain_query` apparently
returns a combined view; our own non-MCP path needs two calls to get it.

PROVISIONING GAP, stated rather than hidden: LLD §2 names `ENGRAM_TARGET_
PROBE_DSN` as a dedicated read-only role on the target cluster. As of this
module's authoring, only the admin-level `ENGRAM_TARGET_DSN` exists in
`.env` — the read-only role has not actually been created. This module
prefers the probe DSN and falls back to the admin one with a loud warning,
rather than silently running as admin forever. Provisioning the real role
is a manual step (`CREATE ROLE ... ; GRANT SELECT ...` on the target
cluster), not something this module should do to itself.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

import psycopg

DEFAULT_STATEMENT_TIMEOUT_MS = 10_000  # LLD §7: "statement timeout 10 s"

_FULL_SCAN_RE = re.compile(r"spans:\s*FULL SCAN", re.IGNORECASE)
_EXEC_TIME_RE = re.compile(r"execution time:\s*([\d.]+)\s*(µs|ms|s)\b")
_INDEX_REC_RE = re.compile(
    r"CREATE INDEX(?:\s+\w+)?\s+ON\s+[\w.]+\s*\(([^)]+)\)", re.IGNORECASE
)
_UNIT_TO_MS = {"µs": 0.001, "ms": 1.0, "s": 1000.0}


class ExplainResult(NamedTuple):
    latency_ms: float                # real execution time, from EXPLAIN ANALYZE
    has_full_scan: bool
    index_candidate: str | None      # first recommended column list, e.g. "customer_id"
    raw_analyze_plan: str
    raw_explain_plan: str


def _first_execution_time_ms(plan_text: str) -> float | None:
    """First match only — CockroachDB prints the overall summary's
    'execution time' before the per-node tree, which also has its own
    (smaller) 'execution time' lines. Measured: taking the first match is
    what gets the summary, not a leaf node's partial timing.
    """
    m = _EXEC_TIME_RE.search(plan_text)
    if not m:
        return None
    value, unit = m.groups()
    return float(value) * _UNIT_TO_MS[unit]


def _first_index_candidate(plan_text: str) -> str | None:
    m = _INDEX_REC_RE.search(plan_text)
    return m.group(1).strip() if m else None


class SqlProbe:
    """Async context manager — one connection per probe session, closed on exit."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        sslrootcert: str | None = None,
    ) -> None:
        dsn = dsn or os.environ.get("ENGRAM_TARGET_PROBE_DSN")
        if not dsn:
            dsn = os.environ.get("ENGRAM_TARGET_DSN")
            if not dsn:
                raise RuntimeError(
                    "neither ENGRAM_TARGET_PROBE_DSN nor ENGRAM_TARGET_DSN is set"
                )
            print(
                "WARN: ENGRAM_TARGET_PROBE_DSN not set — falling back to "
                "ENGRAM_TARGET_DSN (the admin role). Provision the dedicated "
                "read-only probe role on the target cluster before this runs "
                "against anything but a disposable sandbox."
            )
        if sslrootcert and "sslrootcert=" not in dsn:
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}sslrootcert={sslrootcert}"
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        self._conn: psycopg.AsyncConnection | None = None

    async def __aenter__(self) -> "SqlProbe":
        self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        async with self._conn.cursor() as cur:
            await cur.execute(f"SET statement_timeout = {int(self._statement_timeout_ms)}")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def explain_analyze(self, sql: str) -> ExplainResult:
        """`sql` must be a complete, literal statement — `EXPLAIN` does not
        support bind parameters. This is a probe adapter, not a general
        query executor: callers pass a scenario's fixed SQL text, never
        interpolated user input (that would be a SQL-injection hole, the
        same class of risk `RecipeRenderer`'s allowlist exists to prevent
        on the write side).
        """
        if self._conn is None:
            raise RuntimeError("SqlProbe must be used as an async context manager")

        async with self._conn.cursor() as cur:
            await cur.execute(f"EXPLAIN ANALYZE {sql}")
            analyze_rows = await cur.fetchall()
        analyze_plan = "\n".join(r[0] for r in analyze_rows)

        async with self._conn.cursor() as cur:
            await cur.execute(f"EXPLAIN {sql}")
            explain_rows = await cur.fetchall()
        explain_plan = "\n".join(r[0] for r in explain_rows)

        latency_ms = _first_execution_time_ms(analyze_plan)
        if latency_ms is None:
            raise RuntimeError(
                f"could not parse execution time from EXPLAIN ANALYZE output: {analyze_plan[:200]!r}"
            )

        return ExplainResult(
            latency_ms=latency_ms,
            has_full_scan=bool(_FULL_SCAN_RE.search(analyze_plan)),
            index_candidate=_first_index_candidate(explain_plan),
            raw_analyze_plan=analyze_plan,
            raw_explain_plan=explain_plan,
        )

    async def get_table_columns(self, table: str, *, schema: str = "public") -> set[str] | None:
        """LLD §10 point 2's "cross-checked against MCP `get_table_schema`
        (no fabricated objects)" — that adapter doesn't exist yet, so
        `agent/tools/recipe_renderer.py` uses this real `information_schema`
        query instead. Same real signal, different access path; not a mock.

        Returns `None` if the table doesn't exist — the caller must treat
        that as a validation failure, never as "no columns to check."
        """
        if self._conn is None:
            raise RuntimeError("SqlProbe must be used as an async context manager")
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
            rows = await cur.fetchall()
        return {r[0] for r in rows} if rows else None
