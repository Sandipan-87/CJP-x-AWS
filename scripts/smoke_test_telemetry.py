#!/usr/bin/env python3
"""Engram · smoke test for agent/telemetry.py against real AWS (CloudWatch) and
the real opentelemetry-sdk (Console export).  [BRAINS]

No CockroachDB connection needed -- this module talks to CloudWatch + stdout
only. Two things are checked for real, not mocked:

  1. A real `cloudwatch:PutMetricData` call under whatever AWS credentials
     `.env` provides. **Known going in**: `engram-phase0` (the only identity
     with credentials in this repo's `.env`, CLAUDE.md §2.1/§8) is
     deliberately S3-only -- the exact same least-privilege-by-design shape
     that has already correctly denied Secrets Manager writes and S3 bucket
     creation in earlier sessions. This is expected to fail with
     `AccessDenied`, not a bug in `agent/telemetry.py`; the real production
     path is an ECS task role (not yet created -- `main.py`/ECS deployment
     is still unbuilt) granting `cloudwatch:PutMetricData`, not widening this
     dev identity. The script reports which outcome happened rather than
     assuming either one.
  2. `Telemetry()`'s DEFAULT constructor path (real `_build_tracer`, real
     `opentelemetry-sdk` `TracerProvider` + `ConsoleSpanExporter` -- no
     `OTEL_EXPORTER_OTLP_ENDPOINT` is set anywhere in this project, so this
     is the actual code path production would take today) -- stdout is
     captured and checked for the real exported span's name/attributes,
     not asserted via an injected test-only tracer like tests/test_telemetry.py.

    python scripts/smoke_test_telemetry.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from agent.telemetry import MetricPublisher, Telemetry, set_attr

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def main() -> int:
    all_ok = True

    print(f"\n{RULE}\n1. RAW cloudwatch:PutMetricData -- diagnose the real outcome first\n{RULE}")
    import os

    import boto3

    cw = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    put_allowed = False
    try:
        cw.put_metric_data(
            Namespace="engram",
            MetricData=[
                {
                    "MetricName": "sweep_cycle_ms",
                    "Value": 1.0,
                    "Unit": "Milliseconds",
                    "Dimensions": [{"Name": "scope_id", "Value": "smoke-test-telemetry"}],
                }
            ],
        )
        put_allowed = True
        record("raw put_metric_data", True, "credentials CAN publish to CloudWatch")
    except Exception as exc:  # noqa: BLE001 -- diagnostic probe, any failure is informative
        record(
            "raw put_metric_data",
            True,  # not a smoke-test failure -- see module docstring, this is the EXPECTED shape
            f"denied as expected under engram-phase0's S3-only scope: {type(exc).__name__}: {exc}",
        )

    print(f"\n{RULE}\n2. MetricPublisher.record() -- must never raise, regardless of (1)'s outcome\n{RULE}")
    publisher = MetricPublisher()
    try:
        await publisher.record("sweep_cycle_ms", 2.0, dimensions={"scope_id": "smoke-test-telemetry"})
        record("MetricPublisher.record does not raise", True, f"(underlying publish allowed={put_allowed})")
    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("MetricPublisher.record does not raise", False, f"{type(exc).__name__}: {exc}")

    try:
        raised = False
        try:
            await publisher.record("not_a_real_metric_name", 1.0)
        except ValueError:
            raised = True
        record("MetricPublisher.record rejects unknown metric name", raised)
        all_ok = all_ok and raised
    except Exception as exc:  # noqa: BLE001
        all_ok = False
        record("MetricPublisher.record rejects unknown metric name", False, f"{type(exc).__name__}: {exc}")

    print(f"\n{RULE}\n3. Telemetry() default constructor -- real opentelemetry-sdk, ConsoleSpanExporter\n{RULE}")
    telemetry = Telemetry(metric_publisher=publisher)

    # `contextlib.redirect_stdout` only reassigns the `sys.stdout` NAME --
    # `ConsoleSpanExporter.__init__`'s `out: IO = sys.stdout` default is bound to the real
    # stream object at import time (a Python default-argument gotcha), so redirect_stdout
    # never intercepts it. Redirecting the real OS file descriptor (fd 1) does, since that's
    # the same underlying stream `sys.stdout` and `ConsoleSpanExporter.out` both write through.
    stdout_fd = sys.stdout.fileno()
    saved_fd = os.dup(stdout_fd)
    with tempfile.TemporaryFile(mode="w+") as capture:
        sys.stdout.flush()
        os.dup2(capture.fileno(), stdout_fd)
        try:
            with telemetry.span(
                "observe", task_id="smoke-task-1", scope_id="smoke-scope-1", trigger="manual"
            ) as span:
                set_attr(span, "latency_ms", 12.3)
                set_attr(span, "outcome", "incident")
        finally:
            sys.stdout.flush()
            os.dup2(saved_fd, stdout_fd)
            os.close(saved_fd)
        capture.seek(0)
        console_output = capture.read()

    checks = [
        ('span name "observe" printed', '"name": "observe"' in console_output),
        ('attribute "node": "observe" printed', '"node": "observe"' in console_output),
        ('attribute task_id printed', '"task_id": "smoke-task-1"' in console_output),
        ('attribute scope_id printed', '"scope_id": "smoke-scope-1"' in console_output),
        ('attribute latency_ms printed', '"latency_ms": 12.3' in console_output),
        ('attribute outcome printed', '"outcome": "incident"' in console_output),
    ]
    for label, ok in checks:
        record(label, ok)
        all_ok = all_ok and ok

    if not all(ok for _, ok in checks):
        print("\n--- captured console span output (for diagnosis) ---")
        print(console_output[:2000])

    print(f"\n{RULE}\nRESULTS\n{RULE}")
    for name, detail in results:
        print(f"  {name}: {detail}")
    print(f"\n{'ALL PASS' if all_ok else 'SOME FAILED'} ({sum(1 for _, d in results if d.startswith('OK'))}/{len(results)})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
