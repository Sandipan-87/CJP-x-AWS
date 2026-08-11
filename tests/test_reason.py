"""Engram · unit tests for agent/nodes/reason.py's pure helpers AND its
control flow (retry-with-feedback, repair loop, final failure) via a
scripted fake LLMProvider + fake Database -- no real Ollama/cluster needed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.errors import LLMSchemaError
from agent.nodes.reason import _citations_from_bundle, _compute_falsification, reason
from agent.providers.base import LLMResult


def _obs(index_candidate):
    return {"payload": {"index_candidate": index_candidate}}


def test_falsification_match_when_columns_overlap():
    ev = _compute_falsification(_obs("customer_id"), ["customer_id"])
    assert ev.index_recommendation_match is True


def test_falsification_mismatch_when_no_overlap():
    ev = _compute_falsification(_obs("customer_id"), ["order_date"])
    assert ev.index_recommendation_match is False


def test_falsification_partial_overlap_counts_as_match():
    """Any shared column counts -- the recommendation doesn't have to be
    proposed as an exact, single-column match."""
    ev = _compute_falsification(_obs("customer_id, region"), ["customer_id", "region", "extra"])
    assert ev.index_recommendation_match is True


def test_falsification_none_when_no_recommendation_exists():
    """Missing recommendation is inconclusive, not a mismatch -- sweep-type
    observations may have no index_candidate at all."""
    ev = _compute_falsification(_obs(None), ["customer_id"])
    assert ev.index_recommendation_match is None


def test_falsification_none_observation():
    ev = _compute_falsification(None, ["customer_id"])
    assert ev.index_recommendation_match is None


def test_citations_empty_when_bundle_is_none():
    assert _citations_from_bundle(None) == []


def test_citations_empty_when_bundle_has_no_items():
    assert _citations_from_bundle({"items": []}) == []


def test_citations_built_from_bundle_items():
    bundle = {"items": [
        {"item_id": "abc", "hybrid_score": 0.83, "class": "episode"},
        {"item_id": "def", "similarity": 0.5, "class": "procedure"},  # no hybrid_score -- falls back
    ]}
    citations = _citations_from_bundle(bundle)
    assert len(citations) == 2
    assert citations[0].memory_item_id == "abc"
    assert citations[0].score == 0.83
    assert citations[0].source == "episode"
    assert citations[1].score == 0.5


# ---------------------------------------------------------------- control flow

class _FakeLLM:
    """Scripted responses, one per call to complete()."""

    def __init__(self, responses: list[LLMResult]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []  # each entry: the `messages` list at call time

    async def complete(self, system, messages, tools, schema=None):
        self.calls.append([dict(m) for m in messages])
        return self._responses.pop(0)


class _FakeDb:
    def __init__(self) -> None:
        self.decisions: list[dict] = []

    async def insert_decision(self, task_id, scope_id, node, *, model_id, reasoning, citations=None,
                               model_version=None, input_fingerprint=None):
        self.decisions.append({"task_id": task_id, "node": node, "reasoning": reasoning,
                                "citations": citations})
        return "fake-decision-id"


def _tool_call(action_kind="create_index", columns=("customer_id",), **overrides):
    args = {
        "reasoning": "the plan shows a full scan on a selective predicate",
        "hypothesis": "missing index on the filtered column",
        "action_kind": action_kind,
        "parameters": {"table": "orders", "columns": list(columns)},
        "expected_effect": "seq scan becomes an index scan",
        "risk": "low",
        "confidence": 0.8,
    }
    args.update(overrides)
    return LLMResult(text="", tool_calls=[{"name": "propose_remediation", "arguments": args}], usage={})


def _base_state(index_candidate="customer_id"):
    return {
        "task_id": "t1", "scope_id": "s1",
        "observations": [{"payload": {"text": "slow query", "latency_ms": 4300.0,
                                        "plan_has_seq_scan": True, "index_candidate": index_candidate}}],
        "recall_bundle": None,
    }


def test_reason_succeeds_first_round_when_columns_match():
    llm = _FakeLLM([_tool_call(columns=("customer_id",))])
    db = _FakeDb()

    async def run():
        return await reason(_base_state(), db, llm)

    update = asyncio.run(run())
    assert update["phase"] == "reason"
    assert update["proposal"]["action_kind"] == "create_index"
    assert len(llm.calls) == 1
    assert len(db.decisions) == 1


def test_reason_retries_with_feedback_on_falsification_mismatch_then_succeeds():
    llm = _FakeLLM([
        _tool_call(columns=("wrong_column",)),   # round 1: mismatch
        _tool_call(columns=("customer_id",)),    # round 2: matches after feedback
    ])
    db = _FakeDb()

    async def run():
        return await reason(_base_state(), db, llm)

    update = asyncio.run(run())
    assert update["proposal"]["action_kind"] == "create_index"
    assert len(llm.calls) == 2
    # round 2's message list must include feedback about the round-1 mismatch
    assert any("rejected" in m.get("content", "") for m in llm.calls[1])


def test_reason_raises_llm_schema_error_after_exhausting_rounds():
    llm = _FakeLLM([
        _tool_call(columns=("wrong_a",)),
        _tool_call(columns=("wrong_b",)),
        _tool_call(columns=("wrong_c",)),
    ])
    db = _FakeDb()

    async def run():
        await reason(_base_state(), db, llm, max_rounds=3)

    with pytest.raises(LLMSchemaError):
        asyncio.run(run())
    assert len(llm.calls) == 3
    assert len(db.decisions) == 0  # never persisted a decision for a failed proposal


def test_reason_treats_no_recommendation_as_acceptable_not_a_mismatch():
    """No index_candidate at all -> match is None, not False -> should NOT retry."""
    llm = _FakeLLM([_tool_call(columns=("anything",))])
    db = _FakeDb()

    async def run():
        return await reason(_base_state(index_candidate=None), db, llm)

    update = asyncio.run(run())
    assert update["phase"] == "reason"
    assert len(llm.calls) == 1


def test_reason_retries_when_model_answers_in_prose_with_no_tool_call():
    llm = _FakeLLM([
        LLMResult(text="I think you should add an index.", tool_calls=[], usage={}),
        _tool_call(columns=("customer_id",)),
    ])
    db = _FakeDb()

    async def run():
        return await reason(_base_state(), db, llm)

    update = asyncio.run(run())
    assert update["phase"] == "reason"
    assert len(llm.calls) == 2
