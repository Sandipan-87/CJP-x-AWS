-- Engram · Migration 009 — roles for the three lifecycle-worker Lambdas.  [PLUMBER]
-- design/02-low-level-design.md §9: consolidator (1h), decayer (nightly), embedding_backfill
-- (nightly + on-demand). Same least-privilege discipline as every prior per-Lambda role
-- (engram_webhook/engram_sweep_enumerator/engram_approver) — one role per Lambda, scoped to
-- exactly the tables that Lambda's own SQL touches, no DELETE, no GRANT, no ALTER.
--
-- A real gap in the frozen 001 schema, closed here rather than by editing 001 (same layering
-- convention 005_reader_approvals_grant.sql already used for a frozen-migration gap): §9's own
-- idempotency column names "UNIQUE(scope_id, name)" for `procedures`, but 001_engram_schema.sql
-- never actually declared that constraint -- `consolidator`'s `ON CONFLICT (scope_id, name) DO
-- NOTHING` needs a real unique index on exactly those columns to be valid SQL at all.
CREATE UNIQUE INDEX IF NOT EXISTS procedures_scope_name_idx ON procedures (scope_id, name);

-- embedding_backfill: `WHERE embedding IS NULL LIMIT 500` -> Cohere -> UPDATE memory_items,
-- via embedding_cache first (D9). Needs SELECT+UPDATE on memory_items (SELECT to find the NULL
-- rows AND because an UPDATE's WHERE clause needs SELECT on any column it reads — same shape as
-- migration 007's entities finding), SELECT+INSERT on embedding_cache (SELECT for the cache
-- check, INSERT for a miss — mirrors agent/memory/db.py's get_cached_embeddings/
-- insert_embedding_cache pair exactly, reimplemented independently in workers/ per this
-- project's "workers/ never imports agent/" convention).
CREATE ROLE IF NOT EXISTS engram_embedding_backfill;
GRANT SELECT, UPDATE ON memory_items TO engram_embedding_backfill;
GRANT SELECT, INSERT ON embedding_cache TO engram_embedding_backfill;

-- decayer: `UPDATE procedures SET confidence = wilson(...) * exp(...)`, retiring low-confidence
-- procedures and their orphaned memory_items. SELECT+UPDATE on procedures (SELECT to read
-- outcome_stats/updated_at/status, UPDATE to write confidence/status); SELECT+UPDATE on
-- memory_items (SELECT to find rows with source_row_id pointing at a just-retired procedure,
-- UPDATE to flip their status to 'retired').
CREATE ROLE IF NOT EXISTS engram_decayer;
GRANT SELECT, UPDATE ON procedures TO engram_decayer;
GRANT SELECT, UPDATE ON memory_items TO engram_decayer;

-- consolidator: clusters episode memory_items into draft/active procedures. Needs SELECT on
-- memory_items (episodes) and remediation_actions (joined via provenance->>'action_id' to read
-- the real action_kind/outcome/parameters a cluster shares — more reliable than parsing the
-- human-readable episode content string); SELECT+INSERT on procedures (SELECT for the
-- first-induction-per-scope check and the sources-overlap idempotency check, INSERT for a new
-- draft/active row); SELECT+INSERT on memory_items (SELECT for the ANN clustering query itself,
-- INSERT for the class='procedure' memory item written at the draft->active promotion, which is
-- written with embedding=NULL by design — same seed-then-backfill deferral as every other
-- episode/procedure row, invariant #1). No embedding_cache grant: `handler.py`'s own docstring
-- (simplification #1) decided clustering reuses the already-stored `memory_items.embedding`
-- directly rather than making a fresh Cohere call, so this role never touches that table —
-- granting it anyway would be exactly the unused, speculative permission this project's own
-- least-privilege discipline argues against.
--
-- A real, measured CockroachDB behavior, caught live by scripts/bootstrap_lifecycle_roles.py's
-- own verification step, NOT assumed from the SQL alone (same discipline as migration 007's
-- entities-SELECT-for-ON-CONFLICT finding): inserting into a table with a nullable FK column
-- requires SELECT on the REFERENCED table even when that column is omitted from the INSERT
-- (and so is implicitly NULL) -- CockroachDB still validates the constraint's existence at
-- privilege-check time, not just when the value is actually non-NULL. `procedures.created_by`
-- REFERENCES tasks(task_id) and `memory_items.entity_id` REFERENCES entities(entity_id); this
-- role's own INSERTs never set either column, but both grants below are still required for
-- those INSERTs to succeed at all -- confirmed by a real `InsufficientPrivilege` error on
-- `tasks`/`entities` respectively before these two lines existed, not a design choice.
CREATE ROLE IF NOT EXISTS engram_consolidator;
GRANT SELECT ON remediation_actions TO engram_consolidator;
GRANT SELECT ON tasks TO engram_consolidator;
GRANT SELECT ON entities TO engram_consolidator;
GRANT SELECT, INSERT ON memory_items TO engram_consolidator;
GRANT SELECT, INSERT ON procedures TO engram_consolidator;
