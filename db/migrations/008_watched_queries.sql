-- Engram · Migration 008 — watched_queries registry + engram_sweep_enumerator role.  [PLUMBER]
-- design/02-low-level-design.md §5.1 step 1 / CLAUDE.md's own "Next action" list: the sweep
-- EventBridge rule (infra/engram_infra/agent_stack.py's SweepRule) has always been created
-- `enabled=False` because nothing in this codebase ever decided WHICH scope/cluster/table/query
-- is worth probing every 5 minutes -- the LLD's own answer to that ("MCP show_running_queries")
-- needs an MCP client this project has never built (a separate, larger, already-tracked gap).
--
-- This is a deliberately smaller, honest substitute, not a shortcut around that gap: an explicit,
-- ops-maintained registry of queries worth periodically re-checking, the same "watched query
-- list" pattern real DB reliability teams already use when they don't have (or don't trust) fully
-- automatic traffic discovery. `workers/sweep_enumerator/handler.py` reads the enabled rows here
-- and enqueues one real agent/main.py-schema SQS message per row -- `agent/main.py`'s own
-- `SqlProbe.explain_analyze()` still does the REAL measurement downstream, unchanged; this table
-- only decides WHICH query gets that treatment on a given tick, nothing about the measurement
-- itself is faked.
--
-- No TTL: this is small, operator-maintained configuration, not high-volume transient data (same
-- class as `procedures`, which also has no TTL).

CREATE TABLE watched_queries (
  watched_query_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_id          STRING NOT NULL,
  target_cluster_id STRING NOT NULL,
  table_name        STRING NOT NULL,
  query_text        STRING NOT NULL,
  enabled           BOOL NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX watched_queries_enabled_idx ON watched_queries (enabled);

-- Read-only, matching engram_probe/engram_reader's discipline: the enumerator Lambda only ever
-- lists candidates, it never writes here (populating/editing the registry is an operator action,
-- not something the automated sweep path should be able to do to itself).
CREATE ROLE IF NOT EXISTS engram_sweep_enumerator;
GRANT SELECT ON watched_queries TO engram_sweep_enumerator;
