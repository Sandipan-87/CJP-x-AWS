"""Engram · unit tests for agent/graph.py's pure routing logic.

_route_after_observe is the one part of graph.py testable without a
cluster/LangGraph runtime -- everything else in that module is exercised
live by scripts/smoke_test_graph.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END

from agent.graph import _route_after_observe


def test_routes_to_recall_when_incident_fingerprint_is_set():
    state = {"incident_fingerprint": "abc123"}
    assert _route_after_observe(state) == "recall"


def test_routes_to_end_when_incident_fingerprint_is_none():
    state = {"incident_fingerprint": None}
    assert _route_after_observe(state) == END


def test_routes_to_end_when_incident_fingerprint_is_absent():
    """.get() default -- a state dict missing the key entirely behaves the
    same as an explicit None, not a KeyError."""
    assert _route_after_observe({}) == END
