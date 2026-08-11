"""Engram · agent/schemas.py — pydantic-validated payloads.  [BRAINS]

design/02-low-level-design.md §3: `Proposal` is a "pydantic, frozen contract
Day 4" — deliberately NOT a `TypedDict` like everything in `agent/state.py`,
because it needs real validation (`agent/nodes/reason.py`'s schema-failure
repair loop), not just a JSON-serializable shape. Kept in its own module
rather than `agent/state.py` because pydantic validation is a different kind
of contract than a checkpoint-safe TypedDict, and because `ActionKind` is
also needed by the not-yet-written `agent/tools/recipe_renderer.py` (LLD
§10) — a shared home avoids that future module reaching into `agent/nodes/`.

`Evidence` and `Citation` are populated by `agent/nodes/reason.py` itself,
never by the LLM directly — see that module's docstring for why (the model
proposing its own falsification evidence would be grading its own homework).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ActionKind(str, Enum):
    """LLD §3 Proposal / §10 recipe renderer's allowlist."""

    create_index = "create_index"
    analyze_table = "analyze_table"


class Evidence(BaseModel):
    """LLD §3: "falsification: list[Evidence] # tool_call_id + result_summary
    + index-recommendation match." Computed by `reason(node)` from the
    real `EXPLAIN`/`SqlProbe` signal already captured in the triggering
    observation — never self-reported by the LLM.
    """

    tool_call_id: str | None = None
    result_summary: str
    index_recommendation_match: bool | None  # None = no recommendation existed to compare against


class Citation(BaseModel):
    """LLD §3: "citations: list[Citation] # memory_item_id + score + source."
    Populated by `reason(node)` from `AgentState.recall_bundle`, never
    invented by the LLM.
    """

    memory_item_id: str
    score: float
    source: str


class Proposal(BaseModel):
    """LLD §3, verbatim field set. `reasoning` is REQUIRED — audit-grade
    rationale INSIDE the validated JSON, never a vendor "thinking" channel
    (docs/external-constraints.md §3.1).
    """

    reasoning: str = Field(min_length=1)
    hypothesis: str
    falsification: list[Evidence]
    action_kind: ActionKind
    parameters: dict
    expected_effect: str
    risk: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0.0, le=1.0)
    citations: list[Citation]
