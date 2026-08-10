"""Engram · T1 (design/02-low-level-design.md §14) — scoring unit tests.

Pure functions, no DB/cluster needed — runs anywhere: `pytest tests/test_scoring.py`.
"""

from __future__ import annotations

import sys
from math import isclose
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.memory.scoring import (
    CONFIDENCE_FLOOR,
    entity_affinity,
    hybrid,
    recency,
    wilson_lb,
)


def test_wilson_lb_1_of_1_does_not_outrank_47_of_50():
    """The LLD's own stated invariant #10 example — a perfect but tiny
    sample must lose to a large, slightly-imperfect one."""
    assert wilson_lb(1, 1) < wilson_lb(47, 50)


def test_wilson_lb_zero_attempts_is_zero():
    assert wilson_lb(0, 0) == 0.0


def test_wilson_lb_more_attempts_same_ratio_increases_confidence():
    """Tighter confidence interval as the sample grows, ratio held fixed."""
    assert wilson_lb(1, 2) < wilson_lb(50, 100) < wilson_lb(500, 1000)


def test_wilson_lb_bounded_0_to_1():
    for successes, attempts in [(0, 1), (1, 1), (1, 100), (99, 100), (1000, 1000)]:
        v = wilson_lb(successes, attempts)
        assert 0.0 <= v <= 1.0


def test_recency_decay_monotonicity():
    """Strictly decreasing in age; recency(0) == 1.0 exactly."""
    assert recency(0.0) == 1.0
    ages = [0.0, 1.0, 7.0, 14.0, 30.0, 90.0]
    values = [recency(a) for a in ages]
    assert values == sorted(values, reverse=True)
    assert values[-1] > 0.0  # asymptotic, never hits exactly zero


def test_entity_affinity_full_overlap_is_one():
    assert entity_affinity({"a", "b"}, {"a", "b"}) == 1.0


def test_entity_affinity_no_overlap_is_zero():
    assert entity_affinity({"a"}, {"b"}) == 0.0


def test_entity_affinity_both_empty_is_zero_not_one():
    """Empty/empty means no shared context to reward -- not a perfect match."""
    assert entity_affinity(set(), set()) == 0.0


def test_entity_affinity_partial_overlap_is_jaccard():
    # {a,b} & {b,c} = {b} (1); {a,b} | {b,c} = {a,b,c} (3)
    assert isclose(entity_affinity({"a", "b"}, {"b", "c"}), 1 / 3)


def _base_kwargs(**overrides):
    kwargs = dict(
        similarity=0.9,
        stored_confidence=0.8,
        successes=10,
        attempts=10,
        age_days=1.0,
        status="active",
        item_entities=set(),
        incident_entities=set(),
    )
    kwargs.update(overrides)
    return kwargs


def test_hybrid_hard_filter_below_confidence_floor():
    assert hybrid(**_base_kwargs(stored_confidence=CONFIDENCE_FLOOR - 0.01)) == float("-inf")


def test_hybrid_hard_filter_at_confidence_floor_is_not_filtered():
    """Floor is an exclusive lower bound (`< 0.15`), not inclusive."""
    assert hybrid(**_base_kwargs(stored_confidence=CONFIDENCE_FLOOR)) != float("-inf")


def test_hybrid_hard_filter_non_active_status():
    for status in ("draft", "retired"):
        assert hybrid(**_base_kwargs(status=status)) == float("-inf")


def test_hybrid_higher_similarity_scores_higher_all_else_equal():
    low = hybrid(**_base_kwargs(similarity=0.1))
    high = hybrid(**_base_kwargs(similarity=0.9))
    assert high > low


def test_hybrid_score_is_bounded_reasonably():
    """Weights sum to 1.0 and every term is bounded [0,1] (recency, wilson_lb,
    entity_affinity all are), so a passing hybrid() score should stay in [0,1]."""
    score = hybrid(**_base_kwargs())
    assert 0.0 <= score <= 1.0
