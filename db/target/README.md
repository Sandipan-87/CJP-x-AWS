# Target-cluster migration runbook

**Separate lineage from `db/migrations/`.** `db/migrations/` applies to the MEMORY cluster (the
product being judged); this directory applies to the TARGET cluster (the subject the agent
operates on). CLAUDE.md §2: "two clusters, two roles — never conflate them."

1. `001_target_roles.sql` — creates `engram_probe` (read-only) and `engram_operator`
   (allowlisted DDL: `CREATE INDEX`, `ANALYZE` — no `DROP`/`TRUNCATE`/`GRANT`), per
   `design/01-high-level-design.md` D6/ADR-006. No passwords in this file.
2. `scripts/bootstrap_target_roles.py` — runs step 1's SQL, then sets a random password on each
   role via a separate parameterized statement, constructs `ENGRAM_TARGET_PROBE_DSN` /
   `ENGRAM_TARGET_OPERATOR_DSN`, writes them into `.env`, and live-verifies the privilege
   boundary (probe can `SELECT` but not `CREATE INDEX`; operator can `CREATE INDEX`/`ANALYZE`
   but not `DROP TABLE` or `GRANT`).

Idempotent: `CREATE ROLE IF NOT EXISTS` means re-running step 1 is harmless. Re-running the
bootstrap script rotates both passwords and overwrites the `.env` DSNs — intentional (a
provisioning script, not a one-shot secret).
