#!/usr/bin/env python3
"""Engram · scripts/demo_run.py — operator CLI for the real hackathon demo run.  [PLUMBER]

Drives both CLAUDE.md §1 demo beats against the REAL deployed system: the
live ECS Fargate agent (`engram-agent-cluster`/`engram-agent`), the real
`engram-commands.fifo` SQS queue, the real TARGET/MEMORY CockroachDB
clusters, and the real Vercel dashboard. This is deliberately NOT a smoke
test that calls `process_message()` directly (every `scripts/smoke_test_*.py`
in this repo does that instead) — the whole point of a rehearsal is to prove
the deployed pieces (real `aws ecs stop-task`, real SQS redelivery, real
dashboard clicks) work together, not just the Python code in isolation.

Subcommands (run `python scripts/demo_run.py <cmd> --help` for each one's
flags):

  build        create a scratch table on the TARGET cluster (no index), seed
                it with N rows (batched inserts — a single 1M+ row INSERT in
                one transaction hits a real CockroachDB lock-tracking limit,
                measured in an earlier session), and sanity-check the anomaly
                is real via a local EXPLAIN ANALYZE.
  send         send one real incident-shaped message to the live SQS queue —
                the exact schema agent/main.py's consume_loop() expects.
  watch         poll and print the live task/decisions/approvals/citations
                for a scope_id, once every 2s, until Ctrl+C — a terminal
                companion to the dashboard, not a replacement for it.
  ecs-status    print the live ECS service + task status (cluster/service/
                image digest/health) via the engram-deploy AWS identity.
  kill-task     stop the CURRENTLY RUNNING ECS task for real (the "it
                survives" kill switch) — ECS starts a replacement
                automatically (circuit-breaker-protected FargateService).
  drop          drop a scratch TARGET-cluster table. Memory-cluster rows
                (tasks/decisions/remediation_actions/memory_items/...) are
                deliberately NEVER deleted by this script — this project's
                own standing convention is that a real demo run is the real
                system doing its real job, not test debris to clean up.

Credentials: AWS calls use AWS_DEPLOY_ACCESS_KEY_ID/AWS_DEPLOY_SECRET_ACCESS_KEY
from .env (the `engram-deploy` identity — sqs:SendMessage/GetQueueUrl,
ecs:ListTasks/DescribeTasks/StopTask/DescribeServices are all already
granted, per CLAUDE.md's own change history). DB calls use ENGRAM_TARGET_DSN
(scratch tables) / ENGRAM_MEMORY_DSN (read-only watch queries) from .env.

The TARGET cluster is only reachable over port 26257 with the VPN connected
(CLAUDE.md's own standing note: a transparent squid proxy blocks it
otherwise) — confirm that BEFORE running `build`.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import os

import boto3
import psycopg

QUEUE_NAME = "engram-commands.fifo"
CLUSTER_NAME = "engram-agent-cluster"
SERVICE_NAME = "engram-agent"
LOG_GROUP = "/ecs/engram-agent"
BATCH_ROWS = 300_000  # a single >~1M-row INSERT...SELECT in one txn hits a real CockroachDB
# lock-tracking limit (ConfigurationLimitExceeded) -- measured directly in an earlier session.

RULE = "-" * 72


def _aws_session() -> boto3.Session:
    return boto3.Session(
        aws_access_key_id=os.environ["AWS_DEPLOY_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_DEPLOY_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def _cert_path() -> str:
    """Copies the shared CA cert to `tempfile.gettempdir()` (a real Windows path with no
    spaces, resolved by Python itself -- NOT bash's `/tmp`, which git-bash only translates
    for values that pass through argv, not literals embedded in a script). This repo's own
    directory has a space in it (`CJP x AWS`), and psycopg3's DSN-URI parser rejects an
    unescaped space in a query-string value (`sslrootcert=...`) -- measured directly this
    session, same class of issue a prior session hit applying a migration.
    """
    src = pathlib.Path(__file__).resolve().parent.parent / "workers" / "common" / "certs" / "memory-ca.crt"
    dst = pathlib.Path(tempfile.gettempdir()) / "engram-demo-memory-ca.crt"
    shutil.copyfile(src, dst)
    return str(dst)


def _dsn_with_cert(dsn: str, cert: str) -> str:
    if "sslrootcert=" in dsn:
        return dsn
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}sslrootcert={cert}"


def _target_dsn(cert: str) -> str:
    return _dsn_with_cert(os.environ["ENGRAM_TARGET_DSN"], cert)


def _memory_dsn(cert: str) -> str:
    return _dsn_with_cert(os.environ["ENGRAM_MEMORY_DSN"], cert)


# --------------------------------------------------------------------------- build


def cmd_build(args: argparse.Namespace) -> int:
    from agent.tools.sql_probe import SqlProbe

    cert = _cert_path()
    dsn = _target_dsn(cert)
    table = args.table
    rows = args.rows

    print(f"{RULE}\nbuild — table={table!r} rows={rows:,}\n{RULE}")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, customer_id INT)")
        print(f"  created {table} (id INT PRIMARY KEY, customer_id INT) -- no secondary index")

        inserted = 0
        while inserted < rows:
            batch = min(BATCH_ROWS, rows - inserted)
            lo, hi = inserted + 1, inserted + batch
            t0 = time.perf_counter()
            cur.execute(
                f"INSERT INTO {table} SELECT g, g %% 500 FROM generate_series(%s, %s) g", (lo, hi)
            )
            inserted += batch
            print(f"  inserted rows {lo:,}-{hi:,} ({inserted:,}/{rows:,}) in {time.perf_counter() - t0:.1f}s")

    print(f"\n{RULE}\nlocal sanity check — EXPLAIN ANALYZE (NOT the number the deployed agent will see)\n{RULE}")
    print(
        "  NOTE: this measures latency from THIS machine, not from inside AWS us-east-1 where\n"
        "  the deployed agent actually runs. An earlier session measured the SAME query/table\n"
        "  shape as materially FASTER from an AWS-co-located caller than from a dev machine far\n"
        "  from the cluster -- do not use this number to predict whether the deployed agent will\n"
        "  classify this as an incident. 1.5M rows was the figure that worked in that session;\n"
        "  this is a shape/plan sanity check only (full scan + a real index candidate), not a\n"
        "  timing guarantee."
    )

    import asyncio

    async def _check() -> None:
        async with SqlProbe(dsn=dsn) as probe:
            result = await probe.explain_analyze(f"SELECT * FROM {table} WHERE customer_id = 42")
        print(f"  local latency_ms={result.latency_ms:.1f}  has_full_scan={result.has_full_scan}  index_candidate={result.index_candidate!r}")
        if not (result.has_full_scan and result.index_candidate == "customer_id"):
            print("  WARNING: this table/query shape is not a clean full-scan-on-customer_id anomaly -- check before sending it.")

    asyncio.run(_check())
    print(f"\n  done. Send it with:\n    python scripts/demo_run.py send --table {table} --scope <your-scope-id>")
    return 0


# --------------------------------------------------------------------------- send


def cmd_send(args: argparse.Namespace) -> int:
    session = _aws_session()
    sqs = session.client("sqs")
    queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]

    message = {
        "scope_id": args.scope,
        "target_cluster_id": os.environ["ENGRAM_TARGET_CLUSTER_ID"],
        "table_name": args.table,
        "query_text": f"SELECT * FROM {args.table} WHERE customer_id = 42",
        "trigger": "manual",
    }
    print(f"{RULE}\nsend — queue={queue_url}\n{RULE}")
    print(json.dumps(message, indent=2))

    resp = sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(message),
        MessageGroupId=args.scope,
        MessageDeduplicationId=uuid.uuid4().hex,
    )
    print(f"\n  sent. MessageId={resp['MessageId']}")
    print(f"  now watch it: python scripts/demo_run.py watch --scope {args.scope}")
    print("  and open the dashboard: https://dashboard-five-chi-90.vercel.app")
    return 0


# --------------------------------------------------------------------------- watch


def _print_snapshot(cur, scope: str) -> None:
    cur.execute(
        "SELECT task_id, task_type, status, trigger, created_at, updated_at FROM tasks "
        "WHERE scope_id = %s ORDER BY created_at DESC LIMIT 3",
        (scope,),
    )
    tasks = cur.fetchall()
    print(f"\n[{time.strftime('%H:%M:%S')}] tasks (scope_id={scope}):")
    if not tasks:
        print("  (none yet)")
        return
    for task_id, task_type, status, trigger, created_at, updated_at in tasks:
        print(f"  task_id={task_id} type={task_type} status={status} trigger={trigger} updated_at={updated_at}")

    latest_task_id = tasks[0][0]

    cur.execute(
        "SELECT node, model_id, created_at FROM decisions WHERE task_id = %s ORDER BY created_at", (latest_task_id,)
    )
    print("  decisions:")
    for node, model_id, created_at in cur.fetchall():
        print(f"    {created_at}  {node:<8} model={model_id}")

    cur.execute(
        "SELECT item_id, similarity, source FROM v_recall_citations WHERE scope_id = %s ORDER BY similarity DESC LIMIT 5",
        (scope,),
    )
    citation_rows = cur.fetchall()
    if citation_rows:
        print("  recall citations (item_id, similarity, source):")
        for item_id, similarity, source in citation_rows:
            print(f"    {item_id}  similarity={similarity:.3f}  source={source}")

    cur.execute(
        "SELECT approval_id, status, requested_at, decided_at, decided_by "
        "FROM approvals WHERE task_id = %s ORDER BY requested_at DESC",
        (latest_task_id,),
    )
    approvals = cur.fetchall()
    if approvals:
        print("  approvals:")
        for approval_id, status, requested_at, decided_at, decided_by in approvals:
            print(f"    approval_id={approval_id} status={status} requested_at={requested_at} decided_by={decided_by}")
            if status == "pending":
                print("    >>> PENDING -- go click Approve/Reject in the dashboard's Approval Queue panel now <<<")

    cur.execute(
        "SELECT action_kind, status, outcome, rendered_sql FROM remediation_actions WHERE task_id = %s", (latest_task_id,)
    )
    actions = cur.fetchall()
    if actions:
        print("  remediation_actions:")
        for action_kind, status, outcome, rendered_sql in actions:
            print(f"    {action_kind}  status={status}  outcome={outcome}  sql={rendered_sql}")


def cmd_watch(args: argparse.Namespace) -> int:
    cert = _cert_path()
    dsn = _memory_dsn(cert)
    print(f"{RULE}\nwatch — scope_id={args.scope} (Ctrl+C to stop)\n{RULE}")
    with psycopg.connect(dsn, autocommit=True) as conn:
        try:
            while True:
                with conn.cursor() as cur:
                    _print_snapshot(cur, args.scope)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n  stopped.")
    return 0


# --------------------------------------------------------------------------- ecs-status / kill-task


def _current_tasks(ecs) -> list[dict]:
    arns = ecs.list_tasks(cluster=CLUSTER_NAME, serviceName=SERVICE_NAME)["taskArns"]
    if not arns:
        return []
    return ecs.describe_tasks(cluster=CLUSTER_NAME, tasks=arns)["tasks"]


def cmd_ecs_status(args: argparse.Namespace) -> int:
    session = _aws_session()
    ecs = session.client("ecs")

    svc = ecs.describe_services(cluster=CLUSTER_NAME, services=[SERVICE_NAME])["services"][0]
    print(f"{RULE}\nservice — {SERVICE_NAME}\n{RULE}")
    print(f"  status={svc['status']} desired={svc['desiredCount']} running={svc['runningCount']} pending={svc['pendingCount']}")

    tasks = _current_tasks(ecs)
    print(f"\n{RULE}\ntasks ({len(tasks)})\n{RULE}")
    for t in tasks:
        containers = t.get("containers", [])
        health = containers[0].get("healthStatus", "UNKNOWN") if containers else "UNKNOWN"
        image_digest = containers[0].get("imageDigest", "?") if containers else "?"
        print(f"  taskArn={t['taskArn']}")
        print(f"    lastStatus={t['lastStatus']} healthStatus={health} startedAt={t.get('startedAt')}")
        print(f"    imageDigest={image_digest}")
    return 0


def cmd_kill_task(args: argparse.Namespace) -> int:
    session = _aws_session()
    ecs = session.client("ecs")
    tasks = _current_tasks(ecs)
    running = [t for t in tasks if t["lastStatus"] == "RUNNING"]
    if not running:
        print("  no RUNNING task found -- run ecs-status first.")
        return 1
    task_arn = running[0]["taskArn"]
    print(f"{RULE}\nkill-task — stopping {task_arn}\n{RULE}")
    ecs.stop_task(cluster=CLUSTER_NAME, task=task_arn, reason="demo rehearsal: kill-and-resume beat")
    print("  stop_task requested. The ECS service will start a replacement automatically")
    print("  (circuit-breaker-protected FargateService) -- expect a new task ARN within ~30-45s;")
    print("  poll with: python scripts/demo_run.py ecs-status")
    print()
    print("  IMPORTANT: the in-flight SQS message this task was processing does NOT become")
    print("  visible to the new task immediately -- the queue's visibility timeout is 120s from")
    print("  the original receive, so there is a real wait (up to ~2 minutes total from when the")
    print("  message was first received) before the new task's consume_loop() picks it back up")
    print("  and resumes from its last checkpoint. This is expected, not a hang -- keep `watch`")
    print("  running and narrate the redelivery while it happens.")
    return 0


# --------------------------------------------------------------------------- drop


def cmd_drop(args: argparse.Namespace) -> int:
    cert = _cert_path()
    dsn = _target_dsn(cert)
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {args.table}")
    print(f"  dropped {args.table} on the TARGET cluster.")
    return 0


# --------------------------------------------------------------------------- CLI


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="create + seed a scratch TARGET table")
    p.add_argument("--table", required=True)
    p.add_argument("--rows", type=int, default=1_500_000)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("send", help="send one real incident message to the live SQS queue")
    p.add_argument("--table", required=True)
    p.add_argument("--scope", required=True)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("watch", help="poll+print live task/decisions/approvals/citations for a scope_id")
    p.add_argument("--scope", required=True)
    p.add_argument("--interval", type=float, default=2.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("ecs-status", help="print the live ECS service+task status")
    p.set_defaults(func=cmd_ecs_status)

    p = sub.add_parser("kill-task", help="stop the currently running ECS task for real")
    p.set_defaults(func=cmd_kill_task)

    p = sub.add_parser("drop", help="drop a scratch TARGET-cluster table")
    p.add_argument("--table", required=True)
    p.set_defaults(func=cmd_drop)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
