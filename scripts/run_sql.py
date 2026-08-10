#!/usr/bin/env python3
"""Engram · SQL file runner over psycopg3.  Role: [PLUMBER]

Executes a .sql file statement-by-statement against a CockroachDB cluster and
prints a transcript suitable for pasting into docs/phase0-verification.md.

Exists because the `cockroach` CLI is not a project dependency — CLAUDE.md §2
pins psycopg3 as the hot path, so proving the vector index through psycopg3 is
stronger P0-P1 evidence than proving it through a CLI we will never ship.

    pip install -r scripts/requirements-verify.txt
    python scripts/run_sql.py db/phase0_vector_probe.sql --dry-run   # parse only
    python scripts/run_sql.py db/phase0_vector_probe.sql

    # stop before the DROP so verify_mcp.py can probe the seeded table:
    python scripts/run_sql.py db/phase0_vector_probe.sql --stop-before-step 8

DSN resolution order: --dsn <literal> | $ENGRAM_MEMORY_DSN (from .env or the
environment). Exit 0 only if every statement succeeded.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import time

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*_a, **_k):  # type: ignore[misc]
        return False

DRIVER = None
try:
    import psycopg  # psycopg3 — the pinned driver

    DRIVER = "psycopg3"
except ImportError:
    try:
        import psycopg2 as psycopg  # type: ignore[no-redef]

        DRIVER = "psycopg2"
    except ImportError:
        pass

RULE = "=" * 72
# Collapse the 1024-dim probe literal so the transcript stays readable.
VEC_LITERAL = re.compile(r"'\[[-0-9eE.,\s]{200,}\]'")


def split_statements(sql: str) -> list[str]:
    """Split on semicolons outside single-quoted strings and -- comments.

    Deliberately simple: the probe file has no dollar-quoting and no semicolons
    inside literals or comments. If that ever changes, this needs a real lexer.
    """
    out: list[str] = []
    buf: list[str] = []
    in_str = False
    in_comment = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_comment:
            if ch == "\n":
                in_comment = False
                buf.append(ch)
            i += 1
            continue

        if in_str:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":          # '' escape
                    buf.append(nxt)
                    i += 2
                    continue
                in_str = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_comment = True
            i += 2
            continue
        if ch == "'":
            in_str = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def step_markers(sql: str) -> dict[int, int]:
    """Map STEP number -> character offset, so --stop-before-step works."""
    marks: dict[int, int] = {}
    for m in re.finditer(r"^--\s*STEP\s+(\d+)", sql, re.MULTILINE):
        marks[int(m.group(1))] = m.start()
    return marks


def echo(stmt: str) -> str:
    return VEC_LITERAL.sub("'[…1024 dims elided…]'", stmt)


def render(cur) -> None:
    """Print a result set as aligned text, or the rowcount if there is none."""
    if cur.description is None:
        print(f"    -> OK (rowcount {cur.rowcount})")
        return
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if not rows:
        print(f"    -> 0 rows  ({', '.join(cols)})")
        return
    cells = [[("NULL" if v is None else str(v)) for v in r] for r in rows]
    widths = [
        min(72, max(len(cols[i]), *(len(r[i]) for r in cells)))
        for i in range(len(cols))
    ]
    print("    " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    print("    " + "-+-".join("-" * w for w in widths))
    for r in cells:
        print("    " + " | ".join(v[: widths[i]].ljust(widths[i]) for i, v in enumerate(r)))
    print(f"    ({len(rows)} row{'s' if len(rows) != 1 else ''})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlfile", type=pathlib.Path)
    ap.add_argument("--dsn", default=None, help="literal DSN; overrides $ENGRAM_MEMORY_DSN")
    ap.add_argument("--dsn-var", default="ENGRAM_MEMORY_DSN")
    ap.add_argument("--dry-run", action="store_true", help="parse and list statements, no connection")
    ap.add_argument("--stop-before-step", type=int, default=None)
    ap.add_argument("--stop-on-error", action="store_true", help="abort at the first failure")
    ap.add_argument("--sslrootcert", default=None,
                     help="path to the cluster's CA cert (e.g. downloaded from "
                          "https://cockroachlabs.cloud/clusters/<CLUSTER_ID>/cert). "
                          "Appended to the DSN if it doesn't already set sslrootcert.")
    args = ap.parse_args()

    load_dotenv()

    sql = args.sqlfile.read_text(encoding="utf-8")
    if args.stop_before_step is not None:
        marks = step_markers(sql)
        if args.stop_before_step not in marks:
            sys.exit(f"FATAL: no '-- STEP {args.stop_before_step}' marker in {args.sqlfile}")
        sql = sql[: marks[args.stop_before_step]]
        print(f"# truncated before STEP {args.stop_before_step}")

    statements = split_statements(sql)

    print(RULE)
    print(f"file       : {args.sqlfile}")
    print(f"statements : {len(statements)}")
    print(f"driver     : {DRIVER or 'NONE INSTALLED'}")
    print(RULE)

    if args.dry_run:
        for i, s in enumerate(statements, 1):
            first = echo(s).splitlines()[0][:96]
            print(f"  [{i:02d}] {first}")
        print("\nDRY RUN — nothing executed.")
        return 0

    if DRIVER is None:
        sys.exit("FATAL: no psycopg driver. pip install -r scripts/requirements-verify.txt")
    if DRIVER == "psycopg2":
        print("WARN: falling back to psycopg2. CLAUDE.md §2 pins psycopg3 —")
        print("      install it before Phase 1: pip install 'psycopg[binary]>=3.2'\n")

    dsn = args.dsn or os.environ.get(args.dsn_var)
    if not dsn:
        sys.exit(
            f"FATAL: no DSN. Set {args.dsn_var} in .env (or pass --dsn).\n"
            "       .env is gitignored; never put a real DSN in .env.example."
        )
    if args.sslrootcert and "sslrootcert=" not in dsn:
        # CockroachDB Cloud's CA chains to a publicly-trusted root (ISRG Root X1 /
        # Let's Encrypt), but `sslrootcert=system` is unreliable against psycopg's
        # own bundled libpq build (manylinux wheel, its own static OpenSSL — "system"
        # resolves against ITS trust store from build time, not the host's actual
        # /etc/ssl/certs). Measured 2026-08-11: verify-full + sslrootcert=system
        # failed with "certificate verify failed" on a GitHub Actions runner even
        # though the cert chain is genuinely public-CA-signed. The reliable fix is
        # the literal one: fetch the real per-cluster cert and point at the file.
        rootcert_path = pathlib.Path(args.sslrootcert).expanduser().resolve()
        sep = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{sep}sslrootcert={rootcert_path}"
    safe = re.sub(r"//([^:]+):[^@]+@", r"//\1:***@", dsn)
    print(f"dsn        : {safe}\n")

    t_conn = time.perf_counter()
    try:
        if DRIVER == "psycopg3":
            conn = psycopg.connect(dsn, autocommit=True)
        else:
            conn = psycopg.connect(dsn)
            conn.autocommit = True
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: connect failed: {type(exc).__name__}: {exc}")
        print(
            "\nTriage:\n"
            "  certificate verify failed -> download the cluster CA from the Cloud console's\n"
            "     Connect panel and append &sslrootcert=<abs path> to the DSN.\n"
            "  password authentication failed -> the SQL password is shown once at user\n"
            "     creation; reset it in the console rather than guessing.\n"
            "  timeout / no route -> confirm the cluster shows Available, and that any\n"
            "     IP-allowlist entry covers your current address."
        )
        return 1
    print(f"connected in {time.perf_counter() - t_conn:.3f}s")

    failures: list[tuple[int, str, str]] = []
    with conn:
        for i, stmt in enumerate(statements, 1):
            print(f"\n--- [{i:02d}/{len(statements)}] " + "-" * 48)
            for line in echo(stmt).splitlines():
                print(f"  {line}")
            t0 = time.perf_counter()
            try:
                with conn.cursor() as cur:
                    cur.execute(stmt)
                    dt = (time.perf_counter() - t0) * 1000
                    print(f"    [{dt:.1f} ms]")
                    render(cur)
            except Exception as exc:  # noqa: BLE001
                dt = (time.perf_counter() - t0) * 1000
                msg = f"{type(exc).__name__}: {exc}"
                print(f"    [{dt:.1f} ms]  ERROR  {msg}")
                failures.append((i, echo(stmt).splitlines()[0][:80], msg))
                if args.stop_on_error:
                    break

    print(f"\n{RULE}")
    if failures:
        print(f"{len(failures)} statement(s) FAILED:")
        for i, head, msg in failures:
            print(f"  [{i:02d}] {head}\n       {msg}")
    else:
        print("all statements OK")
    print(RULE)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
