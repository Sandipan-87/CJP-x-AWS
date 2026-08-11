-- Engram · Migration 005 — engram_reader gains SELECT on approvals.  [PLUMBER]
--
-- CORRECTED 2026-08-11 (Phase 3, dashboard chunk): design/02-low-level-design.md §11.1's own
-- frozen SSE feed table names an `approvals` feed reading the `approvals` TABLE directly
-- ("poll status change", not a view), but migration 002 only ever granted engram_reader SELECT
-- on `v_recent_tasks`, `v_action_feed`, `v_memory_inspector`, and `observations` -- `approvals`
-- was never included. Caught live while building the dashboard's SSE routes: a direct
-- `SELECT * FROM approvals` as engram_reader correctly raised `InsufficientPrivilege`.
--
-- `v_action_feed` already LEFT JOINs approvals for `approval_status`/`decided_by`, but that's not
-- enough for the dashboard's approval queue panel, which needs `approval_id`, `requested_at`,
-- `decided_at`, `channel`, `comment` too (LLD §11.3: "Approve button -> POST"; the button needs
-- the approval_id to target). Granting SELECT on the base table directly, matching §11.1's own
-- literal contract, rather than building a second view merely to route around the gap.
--
-- No write privilege granted -- engram_reader stays read-only (HLD §5.6: "No DB credentials in
-- the frontend"; mutations go through the separate API Gateway + Lambda path, LLD §11.2).

GRANT SELECT ON approvals TO engram_reader;
