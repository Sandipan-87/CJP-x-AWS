# Engram — Low-Level Design (LLD)

> **Status:** APPROVED-for-build · **Owner:** [BRAINS] (agent core) + [PLUMBER] (data) + [ILLUSIONIST] (frontend) — sections are ownership-tagged
> **Inputs:** `design/01-high-level-design.md` (HLD — revised 2026-08-11 for D13), `CLAUDE.md` invariants. `design/03-adr.md` is cited throughout this document but **does not exist yet** (`docs/blocked-register.md` §5); ADR-001 (Ollama-primary) is **reinstated in substance** by D13, ADR-002 (Titan V2) remains **superseded** by the Cohere decision.
> **Revised 2026-08-11 — reasoning primary swapped again (D13):** reasoning = **Ollama Cloud `minimax-m3:cloud`** (ladder Ollama Cloud → Groq → Together AI, behind the unchanged `LLMProvider` ABC) · embeddings = **Cohere `embed-english-v3.0`, natively exactly 1024-dim, no ladder** · AWS anchor = **S3 `engram-agent-artifacts`** via `boto3`. **No Bedrock client is constructed anywhere on any path.**
> **Contracts frozen by this document:** tool-call JSON schemas (Day 4), SQL schema (Day 3), SSE surface (Day 5). Any change after freeze = changelog entry + re-freeze.
> **Conventions:** every DB write carries `scope_id`; every write is idempotent or CAS-guarded; the model never emits SQL/shell; errors are typed (taxonomy §14).

---

## 1. Repository layout (monorepo)

```
engram/
├── agent/                          # [BRAINS] — the Fargate task
│   ├── main.py                     # entrypoint: config, pools, queue consumer, signals, health
│   ├── config.py                   # pydantic-settings; env contract §2
│   ├── graph.py                    # LangGraph assembly, conditional edges, checkpointer wiring
│   ├── state.py                    # AgentState TypedDict + nested schemas (§3)
│   ├── nodes/
│   │   ├── observe.py              # collect + fingerprint + persist observations/entities
│   │   ├── recall.py               # embed → ANN → hybrid re-rank → context bundle
│   │   ├── reason.py               # minimax-m3 hypothesis + falsification loop → Proposal
│   │   ├── gate.py                 # ledger txn + approval wait
│   │   └── act_measure.py          # ledger-first apply + measure + outcome txn (§8)
│   ├── providers/
│   │   ├── base.py                 # LLMProvider, EmbeddingProvider ABCs
│   │   ├── ollama_cloud_llm.py     # PRIMARY reasoning — minimax-m3:cloud, strips <mm:think> tags
│   │   ├── cohere_embed.py         # embed-english-v3.0 @ natively 1024-d — the ONLY embedder
│   │   ├── (groq_llm.py)           # ladder rung 2 — OpenAI-compatible, fastest rung to promote to
│   │   └── (together_llm.py)       # ladder rung 3, last resort — same wire format as groq_llm. NO Bedrock rung exists
│   ├── tools/
│   │   ├── mcp_tool.py             # managed MCP adapter (ro)
│   │   ├── ccloud_tool.py          # ccloud CLI adapter (ro, enum-only)
│   │   ├── cloudwatch_tool.py      # get/put metrics
│   │   ├── sql_probe.py            # target read-only (EXPLAIN ANALYZE)
│   │   ├── sql_operator.py         # target allowlisted DDL
│   │   ├── recipe_renderer.py      # action_kind enum → parameterised SQL templates
│   │   └── s3_artifact.py          # large artifact upload + content hash
│   ├── memory/
│   │   ├── db.py                   # psycopg3 async pool + typed DAO (§6.1)
│   │   ├── leases.py               # FOR UPDATE lease + fence CAS (§6.4)
│   │   ├── actions.py              # idempotency keys, ledger state machine (§8)
│   │   ├── embeddings.py           # write-path embedding + fingerprint cache
│   │   ├── recall.py               # the ONLY ANN code path (§6.5)
│   │   ├── scoring.py              # Wilson LB, recency, affinity, hybrid (§6.6)
│   │   └── audit.py                # AS OF SYSTEM TIME replay (§6.7)
│   ├── skills/                     # CockroachDB Agent Skills (chunked → memory_items)
│   └── telemetry.py                # OTel spans + CloudWatch metrics (§12)
├── workers/                        # [PLUMBER] — Lambda functions
│   ├── consolidator/  decayer/  embedding_backfill/
│   ├── approvals/     metrics_proxy/   alert_ingest/
│   └── common/         # shared DAO + config (thin layer, no agent imports)
├── dashboard/                      # [ILLUSIONIST] — Next.js App Router
├── infra/                          # [PLUMBER] — AWS CDK (Python)
├── db/migrations/                  # [PLUMBER] — 001_…, 002_… (frozen Day 3)
├── scenarios/                      # [PLUMBER] — incident simulator (§13)
├── tests/                          # unit + integration + fencing + e2e (§14)
└── scripts/                        # verify_ollama.py, verify_cohere.py, verify_s3.py,
                                    #   verify_mcp.py, run_sql.py  (no verify_bedrock.py)
```

---

## 2. Configuration contract  [BRAINS + PLUMBER]

`.env` (dev) ↔ Secrets Manager `secret/engram/*` (prod). Loaded by `config.py` (pydantic-settings, strict). **`.env.example` is the authoritative key list** — a committed template holding placeholders only; this table and that file must not drift.

| Env var | Prod secret | Purpose |
|---|---|---|
| `ENGRAM_MEMORY_DSN` | `memory-dsn` | psycopg3 async pool (engram_agent) |
| `ENGRAM_READER_DSN` | `memory-reader-dsn` | dashboard SSE (engram_reader, ro) |
| `ENGRAM_TARGET_PROBE_DSN` | `target-dsn-probe` | probe role (ro) |
| `ENGRAM_TARGET_OPERATOR_DSN` | `target-dsn-operator` | operator role (allowlisted DDL) |
| `OLLAMA_API_KEY` · `OLLAMA_BASE_URL` | `ollama-api-key` | reasoning, primary rung (D13) · `https://ollama.com` |
| `COHERE_API_KEY` | `cohere-api-key` | embeddings — **the only embedding provider; there is no ladder** |
| `GROQ_API_KEY` / `TOGETHER_API_KEY` | — | ladder rungs 2 and 3; **left unset unless a rung is promoted.** No rung is Bedrock |
| `CRDB_MCP_URL` · `CRDB_MCP_TOKEN` · `CRDB_MCP_CLUSTER_ID` | `mcp-*` | managed MCP (ro) |
| `CCLOUD_TOKEN` | `ccloud-token` | ccloud CLI (ro SA) · **plus** a Cluster-Admin-scoped key for the backups REST API on the *target* cluster |
| `ENGRAM_LLM_PROVIDER` · `ENGRAM_LLM_MODEL` | — | `ollama` · `minimax-m3:cloud` — **both UNVERIFIED (tag and endpoint shape) — probe before the Day-4 tool-schema freeze.** A ladder change swaps these two values and nothing else: config, not code |
| `ENGRAM_LLM_TIMEOUT_S` · `ENGRAM_LLM_MAX_RETRIES` | — | 90 · 3 |
| `ENGRAM_LEASE_TTL_S` · `ENGRAM_LEASE_RENEW_S` | — | 60 · 15 |
| `ENGRAM_QUEUE_URL` | — | SQS URL |
| `ENGRAM_S3_BUCKET` · `AWS_REGION` | — | `engram-agent-artifacts` · `us-east-1` — **`boto3` is constructed for S3 and nothing else** |
| `ENGRAM_EMBED_PROVIDER` · `ENGRAM_EMBED_MODEL` · `ENGRAM_EMBED_DIMS` | — | `cohere` · `embed-english-v3.0` · `1024`, asserted at startup |

**1024 is a schema constraint, not a tuning knob.** `VECTOR(1024)` plus the C-SPANN `vector_cosine_ops` index fix the width; `embed-english-v3.0` emits exactly 1024 dims natively, so **no truncation, padding or projection exists in the write path**. Changing `ENGRAM_EMBED_MODEL` changes the *vector space*, not the width — a full re-embed, never a config tweak (invariant #2, HLD §9.2).

**`input_type` is a required per-call argument, not config:** `search_document` on the write path, `search_query` on recall. Collapsing the two degrades recall **silently** — no exception is raised, the answers just get worse — so the call sites in `memory/embeddings.py` and `memory/recall.py` each pass it explicitly.

Startup self-tests (fail fast, exit 1): DB reachable · **Cohere returns a vector with `len(vec) == 1024`** — asserted, not logged, because a wrong width must never reach a `VECTOR(1024)` column · **Ollama Cloud reachable + strict-JSON tool-call probe, with `<mm:think>` tag-stripping exercised** · MCP `list_clusters` · **S3 put/get/content-hash round-trip on `engram-agent-artifacts`** · lease acquire/release round-trip.

---

## 3. Agent state (LangGraph `AgentState`)  [BRAINS]

TypedDict, JSON-serializable (checkpoint-safe). `thread_id = task_id`.

```python
class AgentState(TypedDict):
    task_id: str
    scope_id: str
    target_cluster_id: str
    trigger: str                       # eventbridge | webhook | manual
    phase: str                         # observe|recall|reason|gate|act_measure|done|parked
    observations: list[Observation]    # recent, summarized
    incident_fingerprint: str | None   # canonical query-shape / metric signature
    recall_bundle: RecallBundle | None # items + scores + citations
    proposal: Proposal | None          # typed, validated (reason output)
    approval: Approval | None          # gate output
    action: ActionLedgerRow | None     # act output (ledger status)
    measurement: Measurement | None    # before/after
    error: TypedError | None           # last typed error (§14)
    model_meta: dict                   # model_id, version, token usage per call

class Proposal(TypedModel):           # pydantic, frozen contract Day 4
    reasoning: str                    # REQUIRED: audit-grade rationale INSIDE the JSON.
                                      # We never depend on a vendor "thinking" channel: it is
                                      # optional, provider-specific, and unreliable: minimax-m3
                                      # (primary, D13) is reported to leak <mm:think> tags into
                                      # content instead of populating message.thinking (ollama
                                      # #16632, vLLM #45687) -- unverified by us, load-bearing now.
                                      # The model must think here, where the validator sees it.
    hypothesis: str
    falsification: list[Evidence]     # tool_call_id + result_summary + index-recommendation match
    action_kind: ActionKind           # enum: create_index | analyze_table
    parameters: dict                  # typed: table, columns, index_name, ...
    expected_effect: str
    risk: Literal["low","medium","high"]
    confidence: float                 # 0..1
    citations: list[Citation]         # memory_item_id + score + source
```

---

## 4. Graph wiring  [BRAINS]

```
observe ──► recall ──► reason ──► gate ──► act_measure ──► (done)
             │          │          │
             ▼          ▼          ▼
         (no anomaly → done)  (reject/expire → done)   (measure fail → reason)
```

- Conditional edges: `observe` → `done` if no anomaly; `gate` → `done` on reject/expiry, → `reason` (re-plan) if measurement fails.
- Checkpointer: `AsyncCockroachDBSaver` on the memory cluster; every node return checkpoints state (invariant: no side effect without a checkpoint commit).
- Long waits (gate, approvals) are **polls, not LangGraph interrupts** — keeps checkpoint semantics trivial and demo-safe.
- Concurrency: one incident at a time per task; `task_id` from SQS message group; `tasks` UNIQUE insert dedupes.

---

## 5. Nodes — LLD  [BRAINS]

### 5.1 `observe(node)`
1. Collect: MCP (`show_running_queries`, `list_tables`, `get_table_schema`), probe SQL (`EXPLAIN ANALYZE` on scenario query via `sql_probe`), CloudWatch trends (target metric export), ccloud (`cluster info` only — the backup signal comes from the Cloud REST API when entering Act, **not** `ccloud cluster backup list`, which does not exist; §5.5).
2. Fingerprint: normalize SQL (lowercase, collapse literals) → `sha256`; or metric signature.
3. Anomaly rule (deterministic): probe latency > threshold AND plan shows seq scan where index candidate exists → incident.
4. One txn: `INSERT tasks(incident)` + `INSERT observations` (fingerprint, payload) + upsert `entities` (table/cluster/query). Embed **query fingerprint** via Cohere with **`input_type='search_document'`** (write path) → `memory_items` (class=query_fingerprint) at write time (invariant #2).
5. Emit telemetry: `sweep_cycle_ms`, `observations_written`.
6. Errors: MCP timeout (typed) → degrade to probe-only observation; never fail the sweep on a single source.

### 5.2 `recall(node)`
1. Query embedding via **Cohere `embed-english-v3.0` with `input_type='search_query'`** (cache hit on fingerprint → skip the API call entirely; a hit removes the largest term in the recall latency budget, HLD §9.3).
2. ANN (single choke point `recall.py`): `WHERE scope_id=$1 AND status='active' ORDER BY embedding <=> $2 LIMIT 20`, beam 64 for remediation class.
3. Enrich + hybrid score (§6.6) → hard filters (`confidence ≥ 0.15`, `status='active'`) → top-k bundle with citations.
4. Persist: `decisions(node='recall', citations, scores)`; telemetry `memory_recall_latency_p99`, `recall_top1_score`.
5. Return bundle; if bundle empty → incident proceeds with zero-shot prompt (and `recall_hit_rate` counts a miss).

### 5.3 `reason(node)`
1. Prompt = system (role, safety rules, output schema) + context bundle (ranked, cited) + entity model + observations.
2. **Ollama Cloud call** (`minimax-m3:cloud`, primary rung, D13) via `LLMProvider.complete` with the tools schema (falsification tool = `explain_query`). **Strip `<mm:think>` tags from the content before JSON parsing** — this is load-bearing on the primary path, not a defensive extra — validation runs on the JSON, and the audit rationale is the required `reasoning` field, never a thinking channel.
3. Falsification loop (rounds < 3) — **there is no hypopg**: CockroachDB cannot `EXPLAIN` an arbitrary hypothetical index, so the pre-gate evidence is (a) **schema validation** (columns/table confirmed via MCP `get_table_schema` — no fabricated objects) plus (b) the optimizer's own signal: run `EXPLAIN` on the **original slow SELECT** (MCP `explain_query`, or `EXPLAIN ANALYZE` via the probe role) — **never** `EXPLAIN` the proposed DDL (`EXPLAIN CREATE INDEX` only shows the index-build plan and says nothing about its effect on the query) — and parse the plan for: seq scan + filter where an index is expected, the **"index recommendations" section** CockroachDB emits when a missing index is detected (generated by the optimizer's internal hypothetical-index analysis; see cockroach issues #73817/#107958), and whether the proposed `(table, columns)` matches that recommendation. Alignment with the recommendation = pre-gate falsification evidence; mismatch = revise hypothesis. Day-1 check: confirm the MCP `explain_query` response includes the recommendations section (else use the probe role's `EXPLAIN ANALYZE`). The real effect is still proven by before/after `EXPLAIN ANALYZE` in Act & Measure.
4. Validate output with pydantic `Proposal`; on schema failure → 1 repair turn (schema error string) → else typed `LLMSchemaError` → park.
5. Persist `decisions(node='reason', reasoning, model_id, token_usage)`.
6. Telemetry: `llm_latency_ms`, `llm_token_usage`, `llm_failures` (on error).

### 5.4 `gate(node)`
1. ONE txn (invariant #6): `decisions(intent)` + `remediation_actions(status='proposed', idempotency_key, rendered_sql, parameters)` + `approvals(status='pending')`.
2. SSE push via dashboard feed (no DB write from dashboard needed).
3. Wait: poll `approvals` every 2 s (single-row indexed read) up to `ENGRAM_APPROVAL_TIMEOUT_S` (default 600).
4. On `approved` → proceed; `rejected`/`expired` → outcome row (`skipped`) + episode memory + done.
5. Telemetry: `gate_wait_ms`, `blocked_by_backup_gate` (checked at Act entry).

### 5.5 `act_measure(node)` — ledger-first protocol (ADR-004, full detail §8)
1. Re-verify backup gate via **`GET /api/v1/clusters/{id}/backups`** (NOT `ccloud cluster backup list` — that subcommand does not exist in ccloud 0.6.12) →
   - **(a) empty list** (fresh cluster, no managed backup yet) → **block + park** — safe default, never proceed on an unverified backup; dashboard shows the reason; auto-retry on next sweep.
   - **(b) entries but none within the required window** → block + park, same override path.
   - **(c) recent backup** → proceed.
   - Human override: explicit `override_backup_gate=true` flag on the task, recorded in `decisions` (auditable) — the gate can be waived, never silently bypassed.
   - **Verified 2026-08-03:** returns `200 {"backups": [...]}` on a **Basic** cluster (fixture `fixtures/cloudapi-backups-basic.json`), and `403 unauthorized` when the SA key is not scoped to that cluster — ours currently 403s on the *target* and 200s on *memory*. Requires **Cluster Admin**, not Cluster Operator; grant + rescope at Day-1 (ADR-006). On a fresh Basic cluster the list is **empty**, so path (a) is the default — that is the refusal beat, and we must not claim the allow-path was tested unless it was.
2. Measure before: probe `EXPLAIN ANALYZE` → `measured_before` (latency, rows scanned, plan JSON).
3. **Ledger txn** (memory): `decisions(act)` + `remediation_actions(status='applied', measured_before)` — commit.
4. Apply DDL via `sql_operator` using rendered idempotent recipe (`CREATE INDEX IF NOT EXISTS …`, `ANALYZE`).
5. Measure after: probe again → `measured_after`.
6. **Outcome txn** (memory): update `remediation_actions(outcome, measured_after, applied_at)` + `procedures` stats (`successes`/`attempts`, recompute Wilson) + `memory_items(episode, provenance, embedding)` + `approvals(decided_*)`.
7. Telemetry: `time_to_remediation`, `exactly_once_conflicts_detected` (on reconcile), `outcome`.

**Signal handling (main.py):** SIGTERM → flush checkpoint, release lease, exit 0 (within ECS grace 25 s). SIGKILL → lease expiry + checkpoint tables cover it.

---

## 6. Memory layer  [PLUMBER]

### 6.1 `memory/db.py` — psycopg3 async pool
- Pool size 5 (single task, low concurrency), `connect_timeout=10`, statement timeout 30 s.
- Connection-loss handling: pool `replace` on OperationalError; in-flight txn retried once with fresh connection **only if the txn was read-only**; write txns are never blindly retried (idempotency/reconcile instead).
- DAO methods (all take `scope_id`):

| Method | Effect |
|---|---|
| `insert_task`, `update_task_status` | tasks lifecycle — `insert_task` takes the pre-computed `incident_fingerprint`; a UniqueViolation on `tasks_active_incident_idx` is **not an error**: return the existing task_id so the caller attaches its observation to the in-flight incident instead of spawning a second agent |
| `insert_observation`, `upsert_entity` | operational + entity memory |
| `insert_memory_item(content, class, embedding, provenance)` | semantic/episodic/procedural |
| `recall_ann(scope_id, vec, limit, beam)` | the only ANN query (§6.5) |
| `get_candidate_details(ids)` | enrich for re-rank |
| `acquire_lease / renew_lease / release_lease / takeover_lease` | §6.4 |
| `insert_decision`, `insert_tool_call` | audit |
| `insert_remediation_action`, `update_remediation_status`, `get_by_idempotency_key` | ledger |
| `insert_approval`, `decide_approval(…CAS…)`, `poll_approval` | gate |
| `update_procedure_stats`, `recompute_confidence` | decayer |
| `audit_replay(task_id, as_of)` | AS OF SYSTEM TIME |
| `dashboard_*` (cursor reads) | §11 |

### 6.2 Schema — `db/migrations/001_engram_schema.sql` (frozen Day 3)

> ✅ **THE DDL BELOW IS THE FIXED STATE — all three defects applied 2026-08-10.** The diagnoses are kept because the *reasons* are load-bearing: they are why the statement order and the `ON DELETE` actions cannot be "tidied up" later.
>
> **(a) Forward FK reference — FIXED by ordering.** `approvals.action_id REFERENCES remediation_actions(action_id)` used to be declared *before* `remediation_actions` was created, so migration `001` failed outright. `remediation_actions.approval_id` already carried a "no FK: avoids cycle" note, so the cycle was known — but the other direction was left as a hard FK. **`remediation_actions` is now created before `approvals`, and the order is part of the frozen contract.**
>
> **(b) Row-level TTL on `tasks` would fail against inbound FKs — FIXED by explicit `ON DELETE` on every child.** `tasks` has `ttl_expire_after = '90 days'`, and its children carried no `ON DELETE` action, while **`procedures.created_by` has no TTL at all** (procedures are indefinite by design). When the TTL job deleted a 90-day-old task, that FK blocked the delete and the TTL job errored — silently, in the background. `decisions`/`tool_calls`/`remediation_actions` share the 90-day window but their TTL jobs run *independently*, so ordering was never guaranteed either. Applied (invariant #7): **`ON DELETE CASCADE`** on `observations`, `decisions`, `tool_calls`, `remediation_actions`, `working_memory`, `agent_leases` **and `approvals`** — the last one is not named in `docs/invariants.md` §7 but has the same inbound-FK problem on both `task_id` and `action_id`, so it gets the same treatment. **`ON DELETE SET NULL`** on `procedures.created_by` and `tasks.parent_task_id` — a procedure must outlive the task that induced it. T12 back-dates a task and asserts the TTL job actually reclaims it.
>
> **(c) Grants bound only existing tables — FIXED in migration `002`.** `GRANT … ON ALL TABLES IN SCHEMA public` binds only tables existing at grant time, so `003`/`004` tables were uncovered. Migration `002` now also runs `ALTER DEFAULT PRIVILEGES`. Blanket `DELETE` is **dropped from `engram_agent`**: Row-Level TTL does the deleting. The one exception is `agent_leases`, which gets a table-scoped `DELETE` because the SIGTERM release path in §6.4 is a real `DELETE` — dropping it there would strand the lease for a full 60 s expiry and slow the kill-and-resume demo.

```sql
-- PREREQUISITE (run before migration): SET CLUSTER SETTING feature.vector_index.enabled = true;
-- ORDER MATTERS: seed corpus rows, THEN create the vector index (invariant #1).
-- ORDER ALSO MATTERS FOR FKs: remediation_actions MUST precede approvals (note (a) above).
-- EVERY FK to a TTL'd parent carries an explicit ON DELETE action (note (b), invariant #7).

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
-- CREATED AFTER SEEDING (invariant #1):
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

-- remediation_actions MUST be created BEFORE approvals (note (a)): approvals.action_id
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
-- carry an explicit ON DELETE action or the TTL job fails silently (note (b)).
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

-- LangGraph checkpoint tables (checkpoints / checkpoint_blobs / checkpoint_writes -- UNPREFIXED,
-- corrected 2026-08-11 Phase 3: the original "langgraph_"-prefixed names here were an unverified
-- guess, contradicted by reading the actual installed langchain-cockroachdb==0.3.0 source) are
-- created by AsyncCockroachDBSaver.setup().
-- CRITICAL (docs v26.2, row-level TTL): adding ttl_expire_after to an EXISTING table triggers
-- a FULL TABLE REWRITE — new hidden crdb_internal_expiration column + backfill of every row.
-- Therefore: run saver.setup() at bootstrap on an EMPTY cluster, then IMMEDIATELY apply TTL,
-- BEFORE any checkpoint data exists (migration 004). Corrected 2026-08-11: the mechanism is NOT
-- a hand-rolled `ttl_expire_after` ALTER -- the library ships its own `saver.aenable_ttl()`,
-- which uses `ttl_expiration_expression` against a `created_at` column `setup()` already adds,
-- specifically to avoid the full-rewrite this paragraph warns about. Migration 004 inlines that
-- exact SQL. Never ALTER TTL on them again once hot (invariant #7).

-- Read-only dashboard views (frozen surface, §11)
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

-- No blanket DELETE for engram_agent (note (c)): Row-Level TTL does the deleting. The single
-- exception is agent_leases, whose SIGTERM release path (§6.4) is a real DELETE.
CREATE ROLE engram_agent;   GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO engram_agent;
                            GRANT DELETE ON agent_leases TO engram_agent;
CREATE ROLE engram_reader;  GRANT SELECT ON v_recent_tasks, v_action_feed, v_memory_inspector, observations TO engram_reader;
```

Migrations: `002_…` (grants — `GRANT … ON ALL TABLES` **plus `ALTER DEFAULT PRIVILEGES`**, so the tables `003`/`004` create are covered too), `003_…` (vector index AFTER seed), `004_…` (LangGraph checkpoint TTL). Freeze Day 3.

### 6.3 Seeding & index creation (runbook, P0-P1 + P2-P1)
1. `SET CLUSTER SETTING feature.vector_index.enabled = true;`
2. Migrations 001–002.
3. Bootstrap checkpoints: run `AsyncCockroachDBSaver.setup()` once (creates the three checkpoint tables on the **empty** cluster), then immediately migration 004 — TTL on all three **while empty** (§6.2). Never ALTER them later.
4. Seed corpus (episodes, procedure descriptions, skills) **without** embedding, then the backfill worker embeds via **Cohere `embed-english-v3.0` with `input_type='search_document'`** (asserting `len(vec) == 1024` before any write), **then** migration 003 creates the C-SPANN index. No large batch inserts into a vector-indexed table.

### 6.4 `memory/leases.py` — lease + fence (invariant #5)

```sql
-- acquire (blocking; safe take-or-create, fence token stays monotonic):
BEGIN;
UPDATE agent_leases
   SET holder_id=$2, fence_token=fence_token+1,
       acquired_at=now(), renewed_at=now(),
       expires_at=now()+INTERVAL '60 seconds'
 WHERE task_id=$1 AND expires_at < now();   -- CRITICAL: ONLY expired leases may be taken.
-- rowcount 1 → we took over an EXPIRED lease (token bumped). rowcount 0 → live lease OR no row.
INSERT INTO agent_leases (task_id, holder_id, fence_token,
                          acquired_at, renewed_at, expires_at)
VALUES ($1, $2, 1, now(), now(), now()+INTERVAL '60 seconds')
ON CONFLICT (task_id) DO NOTHING;           -- rowcount 1 → fresh acquire; 0 → live holder exists
COMMIT;
-- if total affected rows == 1 → we hold the lease. Else back off with jitter (1–3 s) and retry.
-- Why this is safe: the row lock on task_id serialises concurrent takeovers, so at most one
-- transaction sees expires_at < now() and bumps the token; the INSERT path cannot clobber an
-- existing row (UNIQUE task_id) and never resets a live holder's fence_token.
-- renew (CAS, every 15 s):
UPDATE agent_leases SET renewed_at=now(), expires_at=now()+INTERVAL '60 seconds'
 WHERE task_id=$1 AND holder_id=$2 AND fence_token=$3;   -- rowcount 0 → lost lease → park
-- release (SIGTERM path):
DELETE FROM agent_leases WHERE task_id=$1 AND holder_id=$2;
```

**Write-time fencing:** every mutating DAO call accepts `holder_id`+`fence_token` and the SQL adds `AND fence_token = $expected` on the owning row where applicable (or the caller verifies lease before write). A stale holder's write affects 0 rows → typed `StaleLeaseError` → park.

### 6.5 `memory/recall.py` — the ONLY ANN path (invariant #3)

```sql
SELECT item_id, class, content, provenance,
       1 - (embedding <=> $2::VECTOR(1024)) AS similarity,
       (embedding <=> $2::VECTOR(1024)) AS distance
FROM memory_items
WHERE scope_id = $1 AND status = 'active'
ORDER BY embedding <=> $2::VECTOR(1024)
LIMIT 20;
```
Beam: `SET vector_search_beam_size = 64` for remediation recall (connection-local, reset after). Review rule: any new ANN query must call `recall_ann()`; grep CI check enforces no other `<=>` in the repo.

### 6.6 `memory/scoring.py` — pure functions (invariants #9, #10)

```python
def wilson_lb(successes: int, attempts: int, z: float = 1.96) -> float:
    if attempts == 0: return 0.0
    p = successes / attempts
    denom = 1 + z*z/attempts
    centre = p + z*z/(2*attempts)
    margin = z * sqrt((p*(1-p) + z*z/(4*attempts)) / attempts)
    return max(0.0, (centre - margin) / denom)

def recency(age_days: float, tau: float = 14.0) -> float:
    return exp(-age_days / tau)

def entity_affinity(item_entities: set, incident_entities: set) -> float:
    return len(item_entities & incident_entities) / max(1, len(item_entities | incident_entities))

def hybrid(item, incident, age_days, z=1.96) -> float:
    if item.confidence < 0.15 or item.status != "active": return -inf   # hard filter
    sim   = item.similarity                                   # 0.45
    conf  = wilson_lb(item.successes, item.attempts, z) * exp(-age_days/90)  # 0.30
    rec   = recency(age_days)                                 # 0.15
    aff   = entity_affinity(item.entities, incident.entities) # 0.10
    return 0.45*sim + 0.30*conf + 0.15*rec + 0.10*aff
```
Unit tests: 1/1 must not outrank 47/50; hard filters; decay monotonicity.

### 6.7 `memory/audit.py` — belief-state replay (invariant #8)

```sql
-- CockroachDB does NOT support placeholders in AS OF SYSTEM TIME — the timestamp must be a
-- constant literal embedded in the SQL string (docs v26.2; issue #30955: "only constant
-- expressions are allowed"). Safe pattern: validate in Python first (strict ISO-8601 UTC via
-- datetime.fromisoformat; reject anything else), then interpolate the validated literal.
SELECT * FROM decisions        AS OF SYSTEM TIME '<iso_utc_timestamp>' WHERE task_id = $1 ORDER BY created_at;
SELECT * FROM tool_calls       AS OF SYSTEM TIME '<iso_utc_timestamp>' WHERE task_id = $1 ORDER BY started_at;
SELECT * FROM working_memory   AS OF SYSTEM TIME '<iso_utc_timestamp>' WHERE task_id = $1;
```
`audit_replay(task_id, as_of_ts)` — contract: `as_of_ts` must parse via `datetime.fromisoformat` **and** carry a UTC offset; only then is it embedded as a quoted literal. Injection-safe by construction: raw input never reaches the SQL string.

---

## 7. Tool adapters  [BRAINS]

All adapters: typed exceptions (`McpTimeoutError`, `CcloudPermissionError`, `LlmRateLimitError`, `LlmTimeoutError`, `EmbeddingProviderError`, `S3UploadError`, `StaleLeaseError`, `SchemaValidationError`, …), audit row per call (`tool_calls`), latency + status recorded. **The LLM exceptions are provider-neutral on purpose** — they are raised by the `LLMProvider` ABC, so promoting a ladder rung does not change any `except` clause. No Bedrock exception type exists, because no Bedrock client is constructed.

| Adapter | Contract (methods) | Hard limits | Notes |
|---|---|---|---|
| `MCPAdapter` (ro) | `list_clusters, get_cluster, list_databases, list_tables, get_table_schema, select_query, explain_query, show_statement, show_running_queries` | client timeout **15 s** (inside server 20 s); explicit `LIMIT` mandatory; 10 KiB summarizer; no write tools requested | `mcp-cluster-id` header pinned; deny-listed schemas → typed error |
| `CcloudAdapter` (ro) | `cluster_info, cluster_list, audit_list` (**no `backup_list`** — the subcommand does not exist) | enum allowlist → adapter builds argv + `-o json`; error codes → typed | pre-flight gate moved to a new `CloudApiAdapter.backups(cluster_id)` over HTTPS |
| `CloudWatchAdapter` | `get_metric_statistics(metric, dims, window)`, `put_metric_data(...)` | — | target metrics via metric-export (enabled once by human) |
| `SqlProbe` | `explain_analyze(sql) -> plan_json, latency_ms` | read-only role; statement timeout 10 s | target cluster |
| `SqlOperator` | `apply(rendered_sql)` | allowlisted DDL; timeout 60 s; no multi-statement | target cluster |
| `RecipeRenderer` | `render(action_kind, parameters) -> rendered_sql` | allowlist: `create_index`, `analyze_table`; validates table/column against MCP schema; output must be idempotent (`IF NOT EXISTS`); no DROP/TRUNCATE/GRANT | unit-tested; rendered SQL stored for human review |
| `S3Artifact` | `put(key, bytes) -> uri`, `get(uri) -> bytes`, content sha256 | `boto3` client for **S3 and nothing else**; bucket fixed to `engram-agent-artifacts`; IAM `s3:PutObject`/`s3:GetObject` on that bucket ARN only | task logs, execution traces, EXPLAIN bundles, plan diffs (invariant #11). The row stores URI + hash, never the blob |
| `OllamaCloudLLM` (**primary, D13**, behind the `LLMProvider` ABC) | `complete(system, messages, tools, schema) -> LLMResult{text, tool_calls, usage}` — **never relies on a vendor thinking channel**; audit rationale is the tool schema's required `reasoning` field | `Authorization: Bearer $OLLAMA_API_KEY`, base `https://ollama.com`; endpoint shape (OpenAI-compatible `POST /v1/chat/completions` vs native `POST /api/chat`) decided by T9a; temp 0.1; timeout 90 s; retries 3 (backoff + jitter); rate-limit aware; **strips `<mm:think>` tags from content before JSON parsing — load-bearing on this rung, not defensive**; probe multi-turn tool-result handling before the Day-4 schema freeze | `ENGRAM_LLM_MODEL` (`minimax-m3:cloud`) and the endpoint shape are **UNVERIFIED** — `verify_ollama.py` (probes A–F, written 2026-08-03) confirms the tag plus strict-JSON tool calling. Rungs 2/3 (`GroqLLM`, `TogetherLLM` — OpenAI-compatible, identical wire format to each other) are **config, not code**. **No rung is Bedrock** |
| `CohereEmbeddings` (**the only embedder — no ladder**) | `embed(texts: list[str], input_type: str) -> list[vector1024]` — `input_type` is a **required argument**, not config: `search_document` on write, `search_query` on recall | `cohere` SDK or a thin `httpx` client; asserts `len(vec) == 1024` at startup and rejects any other width before it can reach a `VECTOR(1024)` column; batch ≤ 96; retry 3 | `embed-english-v3.0`, **natively exactly 1024-dim — no truncation, padding or projection**. Changing the model changes the vector space and owes a full re-embed (HLD §3 D12) |

---

## 8. Exactly-once protocol (ADR-004) — full detail  [BRAINS + PLUMBER]

### 8.1 Idempotency key
```python
key = sha256(f"{cluster_id}‖{canonical_change}".encode())
# canonical_change = sorted JSON of {action_kind, recipe_version, parameters}
```
Deterministic across restarts (parameters come from the typed Proposal, never from free-form LLM text).

### 8.2 Ledger state machine

```
proposed ──(approval)──► approved ──(ledger txn)──► applied ──(measure)──► outcome: success|failure
    └──(reject/expire)──► skipped
applied ──(reconcile on resume)──► reconciled (re-applied or noop)
```

### 8.3 Sequence (the invariant #6 execution)

```
ACT:
  BEGIN (memory);  INSERT decisions(node='act'); INSERT remediation_actions(status='applied', idempotency_key=K); COMMIT;
  -- the ledger row IS the side-effect record; decision+intent+record = one txn ✅
  rendered = recipe_renderer.render(...)          # idempotent SQL
  sql_operator.apply(rendered)                    # target cluster (external)
  measured_after = sql_probe.explain_analyze(...)
  BEGIN (memory); UPDATE remediation_actions(outcome, measured_after, applied_at)
                   UPDATE procedures(outcome_stats, confidence=wilson(...))
                   INSERT memory_items(class='episode', embedding=…, provenance=…); COMMIT;
```

### 8.4 Crash windows & reconcile (each = exactly one action row)

| # | Crash point | On resume | Handler |
|---|---|---|---|
| W1 | before ledger txn | ledger absent | re-run Act from checkpoint (nothing applied) |
| W2 | between ledger commit and DDL | ledger `applied`, DDL missing | probe target (`SHOW INDEX`); if missing → apply recipe (idempotent) → outcome; status stays `applied`, outcome recorded, metric `exactly_once_conflicts_detected`+1 |
| W3 | between DDL and outcome txn | ledger `applied`, DDL present | probe → present → outcome `success`, no re-apply |
| W4 | after outcome txn | complete | noop; `remediation_actions` UNIQUE(K) makes a second row impossible |

`get_by_idempotency_key(K)` on UNIQUE violation → interpret as "already intended" → reconcile against target reality. Never retry blindly (CLAUDE.md §3.4).

---

## 9. Lifecycle workers (Lambda)  [PLUMBER]

| Worker | Trigger | Logic | Idempotency |
|---|---|---|---|
| `consolidator` | EventBridge 1 h | Embed episode summaries → scoped ANN against `class='procedure'` → if ≥3 tight episodes (sim ≥ 0.9) share outcome → INSERT `procedures(status='draft', sources=[episode ids])`; first induction per scope requires human confirm (approvals channel) → then `active` + `memory_items(class='procedure')` | `UNIQUE(scope_id, name)` + `sources` overlap check |
| `decayer` | EventBridge nightly | `UPDATE procedures SET confidence = wilson(successes,attempts) * exp(-(now()-updated_at)/90d)`; `status='retired'` when < 0.15; `memory_items.status='retired'` for orphaned embeddings | batch by `(procedure_id)` — reruns are naturally idempotent |
| `embedding_backfill` | nightly + on-demand | rows with `embedding IS NULL` → **Cohere `embed-english-v3.0`, `input_type='search_document'`** (via `embedding_cache` first) → UPDATE; also fills `embedding_cache` with `model_id='embed-english-v3.0'` | `WHERE embedding IS NULL LIMIT 500` cursor |
| `approvals` | API GW `POST /approvals/{id}` | `UPDATE approvals SET status=$decided, decided_by, decided_at, channel='dashboard' WHERE approval_id=$1 AND status='pending'` → rowcount 0 = 409 (already decided) | CAS on status |
| `metrics_proxy` | API GW `GET /metrics` | CloudWatch GetMetricData, window/dimension params, 30 s cache | — |
| `alert_ingest` | API GW `POST /webhooks/alerts` | validate signature → compute incident fingerprint (normalized query/metric signature) → INSERT observation + `insert_task` — on UniqueViolation of `tasks_active_incident_idx` (sweep already owns it) attach the observation to the existing task, never spawn a second agent | dedupe = fingerprint + partial unique index (ADR-verified) |

Lambda memory: 256 MB, timeout 60 s (consolidator 300 s), no VPC (or NAT) — their only egress is CockroachDB Cloud (`:26257`), `api.cohere.com:443` and S3. **No Bedrock endpoint is reachable or needed.**

---

## 10. Recipe renderer (safety core)  [PLUMBER]

```python
class ActionKind(str, Enum):
    CREATE_INDEX = "create_index"
    ANALYZE_TABLE = "analyze_table"

TEMPLATES = {
  ActionKind.CREATE_INDEX:
    "CREATE INDEX IF NOT EXISTS {index_name} ON {schema}.{table} ({columns})",
  ActionKind.ANALYZE_TABLE:
    "ANALYZE {schema}.{table}",
}
```
Validation pipeline: (1) kind in allowlist; (2) `schema`/`table`/`columns` cross-checked against MCP `get_table_schema` (no fabricated objects); (3) identifiers quoted + regex `^[a-z_][a-z0-9_]*$`; (4) output contains no `DROP|TRUNCATE|GRANT|ALTER|DELETE|UPDATE|INSERT|SET|;` (multi-statement); (5) must be idempotent (IF NOT EXISTS for CREATE). Rendered SQL stored in `remediation_actions.rendered_sql` for human review at the gate. This is the single most important safety control — it makes injection structurally impossible.

---

## 11. Dashboard & API contract  [ILLUSIONIST + PLUMBER]

### 11.1 SSE feeds (server-side cursor poll, 5 s, `LIMIT 25`, read-only role)

| Feed | View | Cursor | Event schema |
|---|---|---|---|
| `tasks` | `v_recent_tasks` | `created_at > cursor` | `{type:"task", task:{…}}` |
| `actions` | `v_action_feed` | `created_at > cursor` | `{type:"action", action:{…}}` |
| `inspector` | `v_memory_inspector` | `created_at > cursor` | `{type:"recall", item:{…, confidence, provenance}}` |
| `approvals` | approvals (pending) | poll status change | `{type:"approval", …}` |

Vercel: route handler `maxDuration=60`, loop 12× (5 s sleep), then 200-close; client reconnects. RU cost per client ≈ 25 rows/5 s.

### 11.2 API Gateway endpoints (API key, CORS for dashboard origin)

| Method/Path | Auth | Behavior | Idempotency |
|---|---|---|---|
| `POST /approvals/{approval_id}` body `{decision: approve\|reject, by, comment}` | API key | CAS pending→decided; 409 if already decided; 404 unknown | CAS on status |
| `GET /metrics?window=1h` | API key | CloudWatch GetMetricData for §12 metrics | cache 30 s |
| `POST /webhooks/alerts` | HMAC signature | → observations + incident task | sha256 dedupe |
| `GET /api/tasks/{id}/replay?as_of=` | API key | audit replay (§6.7) | — |

### 11.3 Approval UX (demo-critical)
Memory Inspector shows recall bundle (similarity, confidence, citations) for the incident; the action card shows `rendered_sql` + measured_before; Approve button → 11.2 POST; agent's 2 s poll picks it up; SSE pushes the state change back to every viewer. Reject → outcome `skipped` + episode memory.

---

## 12. Observability  [ILLUSIONIST]

| Metric | Namespace/unit | Dimensions | Source | Alarm |
|---|---|---|---|---|
| `recall_hit_rate` | engram/count | scope_id | agent | < 0.5 × 30 min |
| `time_to_remediation` | engram/seconds | scope_id, task_id | agent | > 60 s |
| `memory_recall_latency_p99` | engram/ms | scope_id | agent | > 500 ms |
| `blocked_by_backup_gate` | engram/count | scope_id | agent | any (info) |
| `exactly_once_conflicts_detected` | engram/count | scope_id | agent | any (info) |
| `llm_latency_ms` · `llm_failures` · `llm_token_usage` | engram/ms,count,tokens | model_id | agent | failures > 5/10 min → circuit open |
| `sweep_cycle_ms` | engram/ms | scope_id | agent | > 60 s |
| `queue_depth` | AWS/SQS | queue | SQS | > 10 |
| `task_restarts` | AWS/ECS | service | ECS | any during demo window |

OTel spans: one per graph node; attributes `task_id`, `scope_id`, `node`, `model_id`, `retrieved_count`, `top1_score`, `latency_ms`, `outcome`. Logs: JSON lines, always `task_id`/`decision_id`. Health endpoint: `GET /health` → DB ping, lease round-trip, provider self-test results (ECS load balancer target + demo readiness check).

---

## 13. Incident simulator & demo  [PLUMBER]

`scenarios/slow_query/`: target schema `orders` (2M rows, seeded with fixed RNG seed — deterministic), workload script that produces a seq-scan regression (e.g., stats skew / missing index) with before ≈ 4 s, after ≈ 12 ms. `make scenario-1` runs: provision → seed → verify before (`EXPLAIN ANALYZE` recorded) → emit incident → (agent path) → verify after. Recorded artifacts: before/after plan JSON + timings, uploaded to **S3 `engram-agent-artifacts`** via `boto3`, with the row holding `tool_calls.result_uri` + `content_sha256` (invariant #11).

**Demo script (frozen):**
1. Run incident #1 cold (no memory) → ~45 s, `recall_hit_rate` 0.
2. Show Memory Inspector: no citations.
3. Run incident #2 → ~8 s, procedure recalled (score + confidence + citation to #1 on screen).
4. Mid-incident #3: `aws ecs stop-task` → ECS replaces task → new holder resumes → **exactly one** `remediation_actions` row (query on screen) + `exactly_once_conflicts_detected` visible.
5. Audit replay at death instant via `AS OF SYSTEM TIME`.

---

## 14. Test plan (maps to roadmap gates)  [all]

| ID | Test | Type | Gate |
|---|---|---|---|
| T1 | `scoring` (Wilson, hybrid, filters) | unit | — |
| T2 | `recipe_renderer` (allowlist, injection attempts, idempotency) | unit | — |
| T3 | `adapter` mocks (MCP limits, ccloud errors, **Ollama Cloud tool-schema drift + `<mm:think>` tag-stripping**, **a non-1024-width embedding response is rejected, not written**) | unit | — |
| T4 | `test_fencing.py`: two workers, one task → second fenced out, exactly one action row | integration | **P1 exit gate** |
| T5 | `test_atomicity.py`: crash between txn steps → no torn state | integration | **P1 exit gate** |
| T6 | `test_exactly_once.py`: all 4 crash windows (W1–W4) → exactly one row | integration | **P1 exit gate** |
| T7 | `test_recall.py`: seeded corpus → ranking order, hard filters, scope isolation | integration | P2 |
| T8 | `test_kill_resume_e2e.py`: run → kill -9 → resume → complete | e2e | **P1 exit gate** |
| T9a | `verify_ollama.py` (probes A–F, already written 2026-08-03): model tag resolves, endpoint shape (OpenAI-compat vs native), strict-JSON tool calls with the required `reasoning` field, `<mm:think>` tag reality, multi-turn tool results, latency vs the 5 s budget from a Fargate-like env | phase-0 | **P0-B1** |
| T9b | `verify_cohere.py`: `embed-english-v3.0` returns `len(vec) == 1024` — **asserted** — for both `input_type` values; records latency | phase-0 | **P0-B1** |
| T9c | `verify_s3.py`: put → get → content-hash round-trip on `engram-agent-artifacts`; a wrong-bucket put is denied by IAM | phase-0 | **P0-B1** |
| T10 | `verify_mcp.py` limits, `run_sql.py` vector probe | phase-0 | P0-P1/P0-B2 |
| T11 | CI grep: no `<=>` outside `recall.py`; no raw SQL in prompts; **no `bedrock` string in any source file** | static | every PR |
| T12 | `test_ttl_reclaim.py`: back-date a task past 90 days → the TTL job actually deletes it and cascades to every child (§6.2 note (b)); no silent TTL error in the job log | integration | P2 |

CI: `make test` → build → ECR → CDK deploy; demo tag requires manual approval.

---

## 15. Day-1 rollout checklist (order matters)

1. `SET CLUSTER SETTING feature.vector_index.enabled = true` on `engram-memory` (P0-P1).
2. **RESOLVED 2026-08-10 — the embedding provider was switched PRE-SEED, which is why it was free.** Bedrock `InvokeModel` remains blocked account-wide (`ValidationException: Operation not allowed`, an account-activation problem, not IAM) and is now **de-scoped, not worked around**: embeddings are **Cohere `embed-english-v3.0`, natively 1024-dim**, decided before step 6 seeded anything, so no re-embed is owed. `verify_bedrock.py` is not written and never will be (`docs/blocked-register.md` §1–§2).
3. Provider keys → probes (P0-B1, **still open**): `OLLAMA_API_KEY` → `scripts/verify_ollama.py` (T9a — the model tag and endpoint shape are **UNVERIFIED**; confirm both before the Day-4 tool-schema freeze) · `COHERE_API_KEY` → `scripts/verify_cohere.py` (T9b — assert 1024, don't log it) · AWS keys → `scripts/verify_s3.py` (T9c — put/get/hash on `engram-agent-artifacts`).
4. Create `engram-target-sandbox` + probe/operator roles (P0-P2, ADR-006).
5. `verify_mcp.py` limits (P0-B2) — done; backups via the Cloud REST API (P0-P3) — done. See `docs/phase0-verification.md` §4/§5.
6. Migrations 001–002 → seed corpus → backfill embeddings → migration 003 (vector index) (invariant #1 order).
7. Fencing + atomicity + exactly-once tests green (P1 gate).
8. Dashboard + approvals wired (P1-I, Day 5 freeze).
9. Scenario simulator deterministic (P2-P2, full day budget).
10. Demo rehearsal with `stop-task` (P3), freeze tag.

---

## 16. Error taxonomy & recovery matrix

| Exception | Recovery | Parking |
|---|---|---|
| `LlmRateLimitError` / `LlmTimeoutError` (raised by the ABC — Ollama Cloud on rung 1) | backoff+jitter ×3 → promote the next ladder rung (config change) | after 3 failures: circuit open, park, alert |
| `LLMSchemaError` | 1 repair turn → else park | park (human can retry) |
| `EmbeddingProviderError` (Cohere throttle/timeout) | backoff ×3 → degrade: write the row with `embedding IS NULL`, `embedding_backfill` fills it later. **There is no fallback embedder** — a different model is a different vector space | — |
| `EmbeddingDimensionError` (`len(vec) != 1024`) | **never degrade, never write** — park immediately; a wrong width must not reach a `VECTOR(1024)` column | park + alert |
| `S3UploadError` | retry ×3 → degrade to `result_summary` only, `result_uri` NULL; the task continues | — |
| `McpTimeoutError` | degrade to probe-only observation | — |
| `CcloudPermissionError` | park + alert (misconfiguration) | park |
| `StaleLeaseError` | park (another holder owns the task) | park |
| `UniqueViolation(idempotency_key)` | reconcile (W2/W3) | — |
| `SqlOperatorRejected` | park + alert (renderer bug or privilege) | park |
| `ApprovalExpired` | outcome `skipped`, episode memory | — |
| `BackupGateBlocked` (empty / no recent backup) | park + alert; auto-retry next sweep; human override flag recorded in `decisions` | park until override |
