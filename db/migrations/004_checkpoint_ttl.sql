-- Engram · Migration 004 — LangGraph checkpoint TTL.  [PLUMBER] · frozen Day 3 (design/02-low-level-design.md §6.2)
--
-- PREREQUISITE, NOT part of this file: run AsyncCockroachDBSaver.setup() ONCE first, on an
-- EMPTY cluster, to create langgraph_checkpoints / langgraph_checkpoint_blobs /
-- langgraph_checkpoint_writes. Then run this file IMMEDIATELY, before any checkpoint data exists.
--
-- CRITICAL (CockroachDB v26.2 docs, row-level TTL): adding ttl_expire_after to an EXISTING table
-- triggers a FULL TABLE REWRITE — a new hidden crdb_internal_expiration column plus a backfill of
-- every row. On an empty table that rewrite is instant; on a hot table it is not. Invariant #7.
-- Never ALTER the TTL on these three tables again once they hold real checkpoint data.

ALTER TABLE langgraph_checkpoints        SET (ttl_expire_after = '30 days'::interval);
ALTER TABLE langgraph_checkpoint_blobs   SET (ttl_expire_after = '30 days'::interval);
ALTER TABLE langgraph_checkpoint_writes  SET (ttl_expire_after = '30 days'::interval);
