"""Engram · agent/tools/sql_operator.py — SqlOperator: allowlisted DDL apply.  [PLUMBER]

design/02-low-level-design.md §7 adapter table: `SqlOperator | apply
(rendered_sql) | allowlisted DDL; timeout 60 s; no multi-statement | target
cluster.`

**Independent second validation, not trust.** `agent/tools/recipe_renderer.
py` already enforces "no forbidden keywords, no multi-statement" before it
ever renders a SQL string — but this adapter is the actual point of
irreversible effect on the target cluster, and LLD §16 names exactly this
failure mode: `SqlOperatorRejected` ("renderer bug or privilege"). The check
below is a FRESH, separately-written implementation, not an import of
`recipe_renderer`'s regex — a bug in one validator should not silently
disable both layers of the safety core at once.

PROVISIONING GAP, same pattern as `SqlProbe`: LLD §2 names `ENGRAM_TARGET_
OPERATOR_DSN` as a dedicated role scoped to allowlisted DDL only. Only the
admin-level `ENGRAM_TARGET_DSN` exists in `.env` as of this module's
authoring — falls back to it with a loud warning rather than silently
running as admin forever.
"""

from __future__ import annotations

import os
import re

import psycopg

from agent.errors import SqlOperatorRejected

DEFAULT_STATEMENT_TIMEOUT_MS = 60_000  # LLD §7: "timeout 60 s"

_FORBIDDEN_RE = re.compile(
    r"\b(DROP|TRUNCATE|GRANT|ALTER|DELETE|UPDATE|INSERT|SET)\b|;", re.IGNORECASE
)


class SqlOperator:
    """Async context manager — one connection per apply session, closed on exit."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        sslrootcert: str | None = None,
    ) -> None:
        dsn = dsn or os.environ.get("ENGRAM_TARGET_OPERATOR_DSN")
        if not dsn:
            dsn = os.environ.get("ENGRAM_TARGET_DSN")
            if not dsn:
                raise RuntimeError(
                    "neither ENGRAM_TARGET_OPERATOR_DSN nor ENGRAM_TARGET_DSN is set"
                )
            print(
                "WARN: ENGRAM_TARGET_OPERATOR_DSN not set — falling back to "
                "ENGRAM_TARGET_DSN (the admin role). Provision the dedicated "
                "allowlisted-DDL operator role on the target cluster before this "
                "runs against anything but a disposable sandbox."
            )
        if sslrootcert and "sslrootcert=" not in dsn:
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}sslrootcert={sslrootcert}"
        self._dsn = dsn
        self._statement_timeout_ms = statement_timeout_ms
        self._conn: psycopg.AsyncConnection | None = None

    async def __aenter__(self) -> "SqlOperator":
        self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        async with self._conn.cursor() as cur:
            await cur.execute(f"SET statement_timeout = {int(self._statement_timeout_ms)}")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._conn is not None:
            await self._conn.close()

    async def apply(self, rendered_sql: str) -> None:
        """Executes exactly one statement. Raises `SqlOperatorRejected`
        BEFORE touching the network if `rendered_sql` fails this adapter's
        own independent re-check — never trusts that whatever produced this
        string (`recipe_renderer` or otherwise) already validated it.
        """
        if self._conn is None:
            raise RuntimeError("SqlOperator must be used as an async context manager")
        if _FORBIDDEN_RE.search(rendered_sql):
            raise SqlOperatorRejected(
                f"rendered_sql failed the operator's own forbidden-keyword/"
                f"multi-statement check: {rendered_sql!r}"
            )
        async with self._conn.cursor() as cur:
            await cur.execute(rendered_sql)
