-- Engram · Migration 002 — roles + grants.  [PLUMBER] · frozen Day 3 (design/02-low-level-design.md §6.2 note (c))
--
-- Split from 001 on purpose: GRANT ... ON ALL TABLES IN SCHEMA public only binds tables that
-- exist AT GRANT TIME. Running this after 001 covers everything 001 created; ALTER DEFAULT
-- PRIVILEGES then covers what 003/004 create LATER, which a one-shot GRANT would miss.
--
-- No blanket DELETE for engram_agent: Row-Level TTL does the deleting (invariant #7). The single
-- exception is agent_leases, whose SIGTERM release path (LLD §6.4) is a real DELETE — dropping
-- it there would strand the lease for a full 60s expiry and slow the kill-and-resume demo.

CREATE ROLE engram_agent;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO engram_agent;
GRANT DELETE ON agent_leases TO engram_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO engram_agent;

CREATE ROLE engram_reader;
GRANT SELECT ON v_recent_tasks, v_action_feed, v_memory_inspector, observations TO engram_reader;
