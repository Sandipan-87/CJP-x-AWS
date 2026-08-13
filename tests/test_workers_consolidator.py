"""Engram · unit tests for workers/consolidator/handler.py -- clustering/induction/promotion
routing and per-scope failure isolation. `get_consolidator_connection`/the common.db helpers are
all mocked here; the underlying DB privilege boundary is proven live by
scripts/bootstrap_lifecycle_roles.py, not duplicated here (same split as
tests/test_workers_sweep_enumerator.py).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workers"))

from consolidator.handler import handler  # noqa: E402

SCOPE = "scope-1"


def _episode(item_id: str, action_kind="create_index", outcome="success", table="orders"):
    return {
        "item_id": item_id,
        "embedding": [0.1] * 1024,
        "action_kind": action_kind,
        "outcome": outcome,
        "parameters": {"table": table},
    }


TIGHT_CLUSTER = [_episode("e1"), _episode("e2"), _episode("e3")]


def _patched(scopes, candidates_by_scope=None, neighbors=None, first_ever=True, insert_return="proc-1"):
    candidates_by_scope = candidates_by_scope or {}

    def _list_candidates(conn, scope_id):
        return candidates_by_scope.get(scope_id, [])

    return (
        patch("consolidator.handler.get_consolidator_connection", return_value=MagicMock()),
        patch("consolidator.handler.list_scopes_with_episodes", return_value=scopes),
        patch("consolidator.handler.list_unconsolidated_episodes", side_effect=_list_candidates),
        patch("consolidator.handler.ann_neighbors_within_distance", return_value=neighbors or []),
        patch("consolidator.handler.count_procedures_for_scope", return_value=0 if first_ever else 1),
        patch("consolidator.handler.insert_draft_procedure", return_value=insert_return),
        patch("consolidator.handler.promote_procedure_active", return_value="item-1"),
    )


def _neighbors_for(rows):
    return [(r["item_id"], 0.05) for r in rows]


def test_no_scopes_returns_zero():
    patches = _patched([])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = handler({}, None)
    assert result == {"statusCode": 200, "induced": 0, "activated": 0, "pending_confirm": [], "failed": 0, "scopes": 0}


def test_bucket_below_min_cluster_size_is_skipped():
    two_rows = TIGHT_CLUSTER[:2]
    patches = _patched([SCOPE], candidates_by_scope={SCOPE: two_rows}, neighbors=_neighbors_for(two_rows))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as mock_insert, patches[6]:
        result = handler({}, None)
    assert result["induced"] == 0
    mock_insert.assert_not_called()


def test_first_ever_induction_left_as_draft_not_promoted():
    patches = _patched(
        [SCOPE], candidates_by_scope={SCOPE: TIGHT_CLUSTER}, neighbors=_neighbors_for(TIGHT_CLUSTER),
        first_ever=True, insert_return="proc-1",
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as mock_promote:
        result = handler({}, None)
    assert result["induced"] == 1
    assert result["activated"] == 0
    assert result["pending_confirm"] == ["proc-1"]
    mock_promote.assert_not_called()


def test_subsequent_induction_promotes_to_active():
    patches = _patched(
        [SCOPE], candidates_by_scope={SCOPE: TIGHT_CLUSTER}, neighbors=_neighbors_for(TIGHT_CLUSTER),
        first_ever=False, insert_return="proc-2",
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as mock_promote:
        result = handler({}, None)
    assert result["induced"] == 1
    assert result["activated"] == 1
    assert result["pending_confirm"] == []
    mock_promote.assert_called_once()


def test_already_consolidated_cluster_is_a_noop():
    """`ON CONFLICT (scope_id, name) DO NOTHING` -> insert_draft_procedure returns None."""
    patches = _patched(
        [SCOPE], candidates_by_scope={SCOPE: TIGHT_CLUSTER}, neighbors=_neighbors_for(TIGHT_CLUSTER),
        first_ever=False, insert_return=None,
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as mock_promote:
        result = handler({}, None)
    assert result["induced"] == 0
    assert result["activated"] == 0
    mock_promote.assert_not_called()


def test_one_bad_scope_does_not_block_the_rest():
    """Same "never fail on a single source" rule already applied to sweep_enumerator/decayer."""
    def _list_candidates(conn, scope_id):
        if scope_id == "bad-scope":
            raise Exception("boom")
        return TIGHT_CLUSTER

    patches = (
        patch("consolidator.handler.get_consolidator_connection", return_value=MagicMock()),
        patch("consolidator.handler.list_scopes_with_episodes", return_value=["bad-scope", SCOPE]),
        patch("consolidator.handler.list_unconsolidated_episodes", side_effect=_list_candidates),
        patch("consolidator.handler.ann_neighbors_within_distance", return_value=_neighbors_for(TIGHT_CLUSTER)),
        patch("consolidator.handler.count_procedures_for_scope", return_value=1),
        patch("consolidator.handler.insert_draft_procedure", return_value="proc-3"),
        patch("consolidator.handler.promote_procedure_active", return_value="item-1"),
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = handler({}, None)
    assert result["failed"] == 1
    assert result["induced"] == 1
    assert result["scopes"] == 2
