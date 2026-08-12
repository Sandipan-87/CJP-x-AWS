"""Engram · unit tests for workers/metrics/handler.py -- window parsing, the ListMetrics-then-
GetMetricData plumbing, the 30s cache, and the opt-in queue_depth/task_restarts behavior. Mocked
boto3 CloudWatch client throughout -- the real-CloudWatch path (proving the plumbing against
real AWS data, since nothing publishes the `engram` namespace yet) is a live smoke test against
the deployed Lambda, not a unit test.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workers"))

import metrics.handler as metrics_handler  # noqa: E402
from metrics.handler import handler  # noqa: E402


def _event(window: str | None = None, method: str = "GET") -> dict:
    return {"httpMethod": method, "queryStringParameters": {"window": window} if window else None}


def _fake_cw(list_metrics_result=None, get_metric_data_result=None):
    cw = MagicMock()
    cw.list_metrics.return_value = list_metrics_result or {"Metrics": []}
    cw.get_metric_data.return_value = get_metric_data_result or {"MetricDataResults": []}
    return cw


def setup_function():
    metrics_handler._cache.clear()


# --------------------------------------------------------------------- CORS

def test_options_preflight_is_204():
    assert handler({"httpMethod": "OPTIONS"}, None)["statusCode"] == 204


# ---------------------------------------------------------------- window parsing

def test_invalid_window_is_400():
    r = handler(_event("not-a-window"), None)
    assert r["statusCode"] == 400


def test_default_window_used_when_absent():
    with patch("boto3.client", return_value=_fake_cw()):
        r = handler(_event(None), None)
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["window"] == "1h"


# ------------------------------------------------------- engram-namespace metrics

def test_no_published_metrics_returns_empty_series_for_every_engram_metric():
    """The honest current-state case: ListMetrics finds nothing (nothing publishes to the
    `engram` namespace yet), so every metric name comes back with an empty list, not an error."""
    with patch("boto3.client", return_value=_fake_cw()):
        r = handler(_event("1h"), None)
    body = json.loads(r["body"])
    assert set(body["metrics"].keys()) >= set(metrics_handler.ENGRAM_METRICS.keys())
    assert all(body["metrics"][name] == [] for name in metrics_handler.ENGRAM_METRICS)


def test_a_published_metric_is_discovered_and_fetched():
    cw = _fake_cw(
        list_metrics_result={"Metrics": [{"Dimensions": [{"Name": "scope_id", "Value": "s1"}]}]},
        get_metric_data_result={
            "MetricDataResults": [
                {
                    "Id": "m0_0",
                    "Timestamps": [datetime(2026, 1, 1, tzinfo=timezone.utc)],
                    "Values": [0.75],
                }
            ]
        },
    )
    with patch("boto3.client", return_value=cw):
        r = handler(_event("1h"), None)
    body = json.loads(r["body"])
    series = body["metrics"]["recall_hit_rate"]
    assert len(series) == 1
    assert series[0]["dimensions"] == {"scope_id": "s1"}
    assert series[0]["datapoints"][0]["value"] == 0.75


# --------------------------------------------------------------- optional metrics

def test_queue_depth_omitted_without_env_var(monkeypatch):
    monkeypatch.delenv("ENGRAM_SQS_QUEUE_NAME", raising=False)
    with patch("boto3.client", return_value=_fake_cw()):
        r = handler(_event("1h"), None)
    body = json.loads(r["body"])
    assert "queue_depth" not in body["metrics"]
    assert any("queue_depth" in note for note in body["omitted"])


def test_queue_depth_included_with_env_var(monkeypatch):
    monkeypatch.setenv("ENGRAM_SQS_QUEUE_NAME", "engram-commands")
    cw = _fake_cw(get_metric_data_result={"MetricDataResults": [{"Id": "opt", "Timestamps": [], "Values": []}]})
    with patch("boto3.client", return_value=cw):
        r = handler(_event("1h"), None)
    body = json.loads(r["body"])
    assert "queue_depth" in body["metrics"]
    assert not any("queue_depth" in note for note in body["omitted"])


# --------------------------------------------------------------------- caching

def test_repeat_request_within_ttl_is_served_from_cache():
    cw = _fake_cw()
    with patch("boto3.client", return_value=cw):
        r1 = handler(_event("1h"), None)
        r2 = handler(_event("1h"), None)
    assert json.loads(r1["body"])["cached"] is False
    assert json.loads(r2["body"])["cached"] is True
    cw.get_metric_data.assert_not_called()  # ListMetrics found nothing, so this was never called anyway
    cw.list_metrics.assert_called()  # first request; second must NOT have re-called it
    assert cw.list_metrics.call_count == len(metrics_handler.ENGRAM_METRICS)  # only from request 1


def test_different_windows_are_cached_independently():
    with patch("boto3.client", return_value=_fake_cw()):
        r1 = handler(_event("1h"), None)
        r2 = handler(_event("30m"), None)
    assert json.loads(r1["body"])["cached"] is False
    assert json.loads(r2["body"])["cached"] is False
