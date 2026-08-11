#!/usr/bin/env python3
"""Engram · smoke test for workers/approvals/handler.py against the REAL memory cluster.  [PLUMBER]

Seeds a real, disposable task/action/approval row, invokes the Lambda handler function directly
(no actual AWS Lambda/API Gateway involved -- this proves the handler + workers/common/db.py's
pg8000 path work against the real DB; scripts/deploy-time verification of the real HTTP endpoint
is a separate, later step once actually deployed), and cleans up afterward.

    python scripts/smoke_test_approvals_lambda.py --sslrootcert memory-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "workers"))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import psycopg

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def _seed(dsn: str) -> tuple[str, str]:
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO tasks (task_id, task_type, status, trigger, target_cluster_id, scope_id)
                   VALUES (gen_random_uuid(), 'incident', 'awaiting_approval', 'manual',
                           'lambda-smoke', 'lambda-smoke') RETURNING task_id"""
            )
            task_id = (await cur.fetchone())[0]
            await cur.execute(
                """INSERT INTO remediation_actions
                   (action_id, task_id, scope_id, target_cluster_id, action_kind, recipe_version,
                    parameters, rendered_sql, idempotency_key, status)
                   VALUES (gen_random_uuid(), %s, 'lambda-smoke', 'lambda-smoke', 'analyze_table',
                           'v1', '{}'::jsonb, 'ANALYZE lambda_smoke', %s, 'proposed')
                   RETURNING action_id""",
                (task_id, f"lambda-smoke-{uuid.uuid4().hex[:8]}"),
            )
            action_id = (await cur.fetchone())[0]
            await cur.execute(
                """INSERT INTO approvals (approval_id, task_id, action_id, status)
                   VALUES (gen_random_uuid(), %s, %s, 'pending') RETURNING approval_id""",
                (task_id, action_id),
            )
            approval_id = (await cur.fetchone())[0]
        return str(task_id), str(approval_id)
    finally:
        await conn.close()


async def _cleanup(dsn: str, task_id: str) -> None:
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
    finally:
        await conn.close()


def main(sslrootcert: str | None) -> int:
    memory_dsn = os.environ["ENGRAM_MEMORY_DSN"]
    if sslrootcert and "sslrootcert=" not in memory_dsn:
        sep = "&" if "?" in memory_dsn else "?"
        memory_dsn = f"{memory_dsn}{sep}sslrootcert={sslrootcert}"

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    print(f"{RULE}\nSETUP -- real disposable task/action/approval row\n{RULE}")
    task_id, approval_id = asyncio.run(_seed(memory_dsn))
    record("seeded a real pending approval", True, approval_id)

    from approvals.handler import handler  # noqa: E402  (path set up above)

    def call(aid: str, decision: str, by: str = "smoke-test") -> dict:
        event = {
            "httpMethod": "POST",
            "pathParameters": {"approval_id": aid},
            "body": json.dumps({"decision": decision, "by": by}),
        }
        return handler(event, None)

    all_ok = True
    try:
        print(f"\n{RULE}\nINVOCATIONS -- real handler calls against the real DB\n{RULE}")

        r = call(approval_id, "approve")
        record("approve a real pending approval -> 200", r["statusCode"] == 200, str(r))
        all_ok &= r["statusCode"] == 200

        r = call(approval_id, "approve")
        record("approve the same approval again -> 409", r["statusCode"] == 409, str(r))
        all_ok &= r["statusCode"] == 409

        r = call(str(uuid.uuid4()), "approve")
        record("approve an unknown approval_id -> 404", r["statusCode"] == 404, str(r))
        all_ok &= r["statusCode"] == 404

        r = handler({"httpMethod": "OPTIONS"}, None)
        record("OPTIONS preflight -> 204", r["statusCode"] == 204, str(r))
        all_ok &= r["statusCode"] == 204

        r = call(approval_id, "not-a-real-decision")
        record("malformed decision -> 400", r["statusCode"] == 400, str(r))
        all_ok &= r["statusCode"] == 400

    finally:
        print(f"\n{RULE}\nCLEANUP\n{RULE}")
        asyncio.run(_cleanup(memory_dsn, task_id))
        print(f"  deleted task_id={task_id} (cascades to action/approval)")

    print(f"\n{RULE}\nRESULT\n{RULE}")
    width = max((len(k) for k, _ in results), default=10)
    failures = [k for k, v in results if v.startswith("FAIL")]
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    print(f"\n  {len(results) - len(failures)}/{len(results)} checks passed")
    return 0 if all_ok and not failures else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sslrootcert", default=None)
    args = ap.parse_args()
    sys.exit(main(args.sslrootcert))
