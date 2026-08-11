-- Engram · Migration 004 — LangGraph checkpoint TTL.  [PLUMBER] · frozen Day 3 (design/02-low-level-design.md §6.2)
--
-- PREREQUISITE, NOT part of this file: run AsyncCockroachDBSaver.setup() ONCE first, on an
-- EMPTY cluster (scripts/bootstrap_checkpointer.py runs setup() and this file back-to-back in one
-- process, closing the gap two separate manual steps would leave), to create the three checkpoint
-- tables. This file must run IMMEDIATELY after, before any checkpoint data exists.
--
-- CORRECTED 2026-08-11 (Phase 3 bootstrap): the original draft of this file was written before
-- `langchain-cockroachdb` was ever installed, and guessed two things that measurement (reading the
-- actual installed 0.3.0 package's checkpointer/base.py) shows were both wrong:
--   1. Table names: guessed `langgraph_checkpoints`/`langgraph_checkpoint_blobs`/
--      `langgraph_checkpoint_writes` (a prefix that appears nowhere in the library). The real
--      names, from `AsyncCockroachDBSaver.MIGRATIONS`, are unprefixed: `checkpoints`,
--      `checkpoint_blobs`, `checkpoint_writes` (plus a small `checkpoint_migrations` version
--      table, not TTL'd — it never accumulates unbounded rows).
--   2. Mechanism: guessed `ttl_expire_after` — the CockroachDB feature that adds a hidden
--      `crdb_internal_expiration` column and forces the exact full-table-rewrite this file's
--      history warned about (invariant #7). The library ships its own `saver.aenable_ttl()`,
--      which instead uses `ttl_expiration_expression` against a plain `created_at` column
--      `setup()` already adds to all three tables — no hidden column, no rewrite, per the
--      library's own comment ("to avoid full table rewrites"). This file inlines the exact SQL
--      `aenable_ttl()` would run, kept as a plain .sql file so it stays consistent with every
--      other migration here rather than becoming a one-off Python call.
-- Never ALTER these three again once hot (invariant #7) — `ttl_expiration_expression` avoids the
-- REWRITE risk specifically, not the "don't touch it again" discipline generally.

ALTER TABLE checkpoints SET (
    ttl_expiration_expression = $$(created_at + '30 days')$$,
    ttl_job_cron = '@daily'
);
ALTER TABLE checkpoint_blobs SET (
    ttl_expiration_expression = $$(created_at + '30 days')$$,
    ttl_job_cron = '@daily'
);
ALTER TABLE checkpoint_writes SET (
    ttl_expiration_expression = $$(created_at + '30 days')$$,
    ttl_job_cron = '@daily'
);
