"""Engram · unit tests for agent/nodes/observe.py's pure helpers.

normalize_query_text, fingerprint, is_anomaly -- no DB/cluster needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.nodes.observe import ProbeResult, fingerprint, is_anomaly, normalize_query_text


def test_normalize_lowercases():
    assert normalize_query_text("SELECT * FROM Orders") == "select * from orders"


def test_normalize_collapses_number_literals():
    assert normalize_query_text("WHERE id = 12345") == "where id = ?"


def test_normalize_collapses_string_literals():
    assert normalize_query_text("WHERE name = 'bob'") == "where name = ?"
    assert normalize_query_text('WHERE name = "bob"') == "where name = ?"


def test_normalize_collapses_whitespace():
    assert normalize_query_text("SELECT   *\n\nFROM  orders") == "select * from orders"


def test_normalize_is_deterministic_across_different_literal_values():
    """The whole point: two queries differing only in literal VALUES must
    normalize (and therefore fingerprint) identically."""
    a = normalize_query_text("WHERE customer_id = 111")
    b = normalize_query_text("WHERE customer_id = 999999")
    assert a == b


def test_fingerprint_is_sha256_hex():
    fp = fingerprint("select * from orders where customer_id = ?")
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_is_deterministic():
    text = "select * from orders where customer_id = ?"
    assert fingerprint(text) == fingerprint(text)


def test_fingerprint_differs_for_different_queries():
    assert fingerprint("select 1") != fingerprint("select 2")


def _probe(**overrides) -> ProbeResult:
    base = dict(
        query_text="SELECT * FROM orders WHERE customer_id = 1",
        probe_latency_ms=5000.0,
        plan_has_seq_scan=True,
        index_candidate="customer_id",
        table_name="orders",
        target_cluster_id="cluster-1",
    )
    base.update(overrides)
    return ProbeResult(**base)


def test_is_anomaly_all_three_conditions_true():
    assert is_anomaly(_probe(), latency_threshold_ms=1000.0) is True


def test_is_anomaly_false_when_latency_below_threshold():
    assert is_anomaly(_probe(probe_latency_ms=5.0), latency_threshold_ms=1000.0) is False


def test_is_anomaly_false_when_no_seq_scan():
    assert is_anomaly(_probe(plan_has_seq_scan=False), latency_threshold_ms=1000.0) is False


def test_is_anomaly_false_when_no_index_candidate():
    """§5.1 step 3: all three must hold -- a slow seq scan with no index
    candidate is not something the agent can act on, so it's not flagged
    as an actionable incident by this rule."""
    assert is_anomaly(_probe(index_candidate=None), latency_threshold_ms=1000.0) is False
