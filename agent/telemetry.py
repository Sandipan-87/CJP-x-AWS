"""Engram · agent/telemetry.py — OTel spans + CloudWatch metrics.  [BRAINS]

design/02-low-level-design.md's own file-tree comment ("OTel spans + CloudWatch
metrics (§12)") + §12's metric table + §12's span line: "OTel spans: one per
graph node; attributes task_id, scope_id, node, model_id, retrieved_count,
top1_score, latency_ms, outcome."

STATED, not hidden: this module existing does not, by itself, make `GET
/metrics` show real data — CLAUDE.md's own OPEN list has said so since
Session 24 ("nothing in agent/ publishes any engram-namespace metric yet").
Closing that gap needs BOTH this module AND something calling it, so this
session does both: `Telemetry` is built here, and every node
(`observe`/`recall`/`reason`/`gate`/`act_measure`) now accepts an optional
`telemetry: Telemetry | None = None` param — additive only, the exact same
pattern `agent/graph.py`'s `checkpointer` param already established (Session
27): passing `None` (the default, and every existing test's behavior) skips
telemetry entirely, so none of the pre-existing unit tests needed to change.
`agent/graph.py` itself does not yet construct a real `Telemetry()` — nothing
has wired a `main.py` entrypoint to build one — so real spans/metrics still
need that wiring; tracked as real follow-up, not done here.

CANONICAL METRIC TABLE mirrors `workers/metrics/handler.py`'s `ENGRAM_METRICS`
dict — the exact 9 names/units §12 lists — reimplemented independently on
purpose: `workers/` never imports `agent/` (Session 33's `pg8000`-vs-`psycopg3`
split is the same principle), so this is a second copy of the same LLD table,
not a shared import. A metric name outside this table is a caller bug (typo,
or a name the dashboard has no query for) — `MetricPublisher.record()` raises
`ValueError` immediately rather than silently publishing something `GET
/metrics` will never surface.

Two metrics the LLD's node-level prose names but §12's own table OMITS —
`gate_wait_ms` (§5.4 step 5) and `observations_written` (§5.1 step 5) — are
recorded as OTel span attributes only, not CloudWatch metrics, since there is
no dashboard query for either name. Stated here so the gap is a documented
choice, not a silent drop.

CloudWatch publish is best-effort: a `put_metric_data` failure (no
credentials, no network, throttling) is logged and swallowed, never raised —
telemetry must never break the agent's actual work, the same principle
`observe(node)` already applies to its own MCP/CloudWatch/ccloud collection
leg ("never fail the sweep on a single source"), applied here to the publish
side instead.

OTel spans use the standard `opentelemetry-api`/`sdk` (the latter is now a new
`requirements.txt` entry — needed to actually create exportable spans, not
just the bare API's default no-op tracer). No OTel collector or ADOT sidecar
exists anywhere in this project's infra yet, so the default exporter is
`ConsoleSpanExporter` — real, inspectable spans, just printed rather than
shipped to a backend. `OTEL_EXPORTER_OTLP_ENDPOINT` switches to a real OTLP
HTTP exporter IF `opentelemetry-exporter-otlp-proto-http` happens to be
installed; that package is deliberately NOT added to requirements.txt (no
collector endpoint is configured anywhere in this project to point it at yet)
— setting the env var without the package installed logs a warning and falls
back to console rather than crashing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger("engram.telemetry")

NAMESPACE = "engram"

# Mirrors workers/metrics/handler.py's ENGRAM_METRICS -- LLD §12's table, verbatim.
METRIC_UNITS: dict[str, str] = {
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


def elapsed_ms(t0: float) -> float:
    """`(time.perf_counter() - t0) * 1000` — the one-liner every node
    already computed its own way (see `recall.py`'s `_empty_bundle`); named
    here so telemetry call sites don't each re-derive it.
    """
    return (time.perf_counter() - t0) * 1000


class MetricPublisher:
    """CloudWatch `PutMetricData`, namespace fixed to `"engram"` (LLD §12).
    Lazy `boto3` import/client — same reason as `workers/metrics/handler.py`:
    importing this module should never require real AWS credentials.
    """

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # lazy -- see class docstring

            self._client = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        return self._client

    def _put(self, name: str, value: float, dimensions: dict[str, str]) -> None:
        client = self._get_client()
        client.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[
                {
                    "MetricName": name,
                    "Value": value,
                    "Unit": METRIC_UNITS[name],
                    "Timestamp": datetime.now(timezone.utc),
                    "Dimensions": [{"Name": k, "Value": str(v)} for k, v in sorted(dimensions.items())],
                }
            ],
        )

    async def record(self, name: str, value: float, *, dimensions: dict[str, str] | None = None) -> None:
        """Best-effort — see module docstring. Raises only `ValueError` for
        a name outside `METRIC_UNITS`, a caller bug, never a publish failure.
        """
        if name not in METRIC_UNITS:
            raise ValueError(f"{name!r} is not one of LLD §12's metrics: {sorted(METRIC_UNITS)}")
        try:
            await asyncio.to_thread(self._put, name, value, dimensions or {})
        except Exception:
            logger.warning("telemetry: failed to publish metric %r", name, exc_info=True)


def _build_tracer(service_name: str) -> Any:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            # BatchSpanProcessor is right here: batching genuinely reduces network requests
            # to a real collector -- unlike the console path below, this exporter has a
            # network cost worth amortizing.
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            return provider.get_tracer("engram")
        except ImportError:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-exporter-otlp-proto-http "
                "isn't installed -- falling back to ConsoleSpanExporter"
            )
    # SimpleSpanProcessor, not Batch: a real bug caught live (scripts/smoke_test_telemetry.py) --
    # BatchSpanProcessor defers export to a background thread on its own schedule (default 5s or
    # a full batch), so a span opened and closed inside one node call would print nothing until
    # long after the caller moved on, sometimes only at interpreter shutdown. Console export
    # exists for immediate dev visibility, which SimpleSpanProcessor actually gives; there is no
    # network cost here to amortize by batching.
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    return provider.get_tracer("engram")


class Telemetry:
    """Bundles the metric publisher + OTel tracer behind one optional node
    param. Held per-process (or per-test) — not module-global state, so
    tests can construct as many independent instances as they need.
    """

    def __init__(
        self,
        *,
        metric_publisher: MetricPublisher | None = None,
        service_name: str = "engram-agent",
        tracer: Any | None = None,
    ) -> None:
        self.metrics = metric_publisher or MetricPublisher()
        # `tracer=` is a test seam (mirrors CloudApiAdapter's `client=` pattern)
        # so tests can inject a tracer wired to an in-memory exporter instead
        # of going through `_build_tracer`'s real SDK/env-var wiring.
        self._tracer = tracer if tracer is not None else _build_tracer(service_name)

    async def record_metric(self, name: str, value: float, *, dimensions: dict[str, str] | None = None) -> None:
        await self.metrics.record(name, value, dimensions=dimensions)

    @contextmanager
    def span(
        self, node: str, *, task_id: str | None = None, scope_id: str | None = None, **attrs: Any
    ) -> Iterator[Any]:
        """LLD §12: "OTel spans: one per graph node." Yields the live span so
        callers can `set_attribute` values only known partway through the
        node (`model_id`, `retrieved_count`, `top1_score`, `latency_ms`,
        `outcome`) — the LLD names all of these as span attributes, but a
        node doesn't know most of them at span-open time.
        """
        attributes: dict[str, Any] = {"node": node, **attrs}
        if task_id is not None:
            attributes["task_id"] = str(task_id)
        if scope_id is not None:
            attributes["scope_id"] = str(scope_id)
        with self._tracer.start_as_current_span(node, attributes=attributes) as span:
            yield span


def maybe_span(telemetry: "Telemetry | None", node: str, **kw: Any):
    """`with maybe_span(telemetry, "observe", scope_id=scope_id):` — a node
    doesn't need an `if telemetry:` branch just to stay span-aware; when
    `telemetry` is `None` this yields `None` and does nothing, matching
    every node's existing `telemetry=None`-safe default exactly.
    """
    if telemetry is None:
        return nullcontext(None)
    return telemetry.span(node, **kw)


def set_attr(span: Any, key: str, value: Any) -> None:
    """`set_attr(span, "outcome", outcome)` — a no-op when `span` is `None`
    (the `maybe_span(None, ...)` case above)."""
    if span is not None:
        span.set_attribute(key, value)


async def maybe_record(
    telemetry: "Telemetry | None", name: str, value: float, *, dimensions: dict[str, str] | None = None
) -> None:
    """`await maybe_record(telemetry, "sweep_cycle_ms", elapsed_ms(t0), dimensions=...)`
    — a no-op when `telemetry` is `None`."""
    if telemetry is not None:
        await telemetry.record_metric(name, value, dimensions=dimensions)
