#!/usr/bin/env python3
"""Engram · one-time bootstrap for engram_sweep_enumerator (sweep enumerator Lambda).  [PLUMBER]

db/migrations/008_watched_queries.sql. Same pattern as `bootstrap_webhook_role.py`/
`bootstrap_approver_role.py`: sets a random password, writes `ENGRAM_SWEEP_DSN` into the
repo-root `.env` (local testing), attempts to also push it into AWS Secrets Manager (what the
real deployed Lambda reads), tolerating `AccessDenied` under `engram-phase0` the same way every
prior credential gap in this project has -- then LIVE-VERIFIES the actual privilege boundary
using a real, disposable row, not just that the role exists.

    python scripts/bootstrap_sweep_enumerator_role.py --sslrootcert memory-ca.crt
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
DSN_SECRET_NAME = "engram/sweep-dsn"
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

    print(f"{RULE}\nSTEP 1 -- confirm engram_sweep_enumerator exists (migration 008)\n{RULE}")
    conn = await psycopg.AsyncConnection.connect(full_admin_dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT rolname FROM pg_roles WHERE rolname = 'engram_sweep_enumerator'")
            exists = (await cur.fetchone()) is not None
        record("engram_sweep_enumerator role exists", exists)
        if not exists:
            print("  ABORT: run db/migrations/008_watched_queries.sql first.")
            return 1

        print(f"\n{RULE}\nSTEP 2 -- set a fresh random password\n{RULE}")
        password = secrets.token_urlsafe(24)
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
                .format(sql.Identifier("engram_sweep_enumerator"), sql.Literal(password))
            )
        sweep_dsn = _dsn_with_credentials(admin_dsn, "engram_sweep_enumerator", password)
        _upsert_env_var("ENGRAM_SWEEP_DSN", sweep_dsn)
        print("  wrote ENGRAM_SWEEP_DSN to .env")

        print(f"\n{RULE}\nSTEP 3 -- AWS Secrets Manager (what the real deployed Lambda reads)\n{RULE}")
        ok, detail = _put_secret(DSN_SECRET_NAME, sweep_dsn)
        record(f"secret {DSN_SECRET_NAME!r} written", ok, detail)

        print(f"\n{RULE}\nSTEP 4 -- live-verify the privilege boundary (measured, not assumed)\n{RULE}")
        marker = secrets.token_hex(4)
        scope_id = f"bootstrap-sweep-check-{marker}"

        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO watched_queries (scope_id, target_cluster_id, table_name, query_text, enabled) "
                "VALUES (%s, 'bootstrap-check', 'bootstrap_check', 'SELECT 1', false)",
                (scope_id,),
            )

        ok, detail = await _try_as(
            sweep_dsn, sslrootcert,
            "SELECT watched_query_id FROM watched_queries WHERE scope_id = %s", (scope_id,),
        )
        record("engram_sweep_enumerator: SELECT watched_queries succeeds", ok, detail)

        ok, detail = await _try_as(
            sweep_dsn, sslrootcert,
            "INSERT INTO watched_queries (scope_id, target_cluster_id, table_name, query_text) "
            "VALUES (%s, 'bootstrap-check', 'x', 'SELECT 1')",
            (f"{scope_id}-write-attempt",),
        )
        record("engram_sweep_enumerator: INSERT watched_queries correctly FAILS (read-only)", not ok, detail)

        ok, detail = await _try_as(
            sweep_dsn, sslrootcert, "SELECT * FROM tasks LIMIT 1",
        )
        record("engram_sweep_enumerator: SELECT on tasks (not granted) correctly FAILS", not ok, detail)

        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM watched_queries WHERE scope_id = %s", (scope_id,))
        print(f"  cleaned up disposable row (scope_id={scope_id})")

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
