# Migration runbook — P1-P1 (design/02-low-level-design.md §6.2, §6.3)

**Order is load-bearing, not stylistic.** Running these out of order either
fails outright (001/002) or violates invariant #1 / invariant #7.

1. `SET CLUSTER SETTING feature.vector_index.enabled = true;` — once per
   cluster, not a file here (verified already in Phase 0, `docs/phase0-verification.md` §1).
2. `001_engram_schema.sql` — all tables, non-vector indexes, dashboard views.
3. `002_grants.sql` — roles + grants + `ALTER DEFAULT PRIVILEGES` (so tables
   003/004 create later are covered without a second grant pass).
4. Bootstrap checkpoints: run `AsyncCockroachDBSaver.setup()` once, on the
   still-otherwise-empty cluster, then **immediately** `004_checkpoint_ttl.sql`
   — TTL must be applied before any checkpoint row exists (invariant #7; adding
   TTL to a hot table triggers a full rewrite).
5. Seed the corpus (episodes, procedure descriptions, skills) **without**
   embeddings, then run the backfill worker (Cohere `embed-english-v3.0`,
   `input_type='search_document'`, asserting `len(vec) == 1024` before any
   write — confirmed possible, `scripts/verify_cohere.py` gate PASS).
6. `003_vector_index.sql` — only after step 5. Invariant #1: `IMPORT INTO` is
   unsupported on a table with a live vector index.

## Running these

Local port 26257 is blocked by a transparent proxy on this network
(`docs/blocked-register.md` §3) with no admin unblock or alternate network
available. Two ways to actually execute a file, in order of preference:

- **CockroachDB Cloud Console SQL Shell** (same workaround as Phase 0, works
  today, zero setup): paste the file contents into the Console's SQL shell.
- **GitHub Actions** (`.github/workflows/db-migrate.yml`, `workflow_dispatch`):
  runs `scripts/run_sql.py` against `ENGRAM_MEMORY_DSN`/`ENGRAM_TARGET_DSN`
  from GitHub-hosted runners, which sit outside the local network entirely.
  Requires the two DSNs added as repo secrets once (Manual Action Checklist).

Every file here also parses clean with `python scripts/run_sql.py <file>
--dry-run` — no connection needed, useful to sanity-check a new file before
either path above.
