-- Engram · Migration 007 — engram_webhook role for the alert-ingest Lambda.  [PLUMBER]
-- design/02-low-level-design.md §11.2 (POST /webhooks/alerts -> "observations + incident task").
--
-- Mirrors exactly what agent/memory/db.py's insert_incident_observation() does (LLD §5.1 step
-- 4's "one txn: tasks + observations + entities"), reimplemented independently in
-- workers/webhooks/handler.py (workers/common has no agent import, per this project's own
-- directory-tree convention) using the SAME schema, SAME dedupe logic, SAME partial unique
-- index (tasks_active_incident_idx) -- a genuinely different Lambda writing through the same
-- front door the internal observe(node) path already uses, not a shortcut around it.
--
-- Grants, scoped to exactly the three statements that one-txn insert runs:
--   1. INSERT INTO tasks (...) -- plus SELECT, needed for the dedupe fallback query when the
--      INSERT hits tasks_active_incident_idx's unique violation and must look up the existing
--      active incident's task_id instead.
--   2. INSERT INTO observations (...) -- no SELECT/UPDATE needed, this table is append-only here.
--   3. INSERT ... ON CONFLICT (scope_id, kind, name) DO UPDATE INTO entities -- needs SELECT
--      *and* UPDATE, not just INSERT: measured live via scripts/bootstrap_webhook_role.py, not
--      assumed from the SQL alone -- the first attempt granted only INSERT+UPDATE and failed
--      with "does not have SELECT privilege on relation entities". Makes sense on reflection:
--      detecting the conflict in the first place requires reading the existing row.
-- No DELETE, no GRANT, no access to any other table -- same discipline as
-- engram_probe/engram_operator/engram_reader/engram_approver before it.

CREATE ROLE IF NOT EXISTS engram_webhook;
GRANT SELECT, INSERT ON tasks TO engram_webhook;
GRANT INSERT ON observations TO engram_webhook;
GRANT SELECT, INSERT, UPDATE ON entities TO engram_webhook;
