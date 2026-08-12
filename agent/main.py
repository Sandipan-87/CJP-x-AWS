"""Engram · agent/main.py — ECS Fargate entrypoint: SQS consumer, signals, health.  [BRAINS]

design/02-low-level-design.md §1 (file tree: "entrypoint: config, pools, queue
consumer, signals, health"), §2 (config contract), §4 (signal handling), §12
("Health endpoint: GET /health -> DB ping, lease round-trip, provider
self-test results"). This is the first real caller of `build_graph(telemetry=
Telemetry(), ...)` and the first place a REAL (non-`override_backup_gate`)
backup-gate check runs end to end — both named explicitly on CLAUDE.md's own
"Next action" list (Session 34).

Run as a module, not a script — `python -m agent.main` from the repo root
(the ECS container's `CMD`, once one exists). Running `python agent/main.py`
directly would put `agent/` itself on `sys.path[0]` instead of the repo
root, breaking every `from agent.xxx import yyy` import in this file and
everything it pulls in.

REAL DESIGN DECISIONS MADE HERE, none frozen anywhere upstream — recorded so
the next session doesn't have to re-derive them:

1. **`thread_id` = a deterministic function of the incident fingerprint, not
   a fresh UUID.** `agent/graph.py`'s own docstring (Session 27) flags an
   unresolved tension: a LangGraph `thread_id` must exist *before*
   `ainvoke()` runs, but the real `task_id` doesn't exist until `observe
   (node)` dedupes the incident *during* the run — so nothing could
   previously reconcile the two. The fingerprint, though, is computable
   from `query_text` alone, before the graph ever starts (same pure
   `normalize_query_text`/`fingerprint` functions `observe(node)` itself
   uses — imported here, never reimplemented). Using `thread_id =
   f"tid-{fingerprint}"` makes `thread_id` stable across every redelivery
   of the same logical incident, which is exactly what makes LangGraph's
   checkpoint actually resumable after an `aws ecs stop-task` kill: a new
   consumer, redelivered the same (or an equivalent re-probed) incident,
   recomputes the identical `thread_id` and picks the same checkpoint back
   up — no coordination needed beyond the fingerprint itself.
2. **The `tasks.checkpoint_thread_id` write-back happens BEFORE `ainvoke()`,
   not after.** Both `thread_id` and (via a pre-insert, see #3) `task_id`
   are known upfront for an incident, so `db.set_checkpoint_thread_id()`
   (new this session) runs immediately after the pre-insert — if the
   process dies before the graph even starts, the reconciliation row is
   already correct, not dependent on the run completing.
3. **A task row is pre-inserted via `db.insert_task()` before the lease is
   acquired, for incidents only.** `agent_leases.task_id` has a hard FK to
   `tasks(task_id)` (`db/migrations/001_engram_schema.sql:51`) — a lease
   cannot be acquired before a real task row exists, and `observe(node)`
   doesn't create one until partway through the graph run. Pre-inserting
   with the SAME `(task_type, target_cluster_id, incident_fingerprint)`
   `observe(node)` will independently compute means `observe(node)`'s own
   `insert_incident_observation()` call dedupes onto this exact row
   (`tasks_active_incident_idx`) rather than creating a second one — proven
   dedup behavior (Session 19), just triggered by a second caller.
   **Deliberately NOT done for a non-incident (sweep) message**: the
   dedupe index only applies `WHERE task_type = 'incident'`
   (`001_engram_schema.sql:46`), so a sweep pre-insert would never dedupe
   against `observe(node)`'s own insert — it would just leave an orphaned
   extra row every cycle. Sweeps skip the pre-insert, the lease, and the
   `checkpoint_thread_id` write entirely; `observe(node)` still creates its
   own row and the graph still runs (so the observation is still recorded),
   just without lease protection — a sweep's whole lifetime is one cheap
   `EXPLAIN`, not worth resume machinery for.
4. **SQS ack semantics, decided here since neither HLD nor LLD specifies
   any**: a message is deleted (acked) on `"completed"` OR `"parked"` — a
   parked task is a defined, terminal, human-in-the-loop state (LLD §16:
   "park (human can retry)"); redelivering the same message would just
   re-hit the identical blocking condition (a bad `Proposal`, a refused
   backup gate) and burn a real LLM/API call for nothing. A message is left
   un-deleted on `"failed"` (an exception outside the typed `EngramError`
   taxonomy — an actual bug or a transient failure that taxonomy doesn't
   yet cover) so the queue's own visibility timeout and redrive/DLQ policy
   (an infra concern, not this file's) get a chance to retry or escalate it.
5. **The backup gate is real here, not `override_backup_gate=True`.**
   Every existing smoke test uses the override (no `CCLOUD_TOKEN` existed
   when they were written); it now does (Session 29). `build_runtime()`
   constructs a real `CloudApiAdapter` from `CCLOUD_TOKEN` and passes it as
   `backup_gate=`, `override_backup_gate=False` (the default) — the first
   place in this codebase a real backup-gate network call is reachable
   through the actual graph, not a standalone tool test.
6. **Message schema is invented here** — grep confirms no SQS queue, no
   EventBridge rule, and no publisher exist anywhere in this repo or its
   `infra/` CDK stacks (CLAUDE.md's own OPEN list already says so). The
   schema this file expects, until something real publishes to
   `ENGRAM_QUEUE_URL` and proves it wrong:
   `{"scope_id": str, "target_cluster_id": str, "table_name": str,
   "query_text": str, "trigger": "eventbridge"|"manual"|"webhook"}`.
   `main.py` itself runs `SqlProbe.explain_analyze(query_text)` against the
   target cluster to build the `ProbeResult` `observe(node)` needs — the
   message only says WHAT to check, not the measurement itself, matching
   `agent/graph.py`'s own `build_graph()` contract (`state["initial_probe"]`
   must be pre-seeded from outside the graph; `observe(node)`'s module
   docstring already frames this exact bridge, `probe_result_from_explain`,
   as existing for precisely this purpose).
7. **Health endpoint is a hand-rolled minimal HTTP/1.1 responder over
   `asyncio.start_server`, not a new web-framework dependency.** LLD §12
   wants `GET /health` to return DB ping + lease round-trip + provider
   self-test results for an ECS ALB target group — an ALB health check
   only needs a 200 with *some* body on any request line, so a full ASGI
   framework (`aiohttp`/`starlette`, neither currently a dependency
   anywhere in this repo) would be new weight for one endpoint. This
   re-checks DB reachability (`db.ping()`) on every request rather than
   caching the startup self-test result, since a target group polls this
   continuously and DB health can change after startup.

WHAT THIS SESSION DOES **NOT** DO, stated plainly:

- **No SQS queue, EventBridge rule, or ECS service/task-definition is
  created.** That is real, separate `infra/` CDK work (the same kind of
  work `infra/engram_infra/approvals_stack.py` already did for the
  Lambdas), not in scope for "build main.py." `consume_loop()` is written
  against `ENGRAM_QUEUE_URL` and is real, working code — it just has
  nothing to point at in AWS yet, so it cannot be live-verified end to end
  against a real queue this session. Everything downstream of "a message
  has been received" (probe, dedupe, lease, checkpoint reconciliation, the
  full graph run, the REAL backup gate, telemetry) **is** live-verified —
  `scripts/smoke_test_main.py` calls `process_message()` directly with a
  real message body, bypassing only the SQS transport itself.
- **MCP `list_clusters` and the S3 round-trip** (both named in LLD §2's
  startup self-test list) are skipped, not faked — no MCP adapter and no
  `agent/`-side S3 module exist anywhere in this codebase yet (both are
  long-standing, separately-tracked gaps, not new ones introduced here).
- **`ENGRAM_LEASE_TTL_S`** (LLD §2, default 60) is read but currently has
  nowhere to go: `db.py`'s lease SQL hardcodes `DEFAULT_LEASE_TTL_S = 60`
  directly into an f-string (Session 12's own fix for CockroachDB's
  `INTERVAL` literal not accepting a bound parameter) rather than accepting
  it as a call argument. Making that configurable is a real, separate
  change to `db.py`, out of scope here — this file reads the env var only
  so a future wiring has somewhere to read it from, and says so at the
  read site rather than silently dropping it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from typing import Any

from langchain_cockroachdb import AsyncCockroachDBSaver
from langgraph.graph.state import CompiledStateGraph

from agent.errors import EngramError
from agent.graph import build_graph
from agent.memory import leases
from agent.memory.db import Database
from agent.memory.leases import LeaseHandle
from agent.nodes.observe import (
    DEFAULT_LATENCY_THRESHOLD_MS,
    ProbeResult,
    fingerprint,
    normalize_query_text,
    probe_result_from_explain,
)
from agent.providers.base import EmbeddingProvider, LLMProvider
from agent.providers.cohere_embed import CohereEmbeddings
from agent.providers.ollama_cloud_llm import OllamaCloudLLM
from agent.telemetry import Telemetry
from agent.tools.cloud_api import CloudApiAdapter
from agent.tools.sql_operator import SqlOperator
from agent.tools.sql_probe import SqlProbe

logger = logging.getLogger("engram.main")

DEFAULT_HEALTH_PORT = 8080
DEFAULT_RECEIVE_WAIT_S = 20  # SQS long-poll ceiling


@dataclass
class Runtime:
    """Every long-lived, per-process dependency `process_message()` and the
    health/consume loops need. Constructed once in `build_runtime()`, torn
    down via the `AsyncExitStack` that built it — never re-constructed per
    message, matching every existing smoke test's "build once, invoke many
    times" pattern for the graph and its providers.
    """

    db: Database
    embed_provider: EmbeddingProvider
    llm: LLMProvider
    sql_probe: SqlProbe
    sql_operator: SqlOperator
    backup_gate: CloudApiAdapter
    telemetry: Telemetry
    graph: CompiledStateGraph
    holder_id: str
    lease_renew_s: float
    latency_threshold_ms: float


def _holder_id() -> str:
    """`agent_leases.holder_id`'s own column comment: "ECS task ARN /
    process id." No ECS service exists yet to expose a real task ARN via
    the container metadata endpoint — that's real follow-up once ECS
    infra exists, stated here rather than faked. `ECS_TASK_ARN` is checked
    first so this becomes correct for free the moment something sets it.
    """
    return os.environ.get("ECS_TASK_ARN") or f"local-{socket.gethostname()}-{os.getpid()}"


def _thread_id_for_fingerprint(fp: str) -> str:
    """See module docstring, decision #1."""
    return f"tid-{fp}"


def _dsn_with_sslrootcert(dsn: str, sslrootcert: str | None) -> str:
    """Same 3-line append `Database.connect`/`SqlProbe`/`SqlOperator` each
    already do inline — factored out here because `build_runtime()` needs
    it a fourth time, for `AsyncCockroachDBSaver.from_conn_string()`, which
    (unlike those three) has no `sslrootcert=` kwarg of its own to do this
    internally.
    """
    if sslrootcert and "sslrootcert=" not in dsn:
        sep = "&" if "?" in dsn else "?"
        return f"{dsn}{sep}sslrootcert={sslrootcert}"
    return dsn


def _classify_exception(exc: BaseException) -> str:
    """`EngramError` (the full §16 taxonomy) -> `"parked"`; anything else
    (a real bug, an uncategorized transient failure) -> `"failed"`. See
    module docstring, decision #4, for what each outcome means for the
    SQS message.
    """
    return "parked" if isinstance(exc, EngramError) else "failed"


def _initial_state(scope_id: str, target_cluster_id: str, trigger: str, probe_payload: dict) -> dict:
    """Same shape every existing smoke test already builds by hand
    (`scripts/smoke_test_graph.py`, `scripts/smoke_test_checkpointer.py`) —
    `task_id`/`incident_fingerprint`/etc. are placeholders `observe(node)`
    overwrites; nothing here is read back out except by the graph itself.
    """
    return {
        "task_id": "",
        "scope_id": scope_id,
        "target_cluster_id": target_cluster_id,
        "trigger": trigger,
        "phase": "pending",
        "observations": [],
        "incident_fingerprint": None,
        "recall_bundle": None,
        "proposal": None,
        "approval": None,
        "action": None,
        "measurement": None,
        "error": None,
        "model_meta": {},
        "initial_probe": dict(probe_payload),
    }


async def build_runtime(stack: AsyncExitStack) -> Runtime:
    """Constructs every dependency `build_graph()` needs, real telemetry
    included (decision context: this is the first real caller of
    `build_graph(telemetry=Telemetry(), ...)`), and registers every async
    context manager on `stack` so a single `async with AsyncExitStack()`
    in `main()` tears all of them down in reverse order on exit — same
    pattern every smoke test's `async with (A() as a, B() as b, ...):`
    already uses, just assembled dynamically instead of as one literal
    `with` statement.
    """
    db = await Database.connect(sslrootcert=os.environ.get("ENGRAM_MEMORY_SSLROOTCERT"))
    stack.push_async_callback(db.close)

    embed_provider = await stack.enter_async_context(CohereEmbeddings())
    llm = await stack.enter_async_context(OllamaCloudLLM())
    sql_probe = await stack.enter_async_context(
        SqlProbe(sslrootcert=os.environ.get("ENGRAM_TARGET_SSLROOTCERT"))
    )
    sql_operator = await stack.enter_async_context(
        SqlOperator(sslrootcert=os.environ.get("ENGRAM_TARGET_SSLROOTCERT"))
    )
    backup_gate = await stack.enter_async_context(CloudApiAdapter())
    memory_dsn = _dsn_with_sslrootcert(os.environ["ENGRAM_MEMORY_DSN"], os.environ.get("ENGRAM_MEMORY_SSLROOTCERT"))
    checkpointer = await stack.enter_async_context(AsyncCockroachDBSaver.from_conn_string(memory_dsn))

    telemetry = Telemetry()
    gate_timeout_s = float(os.environ.get("ENGRAM_APPROVAL_TIMEOUT_S", "600"))
    lease_renew_s = float(os.environ.get("ENGRAM_LEASE_RENEW_S", str(leases.DEFAULT_RENEW_INTERVAL_S)))
    # ENGRAM_LEASE_TTL_S is read for completeness with LLD §2's config table but currently has
    # nowhere to go -- see module docstring's final paragraph.
    os.environ.get("ENGRAM_LEASE_TTL_S", "60")

    graph = build_graph(
        db, embed_provider, llm, sql_probe, sql_operator,
        checkpointer=checkpointer,
        backup_gate=backup_gate,
        override_backup_gate=False,  # decision #5 -- the real gate, not the escape hatch
        gate_timeout_s=gate_timeout_s,
        telemetry=telemetry,
    )

    return Runtime(
        db=db, embed_provider=embed_provider, llm=llm, sql_probe=sql_probe, sql_operator=sql_operator,
        backup_gate=backup_gate, telemetry=telemetry, graph=graph,
        holder_id=_holder_id(), lease_renew_s=lease_renew_s,
        latency_threshold_ms=DEFAULT_LATENCY_THRESHOLD_MS,
    )


async def run_startup_checks(runtime: Runtime) -> None:
    """LLD §2's "Startup self-tests (fail fast, exit 1)": DB reachable,
    Cohere returns exactly 1024 dims, Ollama reachable, lease acquire/
    release round-trip. MCP `list_clusters` and the S3 round-trip are
    named in the same LLD list but SKIPPED here, not faked — see module
    docstring. Raises on the first failure; `main()` treats that as fatal.
    """
    await runtime.db.ping()
    logger.info("startup check: DB reachable")

    vectors = await runtime.embed_provider.embed(["engram startup self-test"], "search_document")
    if len(vectors[0]) != 1024:
        raise RuntimeError(f"startup check failed: Cohere returned {len(vectors[0])}-dim vector, expected 1024")
    logger.info("startup check: Cohere embeddings reachable, 1024-dim confirmed")

    await runtime.llm.complete("You are a health check.", [{"role": "user", "content": "reply with OK"}], [])
    logger.info("startup check: Ollama Cloud reachable")

    scratch_task_id = await runtime.db.insert_task("engram-startup-self-test", "manual", "manual")
    try:
        handle = await leases.acquire(runtime.db, scratch_task_id, runtime.holder_id, max_attempts=1)
        await handle.release()
        logger.info("startup check: lease acquire/release round-trip OK")
    finally:
        async with runtime.db._pool.connection() as conn, conn.cursor() as cur:  # noqa: SLF001
            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (scratch_task_id,))


async def process_message(runtime: Runtime, msg: dict[str, Any]) -> str:
    """Core per-message orchestration — no SQS in this function at all, so
    it's exactly what `scripts/smoke_test_main.py` and unit tests call
    directly. Returns `"completed"`, `"parked"`, or `"failed"` (see module
    docstring, decision #4, for what each means for the caller's ack
    decision).
    """
    scope_id = msg["scope_id"]
    target_cluster_id = msg["target_cluster_id"]
    table_name = msg["table_name"]
    query_text = msg["query_text"]
    trigger = msg.get("trigger", "eventbridge")

    explain_result = await runtime.sql_probe.explain_analyze(query_text)
    probe = probe_result_from_explain(
        explain_result, query_text=query_text, table_name=table_name, target_cluster_id=target_cluster_id,
    )

    normalized = normalize_query_text(query_text)
    fp = fingerprint(normalized)
    incident = (
        probe["probe_latency_ms"] > runtime.latency_threshold_ms
        and probe["plan_has_seq_scan"]
        and bool(probe.get("index_candidate"))
    )
    thread_id = _thread_id_for_fingerprint(fp)
    config = {"configurable": {"thread_id": thread_id}}
    state = _initial_state(scope_id, target_cluster_id, trigger, probe)

    if not incident:
        # Sweep: no pre-insert, no lease -- see module docstring, decision #3.
        try:
            await runtime.graph.ainvoke(state, config=config)
            return "completed"
        except Exception as exc:  # noqa: BLE001 -- classified below, not swallowed
            outcome = _classify_exception(exc)
            logger.warning("sweep run for scope_id=%s ended %s: %s", scope_id, outcome, exc)
            return outcome

    task_id = await runtime.db.insert_task(
        scope_id, "incident", trigger, target_cluster_id=target_cluster_id, incident_fingerprint=fp,
    )
    await runtime.db.set_checkpoint_thread_id(task_id, thread_id)
    await runtime.db.update_task_status(task_id, "running")

    lease: LeaseHandle = await leases.acquire(
        runtime.db, task_id, runtime.holder_id, renew_interval_s=runtime.lease_renew_s,
    )
    try:
        ainvoke_task = asyncio.ensure_future(runtime.graph.ainvoke(state, config=config))
        lost_task = asyncio.ensure_future(lease.wait_until_lost())
        done, _pending = await asyncio.wait({ainvoke_task, lost_task}, return_when=asyncio.FIRST_COMPLETED)

        if lost_task in done and not ainvoke_task.done():
            ainvoke_task.cancel()
            with suppress(asyncio.CancelledError):
                await ainvoke_task
            logger.warning("lease lost mid-run for task_id=%s -- another holder reclaimed it", task_id)
            await runtime.db.update_task_status(task_id, "parked")
            return "parked"

        lost_task.cancel()
        with suppress(asyncio.CancelledError):
            await lost_task
        ainvoke_task.result()  # re-raises if the graph run itself failed

        await runtime.db.update_task_status(task_id, "completed")
        return "completed"

    except Exception as exc:  # noqa: BLE001 -- classified, not swallowed
        outcome = _classify_exception(exc)
        logger.warning("task_id=%s ended %s: %s", task_id, outcome, exc)
        with suppress(Exception):
            await runtime.db.update_task_status(task_id, outcome)
        return outcome

    finally:
        await lease.release()


async def _delete_message(sqs_client: Any, queue_url: str, receipt_handle: str) -> None:
    await asyncio.to_thread(sqs_client.delete_message, QueueUrl=queue_url, ReceiptHandle=receipt_handle)


async def consume_loop(runtime: Runtime, sqs_client: Any, queue_url: str, shutdown: asyncio.Event) -> None:
    """LLD §4's "queue consumer." Long-polls `ENGRAM_QUEUE_URL`, one message
    at a time (LLD §2 §4: "one incident at a time per task"). Stops pulling
    NEW messages once `shutdown` is set (SIGTERM path) but lets whatever
    `process_message()` call is already in flight finish naturally --
    `graph.ainvoke()` isn't cooperatively cancellable mid-node, and LLD §4
    already accounts for the case where 25s isn't enough: "SIGKILL -> lease
    expiry + checkpoint tables cover it."
    """
    while not shutdown.is_set():
        resp = await asyncio.to_thread(
            sqs_client.receive_message,
            QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=DEFAULT_RECEIVE_WAIT_S,
        )
        messages = resp.get("Messages", [])
        if not messages:
            continue

        raw = messages[0]
        try:
            body = json.loads(raw["Body"])
        except (KeyError, json.JSONDecodeError) as exc:
            # Permanently malformed -- no retry will ever fix this, but deleting it ourselves
            # would hide that a publisher is broken. Leave it for the queue's own redrive
            # policy (infra concern) to eventually DLQ.
            logger.error("malformed SQS message, leaving for redrive policy: %s", exc)
            continue

        outcome = await process_message(runtime, body)
        if outcome in ("completed", "parked"):
            await _delete_message(sqs_client, queue_url, raw["ReceiptHandle"])
        else:
            logger.warning("message left un-acked for redelivery/DLQ, outcome=%r", outcome)


async def _handle_health_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, runtime: Runtime) -> None:
    try:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), timeout=5.0)  # request line; method/path not inspected

        try:
            await runtime.db.ping()
            db_ok = True
        except Exception:  # noqa: BLE001 -- health probe, never propagate
            db_ok = False

        body = json.dumps({"status": "ok" if db_ok else "degraded", "db": db_ok}).encode("utf-8")
        status_line = b"HTTP/1.1 200 OK\r\n" if db_ok else b"HTTP/1.1 503 Service Unavailable\r\n"
        headers = (
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(status_line + headers + body)
        await writer.drain()
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def serve_health(runtime: Runtime, host: str, port: int, shutdown: asyncio.Event) -> None:
    """See module docstring, decision #7. Re-checks `db.ping()` per request
    rather than caching the startup result -- an ALB target group polls
    continuously and DB health can change after boot.
    """
    server = await asyncio.start_server(lambda r, w: _handle_health_request(r, w, runtime), host, port)
    logger.info("health endpoint listening on %s:%d", host, port)
    async with server:
        serve_task = asyncio.ensure_future(server.serve_forever())
        await shutdown.wait()
        serve_task.cancel()
        with suppress(asyncio.CancelledError):
            await serve_task


async def main() -> int:
    logging.basicConfig(level=os.environ.get("ENGRAM_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(signame: str) -> None:
        logger.info("received %s -- draining in-flight work, then exiting (LLD §4: SIGTERM path)", signame)
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except NotImplementedError:
            # Windows has no event-loop-level signal handlers for SIGTERM -- dev-only fallback.
            signal.signal(sig, lambda *_args: shutdown.set())

    async with AsyncExitStack() as stack:
        runtime = await build_runtime(stack)

        try:
            await run_startup_checks(runtime)
        except Exception as exc:  # noqa: BLE001 -- fatal, fail fast per LLD §2
            logger.critical("startup self-test failed, exiting: %s", exc)
            return 1
        logger.info("startup self-tests passed -- holder_id=%s", runtime.holder_id)

        health_port = int(os.environ.get("ENGRAM_HEALTH_PORT", str(DEFAULT_HEALTH_PORT)))
        running: list[asyncio.Task] = [
            asyncio.ensure_future(serve_health(runtime, "0.0.0.0", health_port, shutdown))  # noqa: S104
        ]

        queue_url = os.environ.get("ENGRAM_QUEUE_URL")
        if queue_url:
            import boto3  # lazy -- see agent/telemetry.py's identical rationale

            sqs_client = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            running.append(asyncio.ensure_future(consume_loop(runtime, sqs_client, queue_url, shutdown)))
        else:
            logger.warning(
                "ENGRAM_QUEUE_URL not set -- health endpoint only, no SQS consumption. "
                "No queue exists in AWS yet for this project (infra work, separate from this file)."
            )

        await shutdown.wait()
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
