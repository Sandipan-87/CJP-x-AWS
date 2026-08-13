"""Engram · unit tests for agent/graph.py's pure routing logic.

_route_after_observe, _route_after_gate, and _route_after_act_measure are
the parts of graph.py testable without a cluster/LangGraph runtime --
everything else in that module is exercised live by
scripts/smoke_test_graph.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.graph import END

from agent.graph import _route_after_act_measure, _route_after_gate, _route_after_observe


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


def test_routes_to_act_measure_when_gate_approved():
    assert _route_after_gate({"phase": "gate"}) == "act_measure"


def test_routes_to_reason_when_gate_replans():
    """LLD §4's gate -> reason re-plan edge -- a rejection with budget left."""
    assert _route_after_gate({"phase": "replan"}) == "reason"


def test_routes_to_end_when_gate_rejected_or_expired():
    assert _route_after_gate({"phase": "done"}) == END


def test_routes_to_end_when_phase_is_absent():
    assert _route_after_gate({}) == END


def test_act_measure_routes_to_reason_when_replanning():
    """LLD §4's act_measure -> reason re-plan edge -- a measured regression with budget left."""
    assert _route_after_act_measure({"phase": "replan"}) == "reason"


def test_act_measure_routes_to_end_when_done():
    assert _route_after_act_measure({"phase": "done"}) == END


def test_act_measure_routes_to_end_when_phase_is_absent():
    assert _route_after_act_measure({}) == END
