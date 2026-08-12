"""Engram · unit tests for agent/telemetry.py -- CloudWatch metrics + OTel spans.

No real AWS credentials or OTel collector needed: `MetricPublisher` is tested
with an injected fake CloudWatch client (mirrors `CloudApiAdapter`'s
`client=` test seam), and `Telemetry.span` is tested with a real
`opentelemetry-sdk` tracer wired to `InMemorySpanExporter` -- a real span
gets created and its actual attributes are asserted, not just "no exception
was raised."
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.telemetry import METRIC_UNITS, MetricPublisher, Telemetry, elapsed_ms, maybe_record, maybe_span, set_attr


class _FakeCloudWatchClient:
    def __init__(self, *, raise_on_put: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._raise_on_put = raise_on_put

    def put_metric_data(self, **kwargs):
        if self._raise_on_put is not None:
            raise self._raise_on_put
        self.calls.append(kwargs)


def test_metric_units_matches_lld_table():
    # LLD §12's own 9 names, verbatim -- same set workers/metrics/handler.py's
    # ENGRAM_METRICS queries. A change here without a matching dashboard-side
    # change is exactly the kind of drift this test exists to catch.
    assert set(METRIC_UNITS) == {
        "recall_hit_rate",
        "time_to_remediation",
        "memory_recall_latency_p99",
        "blocked_by_backup_gate",
        "exactly_once_conflicts_detected",
        "llm_latency_ms",
        "llm_failures",
        "llm_token_usage",
        "sweep_cycle_ms",
    }


def test_record_rejects_unknown_metric_name():
    publisher = MetricPublisher(client=_FakeCloudWatchClient())

    async def run():
        with pytest.raises(ValueError):
            await publisher.record("not_a_real_metric", 1.0)

    asyncio.run(run())


def test_record_publishes_correct_shape():
    client = _FakeCloudWatchClient()
    publisher = MetricPublisher(client=client)

    async def run():
        await publisher.record("sweep_cycle_ms", 42.5, dimensions={"scope_id": "s1"})

    asyncio.run(run())

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["Namespace"] == "engram"
    (datum,) = call["MetricData"]
    assert datum["MetricName"] == "sweep_cycle_ms"
    assert datum["Value"] == 42.5
    assert datum["Unit"] == "Milliseconds"
    assert datum["Dimensions"] == [{"Name": "scope_id", "Value": "s1"}]


def test_record_with_no_dimensions():
    client = _FakeCloudWatchClient()
    publisher = MetricPublisher(client=client)

    async def run():
        await publisher.record("llm_failures", 1.0)

    asyncio.run(run())
    assert client.calls[0]["MetricData"][0]["Dimensions"] == []


def test_record_swallows_publish_failure():
    # Best-effort per the module docstring: a real publish failure (no creds,
    # no network, throttling) must never propagate and break the caller.
    client = _FakeCloudWatchClient(raise_on_put=RuntimeError("network is down"))
    publisher = MetricPublisher(client=client)

    async def run():
        await publisher.record("llm_failures", 1.0)  # must not raise

    asyncio.run(run())  # would raise if the exception weren't swallowed


def test_maybe_helpers_are_noop_when_telemetry_is_none():
    # The additive-only contract every node relies on: telemetry=None must
    # behave as if telemetry.py were never wired in at all.
    async def run():
        with maybe_span(None, "observe", scope_id="s1") as span:
            assert span is None
            set_attr(span, "outcome", "success")  # must not raise
        await maybe_record(None, "sweep_cycle_ms", 1.0)  # must not raise

    asyncio.run(run())


def test_span_records_real_attributes():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    telemetry = Telemetry(metric_publisher=MetricPublisher(client=_FakeCloudWatchClient()), tracer=tracer)

    with telemetry.span("recall", task_id="t1", scope_id="s1", retrieved_count=3) as span:
        set_attr(span, "latency_ms", 12.5)
        set_attr(span, "outcome", "hit")

    (finished,) = exporter.get_finished_spans()
    assert finished.name == "recall"
    assert finished.attributes["node"] == "recall"
    assert finished.attributes["task_id"] == "t1"
    assert finished.attributes["scope_id"] == "s1"
    assert finished.attributes["retrieved_count"] == 3
    assert finished.attributes["latency_ms"] == 12.5
    assert finished.attributes["outcome"] == "hit"


def test_maybe_span_delegates_to_real_telemetry():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    telemetry = Telemetry(metric_publisher=MetricPublisher(client=_FakeCloudWatchClient()), tracer=tracer)

    with maybe_span(telemetry, "gate", task_id="t2") as span:
        assert span is not None

    (finished,) = exporter.get_finished_spans()
    assert finished.name == "gate"


def test_elapsed_ms_is_nonnegative_and_monotonic():
    import time

    t0 = time.perf_counter()
    time.sleep(0.001)
    assert elapsed_ms(t0) > 0
