"""Engram · unit tests for workers/decayer/handler.py -- decay/retire routing and per-row failure
isolation. `get_decayer_connection`/the common.db helpers are all mocked here; the underlying DB
privilege boundary is proven live by scripts/bootstrap_lifecycle_roles.py, not duplicated here
(same split as tests/test_workers_sweep_enumerator.py).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workers"))

from common.scoring import CONFIDENCE_FLOOR, decayed_confidence  # noqa: E402
from decayer.handler import handler  # noqa: E402

NOW = datetime.now(timezone.utc)

# age_days=0 (updated_at=NOW) isolates the assertion to successes/attempts alone.
HEALTHY = {"procedure_id": "p-healthy", "outcome_stats": {"successes": 47, "attempts": 50}, "updated_at": NOW, "status": "active"}
DEAD = {"procedure_id": "p-dead", "outcome_stats": {"successes": 0, "attempts": 0}, "updated_at": NOW, "status": "active"}


def _patched(rows, retired_item_count=0):
    return (
        patch("decayer.handler.get_decayer_connection", return_value="fake-conn"),
        patch("decayer.handler.list_decaying_procedures", return_value=rows),
        patch("decayer.handler.update_procedure_confidence"),
        patch("decayer.handler.retire_orphaned_memory_items", return_value=retired_item_count),
    )


def test_no_candidates_returns_zero():
    p1, p2, p3, p4 = _patched([])
    with p1, p2, p3, p4:
        result = handler({}, None)
    assert result == {"statusCode": 200, "decayed": 0, "retired": 0, "orphaned_items_retired": 0, "failed": 0, "candidates": 0}


def test_healthy_procedure_decays_without_retiring():
    p1, p2, p3, p4 = _patched([HEALTHY])
    with p1, p2, p3, p4 as mock_retire_items:
        result = handler({}, None)
    assert result["decayed"] == 1
    assert result["retired"] == 0
    assert result["failed"] == 0
    mock_retire_items.assert_not_called()


def test_zero_attempt_procedure_is_retired_and_orphans_cleared():
    p1, p2, p3, p4 = _patched([DEAD], retired_item_count=2)
    with p1, p2, p3 as mock_update, p4 as mock_retire_items:
        result = handler({}, None)
    assert result["decayed"] == 1
    assert result["retired"] == 1
    assert result["orphaned_items_retired"] == 2
    mock_retire_items.assert_called_once_with("fake-conn", "p-dead")
    call_args = mock_update.call_args.args
    assert call_args[1] == "p-dead"
    assert call_args[3] is True  # retire flag


def test_one_bad_procedure_does_not_block_the_rest():
    rows = [HEALTHY, DEAD]
    p1, p2, p3, p4 = _patched(rows)
    with p1, p2, p3 as mock_update, p4:
        mock_update.side_effect = [Exception("boom"), None]
        result = handler({}, None)
    assert result["failed"] == 1
    assert result["decayed"] == 1
    assert result["candidates"] == 2


def test_decayed_confidence_matches_agent_scoring_formula():
    """Canary: workers/common/scoring.py's copy must stay byte-for-byte in lockstep with
    agent/memory/scoring.py's own wilson_lb + the identical 90-day exp decay -- see
    workers/common/scoring.py's own module docstring for why this is duplicated, not imported."""
    from agent.memory.scoring import wilson_lb as agent_wilson_lb
    from math import exp

    for successes, attempts, age_days in [(47, 50, 0.0), (1, 1, 30.0), (0, 0, 10.0), (5, 8, 90.0)]:
        expected = agent_wilson_lb(successes, attempts) * exp(-age_days / 90)
        assert decayed_confidence(successes, attempts, age_days) == expected


def test_confidence_floor_matches_invariant_9():
    assert CONFIDENCE_FLOOR == 0.15
