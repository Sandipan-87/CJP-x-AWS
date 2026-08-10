-- Engram · Migration 003 — C-SPANN vector index.  [PLUMBER] · frozen Day 3 (design/02-low-level-design.md §6.2, §6.3)
--
-- DO NOT RUN THIS BEFORE THE CORPUS IS SEEDED. Invariant #1: IMPORT INTO is unsupported on a
-- table with a live vector index, and this project seeds via bulk insert + backfill embedding,
-- not a single small write — building the index before seeding forces a slow per-row path.
-- Runbook order (LLD §6.3): migrations 001-002 -> seed corpus WITHOUT embeddings -> backfill
-- worker embeds via Cohere (asserting len(vec) == 1024 before any write) -> THIS FILE.
--
-- Verified 2026-08-03 on a free Basic v26.2.1 cluster (docs/phase0-verification.md §1):
-- EXPLAIN shows a `vector search` operator with `prefix spans` on scope_id; the negative
-- control (same query, no scope_id predicate) correctly shows a FULL SCAN — proving invariant #3
-- (every ANN query must equality-constrain scope_id) is not optional, it's what makes the index
-- usable at all.

CREATE VECTOR INDEX mem_vec_idx ON memory_items (scope_id, embedding vector_cosine_ops)
  WITH (min_partition_size = 16, max_partition_size = 128);
