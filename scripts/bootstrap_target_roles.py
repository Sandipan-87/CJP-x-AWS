#!/usr/bin/env python3
"""Engram · one-time bootstrap for engram_probe/engram_operator on the TARGET cluster.  [PLUMBER]

design/01-high-level-design.md D6/ADR-006. Runs `db/target/001_target_roles.sql` (schema/grants
only, no secrets) against the live target cluster using the admin DSN, then sets a random
password on each role via a separate parameterized statement (never string-interpolated),
constructs `ENGRAM_TARGET_PROBE_DSN`/`ENGRAM_TARGET_OPERATOR_DSN`, and writes them into `.env`.
Closes the provisioning gap `agent/tools/sql_probe.py`/`sql_operator.py` have both been loudly
warning about since Session 20/23 (falling back to the admin DSN otherwise).

Passwords are generated here, never printed, never written anywhere but `.env` (gitignored).

Then LIVE-VERIFIES the actual privilege boundary, not just that the roles exist:
  - engram_probe:    SELECT succeeds, CREATE INDEX fails.
  - engram_operator: CREATE INDEX + ANALYZE succeed, DROP TABLE and GRANT fail.
Uses a disposable scenario table (created as admin, since neither new role can CREATE TABLE)
dropped again at the end regardless of outcome.

    python scripts/bootstrap_target_roles.py --sslrootcert target-ca.crt
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
MIGRATION = pathlib.Path(__file__).resolve().parent.parent / "db/target/001_target_roles.sql"
RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


def _statements_from_sql_file(path: pathlib.Path) -> list[str]:
    lines = [ln for ln in path.read_text().splitlines() if not ln.strip().startswith("--")]
    text = "\n".join(lines)
    return [s.strip() for s in text.split(";") if s.strip()]


def _dsn_with_credentials(base_dsn: str, user: str, password: str) -> str:
    """Swaps user:password in an existing DSN, keeps host/port/db/query string as-is."""
    p = urlparse(base_dsn)
    netloc = f"{user}:{password}@{p.hostname}"
    if p.port:
        netloc += f":{p.port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def _upsert_env_var(key: str, value: str) -> None:
    """Overwrites the line if `key` already exists, else appends a new one. Never prints `value`."""
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


async def _try_as(dsn: str, sslrootcert: str | None, stmt: str) -> tuple[bool, str]:
    """Connects fresh as whatever role `dsn` authenticates as, runs one statement, reports
    success/failure without raising -- used to prove a privilege boundary either direction.
    """
    full_dsn = dsn
    if sslrootcert and "sslrootcert=" not in full_dsn:
        sep = "&" if "?" in full_dsn else "?"
        full_dsn = f"{full_dsn}{sep}sslrootcert={sslrootcert}"
    try:
        async with await psycopg.AsyncConnection.connect(full_dsn, autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(stmt)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def main(sslrootcert: str | None) -> int:
    admin_dsn = os.environ["ENGRAM_TARGET_DSN"]
    full_admin_dsn = admin_dsn
    if sslrootcert and "sslrootcert=" not in full_admin_dsn:
        sep = "&" if "?" in full_admin_dsn else "?"
        full_admin_dsn = f"{full_admin_dsn}{sep}sslrootcert={sslrootcert}"

    # The migration's `ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin ...` statements assume the
    # target cluster's admin role is literally named `engram_admin` (true for the original sandbox
    # cluster). A different CockroachDB Cloud org (e.g. a friend's account) names the admin role
    # after whoever signed up instead -- substitute the real admin username from ENGRAM_TARGET_DSN
    # so this migration still applies correctly. No-op when the DSN user really is `engram_admin`.
    admin_role = urlparse(admin_dsn).username or "engram_admin"

    print(f"{RULE}\nSTEP 1 -- run db/target/001_target_roles.sql (schema/grants, no secrets)\n{RULE}")
    conn = await psycopg.AsyncConnection.connect(full_admin_dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            for stmt in _statements_from_sql_file(MIGRATION):
                stmt = stmt.replace("engram_admin", admin_role)
                await cur.execute(stmt)
                print(f"  executed: {stmt.splitlines()[0]}...")

        print(f"\n{RULE}\nSTEP 2 -- set a fresh random password on each role\n{RULE}")
        probe_pw = secrets.token_urlsafe(24)
        operator_pw = secrets.token_urlsafe(24)
        async with conn.cursor() as cur:
            for role, pw in (("engram_probe", probe_pw), ("engram_operator", operator_pw)):
                await cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
                    .format(sql.Identifier(role), sql.Literal(pw))
                )
                print(f"  password set for {role} (not printed)")

        probe_dsn = _dsn_with_credentials(admin_dsn, "engram_probe", probe_pw)
        operator_dsn = _dsn_with_credentials(admin_dsn, "engram_operator", operator_pw)
        _upsert_env_var("ENGRAM_TARGET_PROBE_DSN", probe_dsn)
        _upsert_env_var("ENGRAM_TARGET_OPERATOR_DSN", operator_dsn)
        print("  wrote ENGRAM_TARGET_PROBE_DSN and ENGRAM_TARGET_OPERATOR_DSN to .env")

        print(f"\n{RULE}\nSTEP 3 -- disposable scenario table (admin-created; neither new role can CREATE TABLE)\n{RULE}")
        table = f"bootstrap_roles_check_{secrets.token_hex(4)}"
        async with conn.cursor() as cur:
            await cur.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, val INT)")
        record("scenario table created as admin", True, table)

        print(f"\n{RULE}\nSTEP 4 -- live-verify the privilege boundary (measured, not assumed)\n{RULE}")

        ok, detail = await _try_as(probe_dsn, sslrootcert, f"SELECT * FROM {table} LIMIT 1")
        record("engram_probe: SELECT succeeds", ok, detail)

        ok, detail = await _try_as(
            probe_dsn, sslrootcert, f"CREATE INDEX ON {table} (val)"
        )
        record("engram_probe: CREATE INDEX correctly FAILS (read-only)", not ok, detail)

        ok, detail = await _try_as(
            operator_dsn, sslrootcert, f"CREATE INDEX bootstrap_check_idx ON {table} (val)"
        )
        record("engram_operator: CREATE INDEX succeeds", ok, detail)

        ok, detail = await _try_as(operator_dsn, sslrootcert, f"ANALYZE {table}")
        record("engram_operator: ANALYZE succeeds", ok, detail)

        ok, detail = await _try_as(operator_dsn, sslrootcert, f"DROP TABLE {table}")
        record("engram_operator: DROP TABLE correctly FAILS", not ok, detail)

        ok, detail = await _try_as(
            operator_dsn, sslrootcert, f"GRANT SELECT ON {table} TO engram_probe"
        )
        record("engram_operator: GRANT correctly FAILS", not ok, detail)

        async with conn.cursor() as cur:
            await cur.execute(f"DROP TABLE IF EXISTS {table}")
        print(f"  dropped {table}")

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
