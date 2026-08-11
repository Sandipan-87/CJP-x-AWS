"""Engram · agent/tools/recipe_renderer.py — allowlisted, idempotent SQL.  [PLUMBER]

design/02-low-level-design.md §10: "This is the single most important
safety control — it makes injection structurally impossible." Pure,
synchronous, dependency-free on purpose — the safety core shouldn't need a
live connection to prove it rejects a bad proposal; a caller wires in real
schema data separately (see `known_columns` below).

Validation pipeline, exactly as specified:

  1. `action_kind` in the allowlist (`agent.schemas.ActionKind`).
  2. `schema`/`table`/`columns` cross-checked against a real schema — LLD
     names MCP `get_table_schema`; that adapter doesn't exist yet.
     `known_columns` (e.g. from `SqlProbe.get_table_columns()`, which reads
     `information_schema` directly) is the substitute — same real signal,
     different access path. **Stated, not hidden:** if the caller passes
     `known_columns=None`, this step is SKIPPED, not silently treated as
     passed — `RenderedRecipe.schema_checked` records which happened.
  3. Identifiers quoted + regex `^[a-z_][a-z0-9_]*$`.
  4. Rendered SQL contains no `DROP|TRUNCATE|GRANT|ALTER|DELETE|UPDATE|
     INSERT|SET` or `;` (multi-statement) — defense-in-depth on top of (3):
     the identifier regex already makes injecting one of these impossible
     through a column/table name, so this check should never actually fire
     in practice; it fires anyway if it ever would.
  5. Output must be idempotent (`IF NOT EXISTS` for `CREATE INDEX`).

`ActionKind` is imported from `agent.schemas`, not redefined here — same
enum `Proposal.action_kind` already uses, values matching the LLD's shown
snippet exactly (`"create_index"`/`"analyze_table"`) even though this
module's member *names* are lowercase, not the LLD's shown uppercase — a
cosmetic difference; the wire values are what matter and those match.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from agent.schemas import ActionKind

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_FORBIDDEN_RE = re.compile(
    r"\b(DROP|TRUNCATE|GRANT|ALTER|DELETE|UPDATE|INSERT|SET)\b|;", re.IGNORECASE
)

TEMPLATES = {
    ActionKind.create_index: "CREATE INDEX IF NOT EXISTS {index_name} ON {schema}.{table} ({columns})",
    ActionKind.analyze_table: "ANALYZE {schema}.{table}",
}


class RecipeRejectedError(Exception):
    """Raised on ANY validation failure — never patched around, never
    partially rendered. This is the point where a bad proposal is caught
    BEFORE it ever becomes SQL a human reviews at the gate.
    """


class RenderedRecipe(NamedTuple):
    sql: str
    schema_checked: bool  # False = no live schema was available to cross-check (step 2 was skipped)


def _validate_identifier(name: str, *, label: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise RecipeRejectedError(f"{label} {name!r} is not a valid identifier (^[a-z_][a-z0-9_]*$)")
    return name


def render(
    action_kind: ActionKind | str,
    parameters: dict,
    *,
    schema: str = "public",
    known_columns: set[str] | None = None,
) -> RenderedRecipe:
    """`known_columns`, if given, is the real column set for
    `parameters["table"]` — step 2's cross-check. `None` means step 2 is
    skipped (see module docstring); `RenderedRecipe.schema_checked` records
    which happened, so a caller can refuse to proceed on `schema_checked=
    False` if it wants stricter behavior than this function itself enforces.
    """
    try:
        kind = ActionKind(action_kind)
    except ValueError as exc:
        raise RecipeRejectedError(f"action_kind {action_kind!r} is not in the allowlist") from exc

    table = parameters.get("table")
    if not table:
        raise RecipeRejectedError("parameters['table'] is required")
    _validate_identifier(table, label="table")
    _validate_identifier(schema, label="schema")

    schema_checked = known_columns is not None
    if schema_checked and not known_columns:
        raise RecipeRejectedError(f"table {schema}.{table} does not exist (empty/unknown column set)")

    if kind is ActionKind.create_index:
        columns = parameters.get("columns")
        if not columns:
            raise RecipeRejectedError("parameters['columns'] is required for create_index")
        for col in columns:
            _validate_identifier(col, label="column")
        if schema_checked:
            missing = set(columns) - known_columns
            if missing:
                raise RecipeRejectedError(
                    f"column(s) {sorted(missing)} do not exist on {schema}.{table} "
                    f"(known columns: {sorted(known_columns)}) — no fabricated objects"
                )
        index_name = parameters.get("index_name") or f"{table}_{'_'.join(columns)}_idx"
        _validate_identifier(index_name, label="index_name")
        sql = TEMPLATES[kind].format(
            index_name=index_name, schema=schema, table=table, columns=", ".join(columns)
        )
    elif kind is ActionKind.analyze_table:
        sql = TEMPLATES[kind].format(schema=schema, table=table)
    else:  # pragma: no cover — ActionKind has exactly these two members
        raise RecipeRejectedError(f"no template for action_kind {kind!r}")

    if _FORBIDDEN_RE.search(sql):
        # Should be unreachable given step 3's identifier regex — defense-in-depth, see module docstring.
        raise RecipeRejectedError(f"rendered SQL failed the forbidden-keyword/multi-statement check: {sql!r}")
    if kind is ActionKind.create_index and "IF NOT EXISTS" not in sql:
        raise RecipeRejectedError(f"rendered SQL is not idempotent (missing IF NOT EXISTS): {sql!r}")

    return RenderedRecipe(sql=sql, schema_checked=schema_checked)
