"""Engram · unit tests for agent/tools/recipe_renderer.py -- LLD §10's "single
most important safety control." Pure, synchronous, no cluster/mocks needed --
every validation step is directly testable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools.recipe_renderer import RecipeRejectedError, render


# --------------------------------------------------------------- happy paths

def test_create_index_renders_correctly():
    result = render("create_index", {"table": "orders", "columns": ["customer_id"]})
    assert result.sql == "CREATE INDEX IF NOT EXISTS orders_customer_id_idx ON public.orders (customer_id)"
    assert result.schema_checked is False  # no known_columns given


def test_create_index_multi_column():
    result = render("create_index", {"table": "orders", "columns": ["customer_id", "region"]})
    assert "customer_id, region" in result.sql


def test_create_index_custom_index_name():
    result = render("create_index", {"table": "orders", "columns": ["customer_id"],
                                       "index_name": "my_custom_idx"})
    assert "my_custom_idx" in result.sql


def test_analyze_table_renders_correctly():
    result = render("analyze_table", {"table": "orders"})
    assert result.sql == "ANALYZE public.orders"


def test_custom_schema():
    result = render("analyze_table", {"table": "orders"}, schema="myschema")
    assert result.sql == "ANALYZE myschema.orders"


def test_action_kind_accepts_enum_member_too():
    from agent.schemas import ActionKind
    result = render(ActionKind.analyze_table, {"table": "orders"})
    assert result.sql == "ANALYZE public.orders"


# --------------------------------------------------------- step 1: allowlist

def test_unknown_action_kind_rejected():
    with pytest.raises(RecipeRejectedError, match="allowlist"):
        render("drop_everything", {"table": "orders"})


# --------------------------------------------- step 2: real-schema cross-check

def test_known_columns_all_present_passes_and_marks_checked():
    result = render("create_index", {"table": "orders", "columns": ["customer_id"]},
                     known_columns={"customer_id", "id", "amount"})
    assert result.schema_checked is True


def test_fabricated_column_rejected_when_known_columns_given():
    with pytest.raises(RecipeRejectedError, match="no fabricated objects"):
        render("create_index", {"table": "orders", "columns": ["fake_col"]},
               known_columns={"customer_id", "id"})


def test_empty_known_columns_means_table_does_not_exist():
    with pytest.raises(RecipeRejectedError, match="does not exist"):
        render("analyze_table", {"table": "ghost_table"}, known_columns=set())


def test_schema_check_skipped_when_known_columns_none():
    """A fabricated column is NOT caught if the caller never provided real
    schema data -- schema_checked=False makes this observable, not silent."""
    result = render("create_index", {"table": "orders", "columns": ["totally_fake_column"]},
                     known_columns=None)
    assert result.schema_checked is False


# --------------------------------------------------- step 3: identifier regex

@pytest.mark.parametrize("bad_table", [
    "orders; DROP TABLE users",
    "orders--comment",
    "1orders",
    "Orders",       # uppercase not allowed by the regex
    "orders table", # space
])
def test_invalid_table_identifier_rejected(bad_table):
    # Empty string is covered separately (test_missing_table_rejected) --
    # it's caught by the "required" check before the identifier regex ever runs.
    with pytest.raises(RecipeRejectedError, match="not a valid identifier"):
        render("analyze_table", {"table": bad_table})


@pytest.mark.parametrize("bad_column", [
    "customer_id; DROP TABLE users",
    "customer id",
    "1column",
])
def test_invalid_column_identifier_rejected(bad_column):
    with pytest.raises(RecipeRejectedError, match="not a valid identifier"):
        render("create_index", {"table": "orders", "columns": [bad_column]})


def test_invalid_schema_identifier_rejected():
    with pytest.raises(RecipeRejectedError, match="not a valid identifier"):
        render("analyze_table", {"table": "orders"}, schema="bad;schema")


def test_invalid_index_name_rejected():
    with pytest.raises(RecipeRejectedError, match="not a valid identifier"):
        render("create_index", {"table": "orders", "columns": ["customer_id"],
                                  "index_name": "bad name"})


def test_non_string_table_rejected():
    with pytest.raises(RecipeRejectedError):
        render("analyze_table", {"table": 123})


# ------------------------------------------------------- required parameters

def test_missing_table_rejected():
    with pytest.raises(RecipeRejectedError, match="table.*required"):
        render("analyze_table", {})


def test_missing_columns_rejected_for_create_index():
    with pytest.raises(RecipeRejectedError, match="columns.*required"):
        render("create_index", {"table": "orders"})


def test_empty_columns_list_rejected():
    with pytest.raises(RecipeRejectedError, match="columns.*required"):
        render("create_index", {"table": "orders", "columns": []})


# ---------------------------------------------------- step 5: idempotency

def test_create_index_output_always_has_if_not_exists():
    result = render("create_index", {"table": "orders", "columns": ["customer_id"]})
    assert "IF NOT EXISTS" in result.sql
