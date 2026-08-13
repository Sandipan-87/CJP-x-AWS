#!/usr/bin/env python3
"""Engram · one-time bootstrap for the three §9 lifecycle-worker roles.  [PLUMBER]

db/migrations/009_lifecycle_roles.sql: `engram_embedding_backfill`, `engram_decayer`,
`engram_consolidator`. Same pattern as `bootstrap_sweep_enumerator_role.py`/
`bootstrap_webhook_role.py`: sets a random password per role, writes each
`ENGRAM_*_DSN` into the repo-root `.env` (local testing), attempts to also push it into
AWS Secrets Manager under the exact secret name `infra/engram_infra/agent_stack.py`'s
`_add_lifecycle_rules` imports by name (`engram/embedding-backfill-dsn`, `engram/decayer-dsn`,
`engram/consolidator-dsn`), tolerating `AccessDenied` under `engram-phase0` the same way every
prior credential gap in this project has -- then LIVE-VERIFIES each role's actual privilege
boundary using a real, disposable row, not just that the role exists.

    python scripts/bootstrap_lifecycle_roles.py --sslrootcert memory-ca.crt
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
RULE = "-" * 72

# (role, env_dsn_var, secret_name) -- matches infra/engram_infra/agent_stack.py's
# DEFAULT_*_DSN_SECRET_NAME constants exactly.
ROLES = [
    ("engram_embedding_backfill", "ENGRAM_EMBEDDING_BACKFILL_DSN", "engram/embedding-backfill-dsn"),
    ("engram_decayer", "ENGRAM_DECAYER_DSN", "engram/decayer-dsn"),
    ("engram_consolidator", "ENGRAM_CONSOLIDATOR_DSN", "engram/consolidator-dsn"),
]

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


async def _verify_embedding_backfill(dsn: str, sslrootcert: str | None, marker: str) -> None:
    """SELECT+UPDATE memory_items, SELECT+INSERT embedding_cache -- no INSERT on memory_items,
    no DELETE anywhere, nothing on unrelated tables (migration 009's own grant list)."""
    ok, detail = await _try_as(dsn, sslrootcert, "SELECT item_id FROM memory_items LIMIT 1")
    record("engram_embedding_backfill: SELECT memory_items succeeds", ok, detail)

    ok, detail = await _try_as(
        dsn, sslrootcert,
        "INSERT INTO memory_items (scope_id, class, content) VALUES (%s, 'episode', 'x')",
        (f"bootstrap-lifecycle-check-{marker}",),
    )
    record("engram_embedding_backfill: INSERT memory_items correctly FAILS (UPDATE-only)", not ok, detail)

    ok, detail = await _try_as(
        dsn, sslrootcert,
        "INSERT INTO embedding_cache (content_sha256, embedding, model_id) "
        "VALUES (%s, %s::VECTOR(1024), 'embed-english-v3.0') ON CONFLICT DO NOTHING",
        (f"bootstrap-lifecycle-check-{marker}", "[" + ",".join(["0.1"] * 1024) + "]"),
    )
    record("engram_embedding_backfill: INSERT embedding_cache succeeds", ok, detail)

    ok, detail = await _try_as(dsn, sslrootcert, "SELECT * FROM tasks LIMIT 1")
    record("engram_embedding_backfill: SELECT tasks (not granted) correctly FAILS", not ok, detail)


async def _verify_decayer(dsn: str, sslrootcert: str | None, marker: str) -> None:
    """SELECT+UPDATE procedures, SELECT+UPDATE memory_items -- no INSERT anywhere."""
    ok, detail = await _try_as(dsn, sslrootcert, "SELECT procedure_id FROM procedures LIMIT 1")
    record("engram_decayer: SELECT procedures succeeds", ok, detail)

    ok, detail = await _try_as(
        dsn, sslrootcert,
        "INSERT INTO procedures (scope_id, name, description, steps) VALUES (%s, %s, 'x', '[]'::JSONB)",
        (f"bootstrap-lifecycle-check-{marker}", f"bootstrap-check-{marker}"),
    )
    record("engram_decayer: INSERT procedures correctly FAILS (UPDATE-only)", not ok, detail)

    ok, detail = await _try_as(dsn, sslrootcert, "SELECT * FROM tasks LIMIT 1")
    record("engram_decayer: SELECT tasks (not granted) correctly FAILS", not ok, detail)


async def _verify_consolidator(dsn: str, sslrootcert: str | None, marker: str) -> None:
    """SELECT remediation_actions, SELECT+INSERT memory_items, SELECT+INSERT procedures, plus
    SELECT tasks/entities -- a real, measured requirement (not a design choice): CockroachDB
    checks SELECT on a nullable FK's referenced table even when that column is left NULL, so
    INSERT INTO procedures/memory_items would otherwise fail on tasks/entities respectively --
    see migration 009's own comment. Deliberately NO embedding_cache grant (handler.py's own
    simplification #1: no fresh embedding call), a real check that the grant this project
    trimmed actually stays trimmed."""
    ok, detail = await _try_as(dsn, sslrootcert, "SELECT action_id FROM remediation_actions LIMIT 1")
    record("engram_consolidator: SELECT remediation_actions succeeds", ok, detail)

    ok, detail = await _try_as(
        dsn, sslrootcert,
        "INSERT INTO procedures (scope_id, name, description, steps) VALUES (%s, %s, 'x', '[]'::JSONB)",
        (f"bootstrap-lifecycle-check-{marker}", f"bootstrap-check-{marker}"),
    )
    record("engram_consolidator: INSERT procedures succeeds (needs SELECT on tasks -- FK check)", ok, detail)

    ok, detail = await _try_as(
        dsn, sslrootcert,
        "INSERT INTO memory_items (scope_id, class, content) VALUES (%s, 'procedure', 'x')",
        (f"bootstrap-lifecycle-check-{marker}",),
    )
    record("engram_consolidator: INSERT memory_items succeeds (needs SELECT on entities -- FK check)", ok, detail)

    ok, detail = await _try_as(
        dsn, sslrootcert,
        "UPDATE procedures SET confidence = 0 WHERE name = %s",
        (f"bootstrap-check-{marker}",),
    )
    record("engram_consolidator: UPDATE procedures correctly FAILS (no UPDATE grant)", not ok, detail)

    ok, detail = await _try_as(
        dsn, sslrootcert,
        "INSERT INTO embedding_cache (content_sha256, embedding, model_id) "
        "VALUES (%s, %s::VECTOR(1024), 'embed-english-v3.0')",
        (f"bootstrap-lifecycle-check-{marker}-2", "[" + ",".join(["0.1"] * 1024) + "]"),
    )
    record("engram_consolidator: INSERT embedding_cache correctly FAILS (no grant, by design)", not ok, detail)


VERIFIERS = {
    "engram_embedding_backfill": _verify_embedding_backfill,
    "engram_decayer": _verify_decayer,
    "engram_consolidator": _verify_consolidator,
}


async def main(sslrootcert: str | None) -> int:
    admin_dsn = os.environ["ENGRAM_MEMORY_DSN"]
    full_admin_dsn = admin_dsn
    if sslrootcert and "sslrootcert=" not in full_admin_dsn:
        sep = "&" if "?" in full_admin_dsn else "?"
        full_admin_dsn = f"{full_admin_dsn}{sep}sslrootcert={sslrootcert}"

    conn = await psycopg.AsyncConnection.connect(full_admin_dsn, autocommit=True)
    marker = secrets.token_hex(4)
    try:
        for role, env_var, secret_name in ROLES:
            print(f"\n{RULE}\n{role}\n{RULE}")
            async with conn.cursor() as cur:
                await cur.execute("SELECT rolname FROM pg_roles WHERE rolname = %s", (role,))
                exists = (await cur.fetchone()) is not None
            record(f"{role} role exists", exists)
            if not exists:
                print("  ABORT: run db/migrations/009_lifecycle_roles.sql first.")
                return 1

            password = secrets.token_urlsafe(24)
            async with conn.cursor() as cur:
                await cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
                    .format(sql.Identifier(role), sql.Literal(password))
                )
            role_dsn = _dsn_with_credentials(admin_dsn, role, password)
            _upsert_env_var(env_var, role_dsn)
            print(f"  wrote {env_var} to .env")

            ok, detail = _put_secret(secret_name, role_dsn)
            record(f"secret {secret_name!r} written", ok, detail)

            await VERIFIERS[role](role_dsn, sslrootcert, marker)

        print(f"\n{RULE}\ncleanup\n{RULE}")
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM procedures WHERE name LIKE %s", (f"bootstrap-check-{marker}%",)
            )
            await cur.execute(
                "DELETE FROM embedding_cache WHERE content_sha256 LIKE %s",
                (f"bootstrap-lifecycle-check-{marker}%",),
            )
            await cur.execute(
                "DELETE FROM memory_items WHERE scope_id LIKE %s",
                (f"bootstrap-lifecycle-check-{marker}%",),
            )
        print(f"  cleaned up disposable rows (marker={marker})")

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
