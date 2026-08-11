#!/usr/bin/env python3
"""Engram · one-time bootstrap for engram_approver (dashboard approvals Lambda).  [PLUMBER]

design/02-low-level-design.md §11.2. Neither `engram_agent` (too broad) nor `engram_reader`
(SELECT-only, migrations 002/005) can perform the CAS UPDATE the approvals endpoint needs --
this is a dedicated role, `db/migrations/006_approver_role.sql`, matching exactly what that one
Lambda does: SELECT + UPDATE on `approvals`, nothing else.

Same pattern as `bootstrap_reader_role.py`/`bootstrap_target_roles.py`: sets a random password,
writes `ENGRAM_APPROVER_DSN` into the repo-root `.env` (for local Lambda testing), then
LIVE-VERIFIES the actual privilege boundary using a real, disposable approval row -- not just
that the role exists.

ALSO creates (or updates) an AWS Secrets Manager secret holding the same DSN -- per HLD's
`secret/engram/*` convention, this is what the REAL deployed Lambda reads at runtime, not a
plaintext Lambda environment variable. `.env` is the local-dev equivalent only.

    python scripts/bootstrap_approver_role.py --sslrootcert memory-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import secrets
import sys
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import psycopg
from psycopg import sql

ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / ".env"
SECRET_NAME = "engram/approver-dsn"
RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


def _dsn_with_credentials(base_dsn: str, user: str, password: str) -> str:
    p = urlparse(base_dsn)
    netloc = f"{user}:{password}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _upsert_env_var(key: str, value: str) -> None:
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    prefix = f"{key}="
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}\n"
            ENV_PATH.write_text("".join(lines))
            return
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(f"{prefix}{value}\n")
    ENV_PATH.write_text("".join(lines))


async def _try_as(dsn: str, sslrootcert: str | None, stmt: str, params: tuple = ()) -> tuple[bool, str]:
    full_dsn = dsn
    if sslrootcert and "sslrootcert=" not in full_dsn:
        sep = "&" if "?" in full_dsn else "?"
        full_dsn = f"{full_dsn}{sep}sslrootcert={sslrootcert}"
    try:
        async with await psycopg.AsyncConnection.connect(full_dsn, autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(stmt, params)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _put_secret(dsn: str) -> tuple[bool, str]:
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return False, "boto3 not installed -- skipping Secrets Manager step (SQL role still provisioned)"

    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client(
        "secretsmanager",
        region_name=region,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    try:
        client.put_secret_value(SecretId=SECRET_NAME, SecretString=dsn)
        return True, f"updated existing secret {SECRET_NAME!r}"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceNotFoundException":
            try:
                client.create_secret(Name=SECRET_NAME, SecretString=dsn)
                return True, f"created new secret {SECRET_NAME!r}"
            except ClientError as exc2:
                return False, f"create_secret failed: {type(exc2).__name__}: {exc2}"
        return False, f"put_secret_value failed: {type(exc).__name__}: {exc}"


async def main(sslrootcert: str | None) -> int:
    admin_dsn = os.environ["ENGRAM_MEMORY_DSN"]
    full_admin_dsn = admin_dsn
    if sslrootcert and "sslrootcert=" not in full_admin_dsn:
        sep = "&" if "?" in full_admin_dsn else "?"
        full_admin_dsn = f"{full_admin_dsn}{sep}sslrootcert={sslrootcert}"

    print(f"{RULE}\nSTEP 1 -- confirm engram_approver exists (migration 006)\n{RULE}")
    conn = await psycopg.AsyncConnection.connect(full_admin_dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT rolname FROM pg_roles WHERE rolname = 'engram_approver'")
            exists = (await cur.fetchone()) is not None
        record("engram_approver role exists", exists)
        if not exists:
            print("  ABORT: run db/migrations/006_approver_role.sql first.")
            return 1

        print(f"\n{RULE}\nSTEP 2 -- set a fresh random password\n{RULE}")
        password = secrets.token_urlsafe(24)
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
                .format(sql.Identifier("engram_approver"), sql.Literal(password))
            )
        print("  password set for engram_approver (not printed)")

        approver_dsn = _dsn_with_credentials(admin_dsn, "engram_approver", password)
        _upsert_env_var("ENGRAM_APPROVER_DSN", approver_dsn)
        print("  wrote ENGRAM_APPROVER_DSN to .env (local dev / Lambda testing only)")

        print(f"\n{RULE}\nSTEP 3 -- AWS Secrets Manager (what the real deployed Lambda reads)\n{RULE}")
        ok, detail = _put_secret(approver_dsn)
        record(f"secret {SECRET_NAME!r} written", ok, detail)

        print(f"\n{RULE}\nSTEP 4 -- disposable task/action/approval row for live verification\n{RULE}")
        async with conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO tasks (task_id, task_type, status, trigger, target_cluster_id, scope_id)
                   VALUES (gen_random_uuid(), 'incident', 'awaiting_approval', 'manual',
                           'bootstrap-check', 'bootstrap-check')
                   RETURNING task_id"""
            )
            task_id = (await cur.fetchone())[0]
            await cur.execute(
                """INSERT INTO remediation_actions
                   (action_id, task_id, scope_id, target_cluster_id, action_kind, recipe_version,
                    parameters, rendered_sql, idempotency_key, status)
                   VALUES (gen_random_uuid(), %s, 'bootstrap-check', 'bootstrap-check',
                           'analyze_table', 'v1', '{}'::jsonb, 'ANALYZE bootstrap_check',
                           %s, 'proposed')
                   RETURNING action_id""",
                (task_id, f"bootstrap-approver-{secrets.token_hex(4)}"),
            )
            action_id = (await cur.fetchone())[0]
            await cur.execute(
                """INSERT INTO approvals (approval_id, task_id, action_id, status)
                   VALUES (gen_random_uuid(), %s, %s, 'pending')
                   RETURNING approval_id""",
                (task_id, action_id),
            )
            approval_id = (await cur.fetchone())[0]
        record("disposable approval row created", True, str(approval_id))

        print(f"\n{RULE}\nSTEP 5 -- live-verify the privilege boundary (measured, not assumed)\n{RULE}")

        ok, detail = await _try_as(
            approver_dsn, sslrootcert,
            "SELECT status FROM approvals WHERE approval_id = %s", (approval_id,),
        )
        record("engram_approver: SELECT approvals succeeds", ok, detail)

        ok, detail = await _try_as(
            approver_dsn, sslrootcert,
            """UPDATE approvals SET status = 'approved', decided_by = 'bootstrap-check',
                   decided_at = now(), channel = 'dashboard'
               WHERE approval_id = %s AND status = 'pending'""",
            (approval_id,),
        )
        record("engram_approver: CAS UPDATE (pending -> approved) succeeds", ok, detail)

        ok, detail = await _try_as(
            approver_dsn, sslrootcert,
            """UPDATE approvals SET status = 'rejected' WHERE approval_id = %s AND status = 'pending'""",
            (approval_id,),
        )
        # Second CAS against the now-'approved' row must affect 0 rows -- but that's a rowcount
        # check the DB driver reports as success with rowcount=0, not an exception, so _try_as
        # (which only reports exceptions) will say "OK" here regardless. Real 404-vs-409 rowcount
        # logic lives in workers/approvals/handler.py's own tests, not this bootstrap script.

        ok, detail = await _try_as(
            approver_dsn, sslrootcert,
            "INSERT INTO approvals (approval_id, task_id, action_id, status) VALUES (gen_random_uuid(), %s, %s, 'pending')",
            (task_id, action_id),
        )
        record("engram_approver: INSERT correctly FAILS", not ok, detail)

        ok, detail = await _try_as(
            approver_dsn, sslrootcert, "SELECT * FROM tasks LIMIT 1"
        )
        record("engram_approver: SELECT on tasks (not granted) correctly FAILS", not ok, detail)

        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
        print(f"  cleaned up disposable rows (task_id={task_id}, cascades to action/approval)")

    finally:
        await conn.close()

    print(f"\n{RULE}\nRESULT\n{RULE}")
    width = max((len(k) for k, _ in results), default=10)
    failures = [k for k, v in results if v.startswith("FAIL")]
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    print(f"\n  {len(results) - len(failures)}/{len(results)} checks passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser()
    ap.add_argument("--sslrootcert", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.sslrootcert)))
