"""Engram · unit tests for agent/tools/sql_probe.py's plan-text parsers.

Fixtures below are the ACTUAL output captured against a real 20k-row
scenario table on the target cluster (2026-08-11), not invented text --
see sql_probe.py's module docstring for the raw capture.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools.sql_probe import _first_execution_time_ms, _first_index_candidate

REAL_ANALYZE_PLAN = """planning time: 388µs
execution time: 21ms
distribution: local
plan type: generic, re-optimized
rows decoded from KV: 20,000 (833 KiB, 1 gRPC calls)
regions: aws-us-east-1
estimated RUs consumed: 16.67195164729535

• filter
│ sql nodes: n1
│ regions: aws-us-east-1
│ execution time: 28µs
│ sql cpu time: 28µs
│ actual row count: 40
│ filter: customer_id = 42
│
└── • scan
      sql nodes: n1
      kv nodes: n163
      regions: aws-us-east-1
      KV time: 20ms
      KV rows decoded: 20,000
      sql cpu time: 5ms
      actual row count: 20,000
      missing stats
      table: smoke_orders_probe@smoke_orders_probe_pkey
      spans: FULL SCAN"""

REAL_EXPLAIN_PLAN = """distribution: local

• filter
│ filter: customer_id = 42
│
└── • scan
      missing stats
      table: smoke_orders_probe@smoke_orders_probe_pkey
      spans: FULL SCAN

index recommendations: 1
1. type: index creation
   SQL command: CREATE INDEX ON defaultdb.public.smoke_orders_probe (customer_id) STORING (amount);"""

NO_FULL_SCAN_PLAN = """planning time: 100µs
execution time: 1ms
distribution: local

• scan
      table: orders@orders_customer_id_idx
      spans: [/42 - /42]"""

NO_RECOMMENDATIONS_PLAN = """distribution: local

• scan
      table: orders@orders_customer_id_idx
      spans: [/42 - /42]"""


def test_execution_time_parses_ms():
    assert _first_execution_time_ms(REAL_ANALYZE_PLAN) == 21.0


def test_execution_time_takes_the_first_match_not_a_leaf_node():
    """The plan has TWO 'execution time' lines (summary: 21ms, filter node:
    28µs) -- the summary (first) one is the one that matters."""
    text = "execution time: 21ms\n...\nexecution time: 28µs\n..."
    assert _first_execution_time_ms(text) == 21.0


def test_execution_time_converts_microseconds():
    assert _first_execution_time_ms("execution time: 500µs") == 0.5


def test_execution_time_converts_seconds():
    assert _first_execution_time_ms("execution time: 2s") == 2000.0


def test_execution_time_none_when_absent():
    assert _first_execution_time_ms("no timing info here") is None


def test_index_candidate_parses_real_recommendation():
    assert _first_index_candidate(REAL_EXPLAIN_PLAN) == "customer_id"


def test_index_candidate_none_when_no_recommendations_section():
    assert _first_index_candidate(NO_RECOMMENDATIONS_PLAN) is None


def test_index_candidate_none_on_analyze_plan_by_design():
    """MEASURED: EXPLAIN ANALYZE never includes a recommendations section --
    this is exactly why explain_analyze() runs plain EXPLAIN separately."""
    assert _first_index_candidate(REAL_ANALYZE_PLAN) is None
