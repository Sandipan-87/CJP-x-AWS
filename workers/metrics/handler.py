"""Engram · workers/metrics/handler.py — GET /metrics?window=1h.  [PLUMBER]

design/02-low-level-design.md §11.2 (`GET /metrics?window=1h`, API key, "CloudWatch
GetMetricData for §12 metrics", "cache 30s") + §12's metric table.

**Stated plainly, not glossed over: nothing in `agent/` publishes any `engram`-namespace
CloudWatch metric yet** (`agent/telemetry.py`, LLD's own named module for this, hasn't been
written -- CLAUDE.md's own OPEN list has said "no telemetry sink exists" since Session 24). This
endpoint's CloudWatch-querying plumbing is real and works against real CloudWatch data (verified
live against the `engram-approvals` Lambda's own AWS-managed `AWS/Lambda` invocation metrics,
which DO exist since it's been invoked for real this session) -- but querying the `engram`
namespace itself will correctly return empty datapoints until something publishes to it. That is
the honest, correct behavior for a dashboard with no data source wired up yet, not a bug in this
endpoint.

**Approach for the `engram`-namespace metrics (§12's `agent`-sourced ones): `ListMetrics` first,
`GetMetricData` second.** `GetMetricData` needs a metric's Dimensions fully specified per query
-- there's no "give me every scope_id combined" mode -- so this handler discovers which
dimension combinations actually exist for each metric name (via `ListMetrics`) and fetches each
one's datapoints. Right now `ListMetrics` returns nothing for the `engram` namespace, so every
one of those metrics correctly comes back with an empty `series` list.

**`queue_depth` (AWS/SQS) and `task_restarts` (AWS/ECS) are opt-in via env var, not
hardcoded:** neither an SQS queue nor an ECS service exists in this project yet (`main.py`, the
SQS consumer, isn't built -- CLAUDE.md's own OPEN list again). Omitted from the response (with a
`omitted` note explaining why) unless `ENGRAM_SQS_QUEUE_NAME` / `ENGRAM_ECS_SERVICE_NAME` +
`ENGRAM_ECS_CLUSTER_NAME` are set. **`task_restarts`'s exact CloudWatch metric name is itself an
assumption, flagged as such**: standard (non-Container-Insights) ECS service metrics don't
publish anything literally named "task restarts"; this uses `RunningTaskCount` as the closest
available proxy, unverified against a real ECS service since none exists to check against.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

ALLOWED_ORIGIN = os.environ.get("ENGRAM_DASHBOARD_ORIGIN", "*")
CACHE_TTL_S = 30
DEFAULT_WINDOW = "1h"

# LLD §12's own table -- name -> (Namespace, Unit). All "agent"-sourced (custom) metrics share
# the "engram" namespace per the table's "engram/count" / "engram/ms" / "engram/seconds" column.
ENGRAM_METRICS: dict[str, str] = {
    "recall_hit_rate": "Count",
    "time_to_remediation": "Seconds",
    "memory_recall_latency_p99": "Milliseconds",
    "blocked_by_backup_gate": "Count",
    "exactly_once_conflicts_detected": "Count",
    "llm_latency_ms": "Milliseconds",
    "llm_failures": "Count",
    "llm_token_usage": "Count",
    "sweep_cycle_ms": "Milliseconds",
}

_cache: dict[str, tuple[float, dict]] = {}

_WINDOW_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Methods": "GET,OPTIONS",
        },
        "body": json.dumps(body),
    }


def _parse_window(raw: str) -> timedelta | None:
    match = _WINDOW_RE.match(raw)
    if not match:
        return None
    count, unit = match.groups()
    return timedelta(seconds=int(count) * _UNIT_SECONDS[unit])


def _fetch_engram_metrics(cw: Any, start: datetime, end: datetime) -> dict[str, list[dict]]:
    """Returns {metric_name: [{"dimensions": {...}, "datapoints": [...]}]} -- one entry per
    distinct dimension combination `ListMetrics` reports for that name, each with its own
    `GetMetricData` result. Empty list for a metric name = nothing has published it (the honest
    current state for every `engram`-namespace metric, per this module's own docstring).
    """
    results: dict[str, list[dict]] = {name: [] for name in ENGRAM_METRICS}

    queries = []
    query_meta: dict[str, tuple[str, dict]] = {}
    for i, (name, unit) in enumerate(ENGRAM_METRICS.items()):
        listed = cw.list_metrics(Namespace="engram", MetricName=name).get("Metrics", [])
        for j, metric in enumerate(listed):
            qid = f"m{i}_{j}"
            dims = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
            query_meta[qid] = (name, dims)
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "engram",
                            "MetricName": name,
                            "Dimensions": metric.get("Dimensions", []),
                        },
                        "Period": 300,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                }
            )

    if not queries:
        return results

    # GetMetricData caps at 500 queries/call -- LLD's own metric list is 9 names, nowhere close.
    resp = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
    for result in resp.get("MetricDataResults", []):
        name, dims = query_meta[result["Id"]]
        datapoints = [
            {"timestamp": ts.isoformat(), "value": val}
            for ts, val in zip(result.get("Timestamps", []), result.get("Values", []))
        ]
        results[name].append({"dimensions": dims, "datapoints": datapoints})

    return results


def _fetch_optional_metric(
    cw: Any, namespace: str, metric_name: str, dimensions: list[dict], start: datetime, end: datetime
) -> list[dict]:
    resp = cw.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "opt",
                "MetricStat": {
                    "Metric": {"Namespace": namespace, "MetricName": metric_name, "Dimensions": dimensions},
                    "Period": 300,
                    "Stat": "Average",
                },
                "ReturnData": True,
            }
        ],
        StartTime=start,
        EndTime=end,
    )
    result = resp["MetricDataResults"][0]
    return [
        {"timestamp": ts.isoformat(), "value": val}
        for ts, val in zip(result.get("Timestamps", []), result.get("Values", []))
    ]


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = event.get("httpMethod", "GET")
    if method == "OPTIONS":
        return _response(204, {})

    window_str = (event.get("queryStringParameters") or {}).get("window") or DEFAULT_WINDOW
    window = _parse_window(window_str)
    if window is None:
        return _response(400, {"error": "window must look like '1h', '30m', '24h', '90s'"})

    now = time.time()
    cached = _cache.get(window_str)
    if cached and now - cached[0] < CACHE_TTL_S:
        body = dict(cached[1])
        body["cached"] = True
        return _response(200, body)

    import boto3  # imported lazily -- keeps unit tests from needing real AWS credentials to import this module

    cw = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    end = datetime.now(timezone.utc)
    start = end - window

    metrics = _fetch_engram_metrics(cw, start, end)

    omitted: list[str] = []
    queue_name = os.environ.get("ENGRAM_SQS_QUEUE_NAME")
    if queue_name:
        metrics_data = _fetch_optional_metric(
            cw, "AWS/SQS", "ApproximateNumberOfMessagesVisible",
            [{"Name": "QueueName", "Value": queue_name}], start, end,
        )
        metrics["queue_depth"] = [{"dimensions": {"QueueName": queue_name}, "datapoints": metrics_data}]
    else:
        omitted.append("queue_depth: ENGRAM_SQS_QUEUE_NAME not set (no SQS queue provisioned yet)")

    service_name = os.environ.get("ENGRAM_ECS_SERVICE_NAME")
    cluster_name = os.environ.get("ENGRAM_ECS_CLUSTER_NAME")
    if service_name and cluster_name:
        metrics_data = _fetch_optional_metric(
            cw, "AWS/ECS", "RunningTaskCount",  # UNVERIFIED proxy for "task restarts" -- see module docstring
            [{"Name": "ServiceName", "Value": service_name}, {"Name": "ClusterName", "Value": cluster_name}],
            start, end,
        )
        metrics["task_restarts"] = [
            {"dimensions": {"ServiceName": service_name, "ClusterName": cluster_name}, "datapoints": metrics_data}
        ]
    else:
        omitted.append(
            "task_restarts: ENGRAM_ECS_SERVICE_NAME/ENGRAM_ECS_CLUSTER_NAME not set "
            "(no ECS service provisioned yet)"
        )

    body = {
        "window": window_str,
        "generated_at": end.isoformat(),
        "cached": False,
        "metrics": metrics,
        "omitted": omitted,
    }
    _cache[window_str] = (now, body)
    return _response(200, body)
