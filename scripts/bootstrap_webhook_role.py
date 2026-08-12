#!/usr/bin/env python3
"""Engram · one-time bootstrap for engram_webhook (alert-ingest Lambda).  [PLUMBER]

design/02-low-level-design.md §11.2 (`POST /webhooks/alerts`). Same pattern as
`bootstrap_approver_role.py`/`bootstrap_reader_role.py`/`bootstrap_target_roles.py`: sets a
random password, writes `ENGRAM_WEBHOOK_DSN` into the repo-root `.env` (local testing), then
LIVE-VERIFIES the actual privilege boundary using real, disposable rows -- not just that the
role exists.

Also generates a random HMAC secret for `POST /webhooks/alerts`' signature verification and
writes it to `.env` as `ENGRAM_WEBHOOK_HMAC_SECRET` -- and, like `bootstrap_approver_role.py`,
attempts to also push both the DSN and the HMAC secret into AWS Secrets Manager (what the real
deployed Lambda reads at runtime), tolerating `AccessDenied` under `engram-phase0` the same way.

    python scripts/bootstrap_webhook_role.py --sslrootcert memory-ca.crt
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
DSN_SECRET_NAME = "engram/webhook-dsn"
HMAC_SECRET_NAME = "engram/webhook-hmac-secret"
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


def _put_secret(name: str, value: str) -> tuple[bool, str]:
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return False, "boto3 not installed"

    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client(
        "secretsmanager",
        region_name=region,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    try:
        client.create_secret(Name=name, SecretString=value)
        return True, f"created secret {name!r}"
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ResourceExistsException":
            client.put_secret_value(SecretId=name, SecretString=value)
            return True, f"updated existing secret {name!r}"
        return False, f"{type(exc).__name__}: {exc}"


async def main(sslrootcert: str | None) -> int:
    admin_dsn = os.environ["ENGRAM_MEMORY_DSN"]
    full_admin_dsn = admin_dsn
    if sslrootcert and "sslrootcert=" not in full_admin_dsn:
        sep = "&" if "?" in full_admin_dsn else "?"
        full_admin_dsn = f"{full_admin_dsn}{sep}sslrootcert={sslrootcert}"

    print(f"{RULE}\nSTEP 1 -- confirm engram_webhook exists (migration 007)\n{RULE}")
    conn = await psycopg.AsyncConnection.connect(full_admin_dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT rolname FROM pg_roles WHERE rolname = 'engram_webhook'")
            exists = (await cur.fetchone()) is not None
        record("engram_webhook role exists", exists)
        if not exists:
            print("  ABORT: run db/migrations/007_webhook_role.sql first.")
            return 1

        print(f"\n{RULE}\nSTEP 2 -- set a fresh random password + HMAC secret\n{RULE}")
        password = secrets.token_urlsafe(24)
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
                .format(sql.Identifier("engram_webhook"), sql.Literal(password))
            )
        webhook_dsn = _dsn_with_credentials(admin_dsn, "engram_webhook", password)
        _upsert_env_var("ENGRAM_WEBHOOK_DSN", webhook_dsn)
        print("  wrote ENGRAM_WEBHOOK_DSN to .env")

        hmac_secret = secrets.token_urlsafe(32)
        _upsert_env_var("ENGRAM_WEBHOOK_HMAC_SECRET", hmac_secret)
        print("  wrote ENGRAM_WEBHOOK_HMAC_SECRET to .env")

        print(f"\n{RULE}\nSTEP 3 -- AWS Secrets Manager (what the real deployed Lambda reads)\n{RULE}")
        ok, detail = _put_secret(DSN_SECRET_NAME, webhook_dsn)
        record(f"secret {DSN_SECRET_NAME!r} written", ok, detail)
        ok, detail = _put_secret(HMAC_SECRET_NAME, hmac_secret)
        record(f"secret {HMAC_SECRET_NAME!r} written", ok, detail)

        print(f"\n{RULE}\nSTEP 4 -- live-verify the privilege boundary (measured, not assumed)\n{RULE}")
        scope_id = f"bootstrap-webhook-check-{secrets.token_hex(4)}"

        ok, detail = await _try_as(
            webhook_dsn, sslrootcert,
            "INSERT INTO tasks (scope_id, task_type, trigger, target_cluster_id, incident_fingerprint) "
            "VALUES (%s, 'incident', 'webhook', 'bootstrap-check', %s)",
            (scope_id, f"fp-{secrets.token_hex(8)}"),
        )
        record("engram_webhook: INSERT tasks succeeds", ok, detail)

        ok, detail = await _try_as(
            webhook_dsn, sslrootcert,
            "SELECT task_id FROM tasks WHERE scope_id = %s", (scope_id,),
        )
        record("engram_webhook: SELECT tasks succeeds (dedupe fallback query)", ok, detail)

        ok, detail = await _try_as(
            webhook_dsn, sslrootcert,
            "INSERT INTO observations (scope_id, source, kind, payload) "
            "VALUES (%s, 'webhook', 'alert', '{}'::jsonb)",
            (scope_id,),
        )
        record("engram_webhook: INSERT observations succeeds", ok, detail)

        ok, detail = await _try_as(
            webhook_dsn, sslrootcert,
            "INSERT INTO entities (scope_id, kind, name) VALUES (%s, 'table', 'bootstrap_check') "
            "ON CONFLICT (scope_id, kind, name) DO UPDATE SET last_seen_at = now()",
            (scope_id,),
        )
        record("engram_webhook: INSERT ... ON CONFLICT DO UPDATE entities succeeds", ok, detail)

        ok, detail = await _try_as(
            webhook_dsn, sslrootcert,
            "DELETE FROM tasks WHERE scope_id = %s", (scope_id,),
        )
        record("engram_webhook: DELETE correctly FAILS (no delete grant)", not ok, detail)

        ok, detail = await _try_as(
            webhook_dsn, sslrootcert, "SELECT * FROM approvals LIMIT 1",
        )
        record("engram_webhook: SELECT on approvals (not granted) correctly FAILS", not ok, detail)

        # cleanup (as admin -- engram_webhook has no DELETE). observations here has task_id=NULL
        # (the test INSERT above didn't set one), so cascading from tasks alone wouldn't reach it.
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM observations WHERE scope_id = %s", (scope_id,))
            await cur.execute("DELETE FROM entities WHERE scope_id = %s", (scope_id,))
            await cur.execute("DELETE FROM tasks WHERE scope_id = %s", (scope_id,))
        print(f"  cleaned up disposable rows (scope_id={scope_id})")

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
