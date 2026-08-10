-- Engram · Migration 001 — core schema.  [PLUMBER] · frozen Day 3 (design/02-low-level-design.md §6.2)
--
-- PREREQUISITE, run once, NOT part of this file: SET CLUSTER SETTING feature.vector_index.enabled = true;
-- ORDER MATTERS, do not reorder tables: remediation_actions MUST precede approvals — approvals.action_id
--   is a hard FK into it (LLD §6.2 note (a): the reverse order made migration 001 fail outright).
-- EVERY FK to a TTL'd parent carries an explicit ON DELETE action (LLD §6.2 note (b), invariant #7) —
--   omitting one makes the TTL job on that parent error silently in the background.
-- Roles/grants are migration 002, not here (LLD §6.2 note (c): GRANT ... ON ALL TABLES only binds
--   tables that exist at grant time, so it must run after every CREATE TABLE it needs to cover).
-- The vector index is migration 003, created AFTER the corpus is seeded (invariant #1 — IMPORT INTO
--   is unsupported on a table with a live vector index).
-- LangGraph checkpoint tables are NOT created here — AsyncCockroachDBSaver.setup() creates them at
--   bootstrap; their TTL is migration 004, applied immediately after setup() on the EMPTY cluster.

CREATE TABLE entities (
  entity_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_id      STRING NOT NULL,
  kind          STRING NOT NULL,              -- cluster|database|table|query|backup_job|alert
  name          STRING NOT NULL,
  attributes    JSONB NOT NULL DEFAULT '{}'::JSONB,
  version       INT NOT NULL DEFAULT 1,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (scope_id, kind, name)
);

CREATE TABLE tasks (
  task_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_id            STRING NOT NULL,
  task_type           STRING NOT NULL,        -- sweep|incident|manual
  status              STRING NOT NULL DEFAULT 'pending',
                      -- pending|running|awaiting_approval|blocked|completed|failed|parked
  trigger             STRING NOT NULL,        -- eventbridge|webhook|manual
  target_cluster_id   STRING,
  incident_fingerprint STRING,                -- sha256(normalized query/metric signature); NULL for non-incident tasks
  checkpoint_thread_id STRING,
  parent_task_id      UUID REFERENCES tasks(task_id) ON DELETE SET NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
) WITH (ttl_expire_after = '90 days'::interval);
CREATE INDEX tasks_status_idx ON tasks (status, created_at);
-- Incident dedupe (webhook vs sweep race): one ACTIVE incident per (cluster, fingerprint).
-- Partial unique indexes are supported (docs, partial-indexes): the constraint covers only
-- in-flight statuses, so completed/failed rows release the slot for future occurrences.
CREATE UNIQUE INDEX tasks_active_incident_idx ON tasks (target_cluster_id, incident_fingerprint)
  WHERE task_type = 'incident'
    AND status IN ('pending','running','awaiting_approval','blocked');

CREATE TABLE agent_leases (
  lease_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID NOT NULL UNIQUE REFERENCES tasks(task_id) ON DELETE CASCADE,
  holder_id    STRING NOT NULL,               -- ECS task ARN / process id
  fence_token  BIGINT NOT NULL DEFAULT 0,
  acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  renewed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at   TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '60 seconds'
);

CREATE TABLE working_memory (
  task_id        UUID PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
  scope_id       STRING NOT NULL,
  state_json     JSONB NOT NULL,              -- full AgentState snapshot
  checkpoint_ref STRING,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
) WITH (ttl_expire_after = '7 days'::interval);

CREATE TABLE observations (
  observation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_id         STRING NOT NULL,
  task_id          UUID REFERENCES tasks(task_id) ON DELETE CASCADE,
  target_cluster_id STRING,
  source           STRING NOT NULL,           -- mcp|ccloud|cloudwatch|sql_probe|webhook
  kind             STRING NOT NULL,           -- metric|schema|query_stats|running_query|backup|alert
  observed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  fingerprint      STRING,
  payload          JSONB NOT NULL
) WITH (ttl_expire_after = '30 days'::interval);
CREATE INDEX observations_scope_time_idx ON observations (scope_id, observed_at DESC);
CREATE INDEX observations_fingerprint_idx ON observations (fingerprint);

CREATE TABLE memory_items (
  item_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_id      STRING NOT NULL,
  class         STRING NOT NULL,   -- query_fingerprint|episode|procedure|skill
  entity_id     UUID REFERENCES entities(entity_id),
  source_row_id UUID,              -- observations.observation_id | procedures.procedure_id
  content       STRING NOT NULL,
  embedding     VECTOR(1024),      -- NULL until embedded (backfill worker fills)
  provenance    JSONB NOT NULL DEFAULT '{}'::JSONB,  -- {task_id, tool_call_ids, skill_sha, model_id}
  status        STRING NOT NULL DEFAULT 'active',    -- active|draft|retired
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- CREATED AFTER SEEDING (invariant #1) — migration 003:
-- VECTOR INDEX mem_vec_idx (scope_id, embedding vector_cosine_ops)
--   WITH (min_partition_size=16, max_partition_size=128);
CREATE INDEX memory_items_class_idx ON memory_items (scope_id, class, updated_at DESC);
-- Verified 2026-08-03 (docs/phase0-verification.md §1.2a): the C-SPANN index does NOT
-- serve plain scope_id predicates — a non-ANN scoped lookup full-scans. recall.py's hard
-- filter is (scope_id, status), which class_idx does not cover. Add it explicitly:
CREATE INDEX memory_items_scope_status_idx ON memory_items (scope_id, status);

CREATE TABLE procedures (
  procedure_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_id      STRING NOT NULL,
  name          STRING NOT NULL,
  description   STRING NOT NULL,
  steps         JSONB NOT NULL,               -- [{action_kind, parameters, expected_effect}]
  outcome_stats JSONB NOT NULL DEFAULT '{"successes":0,"attempts":0}'::JSONB,
  confidence    FLOAT8 NOT NULL DEFAULT 0,    -- Wilson LB × time decay (invariant #10)
  status        STRING NOT NULL DEFAULT 'draft',  -- draft|active|retired
  sources       JSONB NOT NULL DEFAULT '[]'::JSONB, -- [memory_item_id …]
  created_by    UUID REFERENCES tasks(task_id) ON DELETE SET NULL,  -- a procedure OUTLIVES its task
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX procedures_scope_conf_idx ON procedures (scope_id, status, confidence DESC);

CREATE TABLE decisions (
  decision_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id           UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  scope_id          STRING NOT NULL,
  node              STRING NOT NULL,          -- observe|recall|reason|gate|act|measure
  model_id          STRING NOT NULL,          -- Ollama Cloud reasoning model id | embed-english-v3.0
  model_version     STRING,
  input_fingerprint STRING,
  reasoning         JSONB NOT NULL DEFAULT '{}'::JSONB,
  citations         JSONB NOT NULL DEFAULT '[]'::JSONB,  -- [{memory_item_id, score, source}]
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
) WITH (ttl_expire_after = '90 days'::interval);
CREATE INDEX decisions_task_idx ON decisions (task_id, created_at);

CREATE TABLE tool_calls (
  tool_call_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id        UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  decision_id    UUID REFERENCES decisions(decision_id),
  tool           STRING NOT NULL,             -- mcp|ccloud|cloudwatch|sql_probe|sql_operator|llm|recipe|memory|s3
  operation      STRING NOT NULL,
  arguments      JSONB NOT NULL,
  result_summary STRING,
  result_uri     STRING,                      -- s3://… for large artifacts (invariant #11)
  content_sha256 STRING,
  status         STRING NOT NULL,             -- ok|error|timeout
  error_code     STRING,
  latency_ms     INT,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ
) WITH (ttl_expire_after = '90 days'::interval);
CREATE INDEX tool_calls_task_idx ON tool_calls (task_id, started_at);

-- remediation_actions MUST be created BEFORE approvals (note (a) above): approvals.action_id
-- is a hard FK into it. The declaration order is part of the frozen contract.
CREATE TABLE remediation_actions (
  action_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id           UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  scope_id          STRING NOT NULL,
  target_cluster_id STRING NOT NULL,
  action_kind       STRING NOT NULL,        -- allowlist only (recipe_renderer)
  recipe_version    STRING NOT NULL,
  parameters        JSONB NOT NULL,
  rendered_sql      STRING NOT NULL,        -- human-reviewable, idempotent
  idempotency_key   STRING NOT NULL UNIQUE, -- sha256(cluster_id ‖ canonical_change) — THE exactly-once guarantee
  status            STRING NOT NULL,        -- proposed|approved|applied|failed|skipped|reconciled
  approval_id       UUID,                   -- no FK: avoids cycle with approvals
  measured_before   JSONB,
  measured_after    JSONB,
  outcome           STRING,                 -- success|failure|noop
  applied_by        STRING,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at        TIMESTAMPTZ
) WITH (ttl_expire_after = '90 days'::interval);
CREATE INDEX remediation_actions_status_idx ON remediation_actions (status, created_at);

-- approvals has inbound FKs to TWO TTL'd parents (tasks, remediation_actions), so BOTH
-- carry an explicit ON DELETE action or the TTL job fails silently (note (b) above).
CREATE TABLE approvals (
  approval_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
  action_id    UUID NOT NULL REFERENCES remediation_actions(action_id) ON DELETE CASCADE,
  status       STRING NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|expired
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at   TIMESTAMPTZ,
  decided_by   STRING,
  channel      STRING,                             -- dashboard|cli|webhook
  comment      STRING
);
CREATE INDEX approvals_status_idx ON approvals (status, requested_at);

-- embedding cache (D9): never embed the same content twice
CREATE TABLE embedding_cache (
  content_sha256 STRING PRIMARY KEY,
  embedding      VECTOR(1024) NOT NULL,
  model_id       STRING NOT NULL,  -- records 'embed-english-v3.0'. The DIMENSION is pinned by
                                   -- the column type; this column pins the VECTOR SPACE, so a
                                   -- provider/model change is detectable instead of silently
                                   -- mixing incomparable 1024-d spaces (HLD §3 D9/D12).
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
) WITH (ttl_expire_after = '180 days'::interval);

-- Read-only dashboard views (frozen surface, LLD §11)
CREATE VIEW v_recent_tasks AS
  SELECT task_id, task_type, status, trigger, target_cluster_id, created_at, updated_at
  FROM tasks WHERE created_at > now() - INTERVAL '7 days'
  ORDER BY created_at DESC LIMIT 100;
CREATE VIEW v_action_feed AS
  SELECT a.action_id, a.task_id, a.scope_id, a.action_kind, a.status, a.outcome,
         a.rendered_sql, a.created_at, ap.status AS approval_status, ap.decided_by
  FROM remediation_actions a LEFT JOIN approvals ap ON ap.action_id = a.action_id
  WHERE a.created_at > now() - INTERVAL '7 days'
  ORDER BY a.created_at DESC LIMIT 100;
CREATE VIEW v_memory_inspector AS
  SELECT i.item_id, i.class, i.content, i.provenance, i.created_at,
         p.confidence, p.status AS procedure_status
  FROM memory_items i LEFT JOIN procedures p ON p.procedure_id = i.source_row_id
  WHERE i.class IN ('episode','procedure','query_fingerprint')
  ORDER BY i.created_at DESC LIMIT 100;
