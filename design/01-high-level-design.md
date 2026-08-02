# Engram — High-Level Design (HLD)

> **Status:** APPROVED-for-build · **Owners:** [BRAINS] agent core · [PLUMBER] data & infra · [ILLUSIONIST] frontend
> **Companions:** `design/02-low-level-design.md` (code-ready), `design/03-adr.md` (decision log), `design/architecture.svg` (diagram)
> **Date:** 2026-08-03 · supersedes the architecture sketches in `research/cockroachdb_aws_hackathon_strategy.md` §15/§12 where they differ (all deltas are called out in §3).
> **Read this before touching `CLAUDE.md` invariants. Nothing here violates an invariant; a few things make them enforceable.**

---

## 1. Context & goals

**Product:** Engram — an autonomous CockroachDB reliability engineer whose entire competence is its memory. It watches CockroachDB clusters, diagnoses regressions, remediates behind a human approval gate, measures whether the fix worked, and writes the outcome back as a scored, reusable procedure.

**The sentence everything serves:**
> *Engram is the agent whose memory you can kill mid-task — it comes back, finishes the job without redoing it, and solves the next incident in seconds because it remembered the last one.*

**Two demo beats (non-negotiable):**

| Beat | What happens | What judges see |
|---|---|---|
| 1. It remembers | Incident #2 resolved in ~8 s vs #1's ~45 s | Recalled procedure + similarity score + confidence + citation, on screen |
| 2. It survives | `aws ecs stop-task` mid-remediation | New task reclaims lease, resumes from checkpoint, produces **exactly one** action row |

**Hard constraints (from `CLAUDE.md` §4 + Devpost):**

| Constraint | Value | Architectural consequence |
|---|---|---|
| Deadline | 2026-08-18 17:00 ET (submit 12:00 ET) | Every phase gate must be falsifiable; no rework loops (this is why schema + contracts freeze) |
| CockroachDB tools | ≥2 of 4 (we use **all 4**) | MCP, vector index, ccloud CLI, Agent Skills — each load-bearing |
| AWS services | ≥1 of list (we use **8**) | ECS Fargate, Lambda, EventBridge, SQS, API Gateway, S3, Secrets Manager, CloudWatch + IAM |
| Repo | public, Apache-2.0 in first commit | **NOT DONE (2026-08-03).** A first commit exists (`4304008`) with **no `LICENSE` file**. Amend the root commit before adding a remote. |
| Free tier | 50M RU/mo · 10 GiB | no changefeeds, no per-panel polling, SSE + cursor |
| Model access | **ALL Bedrock invoke is blocked account-wide**, not just Anthropic FTU | reasoning = Ollama Cloud `minimax-m3:cloud` routes around it. **Embeddings do NOT** — `amazon.titan-embed-text-v1/v2` and even `claude-3-haiku` (ON_DEMAND, no form) all return `ValidationException: Operation not allowed`. IAM is fine. See CLAUDE.md §8 #1/#2. |

---

## 2. Non-negotiable invariants (from CLAUDE.md §3 — restated, each with "where enforced")

| # | Invariant | Enforced in |
|---|---|---|
| 1 | `feature.vector_index.enabled = true` before any vector DDL; **seed rows before creating the vector index** | Migration order (LLD §6.2), runbook §15 |
| 2 | Embeddings `VECTOR(1024)`; `VECTOR INDEX (scope_id, embedding vector_cosine_ops) WITH (min_partition_size=16, max_partition_size=128)` | DDL `001`; Titan adapter asserts 1024 dims at runtime |
| 3 | Every ANN query equality-constrains `scope_id` and uses `ORDER BY embedding <=> $1 LIMIT k` | `recall.py` (single choke point; no other code path may run ANN) |
| 4 | `remediation_actions.idempotency_key UNIQUE` — **the** exactly-once guarantee | DDL `001`; act protocol (LLD §8) |
| 5 | Leases via `SELECT … FOR UPDATE` + monotonic `fence_token`; stale holders rejected at write time | `leases.py` + every write path uses CAS `WHERE fence_token = $expected` |
| 6 | Decision + intent-to-act + side-effect record commit in **one transaction** | `act_measure.py` ledger-first protocol (LLD §8) |
| 7 | TTL declared in DDL: `working_memory` 7d, `observations` 30d, checkpoint tables on | DDL `001` |
| 8 | `AS OF SYSTEM TIME` reserved for belief-state replay (audit) only | `audit.py`; no other use allowed by review checklist |
| 9 | Hybrid retrieval `0.45·sim + 0.30·confidence + 0.15·recency + 0.10·entity_affinity`; hard-filter `confidence < 0.15` and `status <> 'active'` | `recall.py` scoring module (unit-tested) |
| 10 | Confidence = time-decayed Wilson lower bound | `scoring.py` + nightly decayer |
| 11 | Large artifacts (EXPLAIN bundles, plan diffs) → S3; row holds URI + content hash | `tool_calls.result_uri`; S3 adapter |

---

## 3. Design decisions — delta vs. the original plan

The repo's strategy stands. These are the deltas (each has an ADR in `03-adr.md`):

| # | Decision | Why | ADR |
|---|---|---|---|
| D1 | **Reasoning model = Ollama Cloud `minimax-m3:cloud`** (not Bedrock Claude) | Anthropic FTU approval blocked; M3 is a top-tier agentic model (SWE-Bench Pro 59.0, MCP Atlas 74.2 — leads open-source, beats Opus 4.6); US-hosted, zero data retention; $20/mo Pro plan is cheaper than any Bedrock fallback | ADR-001 |
| D2 | **Embeddings stay on Bedrock Titan V2 @1024** (not moved to Ollama) | `VECTOR(1024)` is a schema invariant; Titan needs no FTU (Amazon's own model, one-click enable); moving embedding models forces a full re-embed and risks a dimension change | ADR-002 |

> ⚠️ **D2's premise was falsified on 2026-08-03.** "Titan needs no FTU, one-click enable" is empirically wrong: Titan invoke fails identically to Claude because the block is **account-level**, not per-model. The *sound* half of D2 is now the urgent half — switching embedders forces a full re-embed, so **the provider must be fixed before the corpus is seeded.** Invariant #2 pins the dimension, never the vector space. ADR-002 needs revision.
| D3 | **EventBridge → SQS → long-lived Fargate task** (not "EventBridge directly") | Durable trigger, at-least-once with dedupe, backpressure; the task stays long-lived so `stop-task` is the demo verb; queue depth is a CloudWatch metric | ADR-003 |
| D4 | **Ledger-first exactly-once across two clusters** | The one-transaction invariant (#6) can only cover the *memory* cluster; the DDL lives on the *target*. Protocol: commit ledger row first (status `applied`), then idempotent DDL, then reconcile on resume | ADR-004 |
| D5 | **API Gateway + Lambda for approvals & metrics proxy** | Vercel serverless must not hold DB/IAM credentials; approvals and CloudWatch reads go through a thin, key-authenticated API | ADR-005 |
| D6 | **Two low-privilege SQL roles on the TARGET cluster** (`engram_probe` read/EXPLAIN, `engram_operator` allowlisted DDL) | MCP is control-plane introspection only; `EXPLAIN ANALYZE` needs execution stats MCP doesn't return; DDL must be possible after approval without MCP write tools | ADR-006 |
| D7 | **No changefeeds; SSE + cursor** (unchanged, now with exact query surface) | RU discipline; frozen in LLD §11 | ADR-007 |
| D8 | **Single agent task; concurrency via leases, not multi-agent** (unchanged, now with a fence-test harness) | Demo + judging story; two-process fencing is a CI test, not a product feature | ADR-008 |
| D9 | **Fingerprint embedding cache** (`embedding_cache`: sha256(content) → vector) | Never embed the same query shape twice; cuts Bedrock cost and RU | — |
| D10 | **CDK (Python) IaC + GitHub Actions CI with a demo-freeze tag** | Single language across the repo; repeatable deploys; the freeze tag guarantees the demo environment is never rebuilt over | — |

---

## 4. System context (C4 Level 1)

```
                        ┌──────────────────────────────┐
                        │  HUMAN OPERATOR / JUDGES      │
                        │  (approves, watches demo)     │
                        └──────────────┬───────────────┘
                                       │ approve/reject · watch
                                       ▼
   ┌──────────────────────┐   ┌────────────────────────────┐
   │ DASHBOARD (Vercel)   │◄──┤ API Gateway + Lambda        │
   │ Next.js + shadcn/ui  │   │ /approvals · /metrics ·     │
   │ SSE + cursor (ro)    │   │ /webhooks/alerts            │
   └───────┬──────────────┘   └──────────────┬─────────────┘
           │ SSE read-only SQL               │ HTTP
           ▼                                 ▼
┌──────────────────────────────┐   ┌────────────────────────────────────┐
│ MEMORY CockroachDB cluster   │   │ ENGRAM AGENT — ECS Fargate (1 task)│
│ = the product                │◄──┤ LangGraph 5-node loop              │
│ psycopg3 (rw) · engram_reader│   │ Observe→Recall→Reason→Gate→Act&Measure
└──────────────────────────────┘   └──┬──────────┬──────────┬───────────┘
                                      │          │          │
                    ┌─────────────────┘          │          └──────────────────┐
                    ▼                            ▼                             ▼
        ┌──────────────────────┐   ┌──────────────────────┐   ┌─────────────────────────┐
        │ Ollama Cloud         │   │ AWS Bedrock          │   │ TARGET CockroachDB      │
        │ minimax-m3:cloud     │   │ Titan V2 @1024 dims  │   │ cluster (demo victim)   │
        │ reasoning (LLM)      │   │ embeddings (only)    │   │ via MCP (ro) + probe/   │
        └──────────────────────┘   └──────────────────────┘   │ operator SQL roles      │
                                                              └─────────────────────────┘
        AWS control plane (EventBridge · SQS · Lambda workers · S3 · Secrets Manager · CloudWatch · IAM)
```

**External systems:** Ollama Cloud (`https://ollama.com`, Bearer `OLLAMA_API_KEY`) · AWS Bedrock (Titan only) · CockroachDB Cloud Managed MCP (`https://cockroachlabs.cloud/mcp`, service-account key, `mcp-cluster-id` header) · ccloud CLI (read-only Cluster Operator SA) · CloudWatch (target metrics exported once by human via `ccloud cluster metric-export cloudwatch enable`).

---

## 5. Component architecture (C4 Level 2)

### 5.1 Agent runtime — ECS Fargate (one long-lived task, `desiredCount=1`)

- **LangGraph graph** with 5 nodes and a typed state object; `AsyncCockroachDBSaver` checkpoints every node transition into the **memory cluster** (`thread_id = task_id`).
- **Node internals:**
  - `observe` — collects signals (MCP introspection, probe queries, CloudWatch trends, ccloud backup status), fingerprints anomalies, writes `observations`/`entities`.
  - `recall` — scoped ANN + hybrid re-ranking → ranked, cited context bundle.
  - `reason` — LLM (M3) hypothesis → **falsification against CockroachDB's built-in index recommendation** (schema-validate the proposal; run `EXPLAIN` on the *original* slow query and align the proposal with the optimizer's "index recommendations" section — CockroachDB has no hypopg-style hypothetical indexes, so the recommendation IS the pre-gate evidence; final proof is before/after `EXPLAIN ANALYZE` after apply) → typed `Proposal`.
  - `gate` — persists decision + intent + approval request in one txn; waits for human decision (polls `approvals`); on timeout → park.
  - `act_measure` — ledger-first apply (D4), measure before/after, write outcome + procedure stats + episode memory in one txn.
- **Tool adapters** (all behind interfaces; see LLD §7): MCP (ro), ccloud (ro, enum-only), CloudWatch (get/put), SQL probe + operator (target), recipe renderer (allowlist), Ollama Cloud LLM, Bedrock Titan embeddings, S3 artifacts.
- **Lease manager** — per-task `FOR UPDATE` lease + fence token; heartbeat every 15 s; expiry 60 s.
- **Signal handling** — SIGTERM (ECS `stop-task` grace): checkpoint + release lease within 25 s; SIGKILL path covered by lease expiry + checkpoint tables.

### 5.2 Memory plane — `engram-memory` CockroachDB cluster (the product)

11 tables grouped by memory type (full DDL in LLD §6):

| Memory type | Tables |
|---|---|
| Working | `working_memory` (7d TTL) · LangGraph checkpoint tables (30d TTL) |
| Transactional | `tasks` · `agent_leases` · `remediation_actions` (idempotency ledger) |
| Operational | `observations` (30d TTL) |
| Semantic/Episodic/Procedural | `memory_items` (vectors) · `procedures` (stats + confidence) |
| Entity | `entities` |
| Audit | `decisions` · `tool_calls` |

Access roles: `engram_agent` (rw) · `engram_reader` (ro, dashboard SSE) — separate SQL users, never shared.

### 5.3 Target plane — `engram-target-sandbox` cluster (the subject)

- Read/introspect: **Managed MCP** (ro) — schema, running queries, `explain_query`; **`engram_probe`** SQL role — `SELECT`, `EXPLAIN ANALYZE` (execution stats MCP doesn't return).
- Write: **`engram_operator`** SQL role — allowlisted DDL only (`CREATE INDEX`, `ANALYZE`) on the app schema; **no** DROP/TRUNCATE/GRANT. The recipe renderer validates every statement; the model never emits SQL.
- Control plane: ccloud CLI (ro) — `cluster info/list`, `audit list`. **The pre-flight gate is NOT ccloud:** `ccloud cluster backup list` does not exist in ccloud 0.6.12. Use `GET https://cockroachlabs.cloud/api/v1/clusters/{id}/backups` (verified 200 on Basic; needs a **Cluster Admin**-scoped SA key for the *target* cluster — ours currently 403s there).

### 5.4 Lifecycle workers — Lambda (deliberately separate from the agent)

| Worker | Trigger | Job |
|---|---|---|
| `consolidator` | EventBridge 1 h | Episode → procedure induction (≥3 tight episodes + shared outcome → candidate; human confirm first time) |
| `decayer` | EventBridge nightly | Recompute Wilson confidence with time decay; retire `confidence < 0.15` |
| `embedding-backfill` | on-demand / nightly | Embed rows with `embedding IS NULL`; populate fingerprint cache |
| `alert-ingest` | API GW webhook | External alerts → `observations` rows (incident trigger path) |
| `metrics-proxy` + `approvals` | API GW | Dashboard-facing CloudWatch reads; approval decisions → `approvals` table |

### 5.5 Orchestration

EventBridge schedules: `sweep-rule` (5 min) → SQS `engram-commands` → agent task consumes (one incident at a time, FIFO); `consolidate-rule` (1 h), `decay-rule` (nightly) → Lambda directly. **Dedupe (webhook vs sweep race):** the *incident fingerprint* (sha256 of normalized query/metric signature) is computed at ingest, before any task is created; SQS FIFO message group = fingerprint; and `tasks` carries a **partial UNIQUE index on `(target_cluster_id, incident_fingerprint) WHERE task_type='incident' AND status IN ('pending','running','awaiting_approval','blocked')`** — the second arrival hits the constraint, is rejected, and its observation is attached to the existing task. One active incident per (cluster, fingerprint); completed/failed rows release the slot for future occurrences.

### 5.6 Dashboard — Vercel (Next.js App Router + Tailwind + shadcn/ui)

- SSE feeds (cursor-based, server-side poll every 5 s): task feed, action feed, memory inspector (recall scores + citations), approval queue.
- Mutations (`approve`/`reject`) → API Gateway → Lambda → memory cluster.
- Metrics → API Gateway `/metrics` → CloudWatch read.
- No DB credentials in the frontend; only the `engram_reader` DSN is present in the *serverless function* (read-only).

---

## 6. Technology stack (locked)

| Layer | Choice | Why |
|---|---|---|
| Agent core | Python 3.12 · LangGraph · `langchain-cockroachdb` (`AsyncCockroachDBSaver`) | checkpoint-in-CockroachDB is the product; pinned in CLAUDE.md |
| DB access | psycopg3 async pool (hot path) | CLAUDE.md §2 |
| LLM client | direct httpx client to Ollama Cloud `/api/chat` (no SDK) | full control of tool JSON schema, thinking-mode, timeouts; tiny surface |
| Embeddings | boto3 → Bedrock `amazon.titan-embed-text-v2:0` (1024, normalize) | invariant #2; no FTU |
| MCP | `mcp` Python client, pinned `mcp-cluster-id` | read-only introspection |
| Frontend | Next.js App Router · Tailwind · shadcn/ui · Vercel | CLAUDE.md §2 |
| IaC | AWS CDK (Python) | one language across repo |
| CI | GitHub Actions (lint → test → build → ECR → CDK deploy) | repeatable; demo-freeze tag |
| Observability | OTel spans (agent) + CloudWatch (metrics/logs/alarms) | judging criterion "Product Readiness" |

---

## 7. AWS architecture

### 7.1 Network
- VPC, 2 AZs, private subnets only; NAT gateway for egress; **no public ingress** to compute.
- Egress allowlist (security-group / NAT route): `ollama.com:443`, `bedrock-runtime.<region>.amazonaws.com:443`, `cockroachlabs.cloud:443`, `api.crdb.io:443` (ccloud), CloudWatch/S3/Secrets via endpoints.

### 7.2 Compute & services

```
EventBridge (3 rules) ──► SQS engram-commands ──► ECS Fargate: engram-agent (1 task, auto-restart)
        │                                         │
        ├─► Lambda consolidator ◄──┐              ├─► S3 engram-artifacts (EXPLAIN bundles, plan diffs)
        ├─► Lambda decayer         │              ├─► Secrets Manager (DSNs, keys)
        └─► Lambda embedding-backfill             ├─► CloudWatch (logs, custom metrics, alarms)
API GW ◄── dashboard ──► Lambda metrics-proxy     └─► Bedrock (Titan) · Ollama Cloud · MCP · ccloud
API GW ◄── dashboard ──► Lambda approvals
API GW ◄── external   ──► Lambda alert-ingest
```

### 7.3 IAM identities (4)

| Identity | Used by | Permissions |
|---|---|---|
| `engram-agent-task-role` | ECS task | `bedrock:InvokeModel` (Titan only, resource-pinned), S3 rw on artifacts bucket, SecretsManager GetSecretValue (pinned), CloudWatch PutMetricData + logs, SQS Receive/Delete, `sts` none |
| `engram-worker-role` | Lambda workers | same, minus SQS. No `ecs:RunTask` — workers never start tasks. |
| `engram-api-role` | API GW Lambdas | SecretsManager (reader DSN), CloudWatch GetMetricData (metrics-proxy only), write to `approvals` via memory DSN |
| `engram-ci-role` | GitHub Actions (OIDC) | ECR push, CDK deploy, ECS update-service (for demo-freeze tag) |

No identity holds both memory-cluster write and target-cluster operator credentials except the agent task — and even there they are separate secrets, fetched independently, so only the credential the current graph node needs is ever resolved (see LLD §13).

### 7.4 Secrets (Secrets Manager, versioned, `secret/engram/*`)

`memory-dsn` (engram_agent) · `memory-reader-dsn` (dashboard Lambda) · `target-dsn-probe` · `target-dsn-operator` · `ollama-api-key` · `mcp-token` · `mcp-cluster-id` · `ccloud-token` · `metric-export` config.

---

## 8. CockroachDB architecture

### 8.1 Two clusters, two roles (never conflated)

| Cluster | Purpose | Tiers | TTL |
|---|---|---|---|
| `engram-memory` | the product — all 8 memory types + checkpoints | Basic (free) | working 7d · observations 30d · checkpoints 30d · audit 90d · procedures/entities indefinite |
| `engram-target-sandbox` | the demo victim — schema + data the agent operates on | Basic (free) | none |

### 8.2 Memory cluster design rules
- Vector index created **after** the seed corpus (invariant #1); C-SPANN `(scope_id, embedding vector_cosine_ops)`, partitions 16/128 (invariant #2).
- All ANN via one choke-point module (invariant #3); beam size 64 for remediation recall, default for sweeps.
- `idempotency_key = sha256(cluster_id ‖ canonical_rendered_change)` (invariant #4).
- Hybrid scoring + Wilson confidence (invariants #9, #10) — pure functions, unit-tested.
- `AS OF SYSTEM TIME` only in `audit.py` (invariant #8).

### 8.3 RU budget (rough, headroom 100×)
**Corrected 2026-08-03 with a measured figure** — one scoped ANN query on a 400-row corpus cost **5.576 RU** (6 ms). Sweeps: 288/day × ~25 queries = **7,200 queries/day**, not 7.2k RU; at ~2–6 RU each that is ~15k–43k RU/day ≈ **0.45M–1.3M RU/mo**. Dashboard SSE: cursor `LIMIT 25` every 5 s per client ≈ 25k/mo. Embeddings stored once. Total still ≪ 50M RU/mo — so the conclusion holds, but with ~40× headroom, not 100×. Re-measure once the corpus is realistically sized: RU per ANN query grows with partition count. **The only real spend: Ollama Cloud Pro $20/mo.**

---

## 9. Model & AI strategy

### 9.1 Reasoning — Ollama Cloud `minimax-m3:cloud`
- **Why:** Anthropic FTU approval on Bedrock is blocked; M3 is a 428B MoE (~23B active) with 1M context, built for agents — autonomous task decomposition, tool invocation, multi-step reasoning; 74.2% MCP Atlas, 59.0% SWE-Bench Pro; US-hosted with zero data retention; `minimax-m3:cloud` is the model id.
- **Contract:** direct `POST https://ollama.com/api/chat`, `Authorization: Bearer`, `stream: false`, tools as JSON schema, temperature 0.1, timeout 90 s, retries 3 with exponential backoff + jitter.
- **Thinking mode — do NOT depend on it (verified bug).** `minimax-m3:cloud` never returns a `message.thinking` field despite `think: true` (ollama issue #16632), and in some modes the `<mm:think>…</mm:think>` tags leak **into the content field** (vLLM #45687). Consequences, baked into the design: (1) the tool-call JSON schema requires an explicit `reasoning: string` property, so audit-grade rationale lives inside the JSON the validator can see; (2) the adapter defensively strips `<mm:think>` tags from content before JSON parsing (they must never reach the schema validator); (3) `verify_ollama.py` probes thinking behavior + multi-turn tool-result handling on Day 1 (see also ollama #16389: M3 stalling on tool-result messages).
- **Rate limits:** Free tier is demo-only. **Budget `Ollama Pro` ($20/mo)** from Day 1 — 3 concurrent runs, ~50× usage. Circuit breaker parks the agent after N consecutive failures.
- **Fallback ladder:** Ollama Cloud → local Ollama (dev only) → Bedrock Claude (if FTU lands before demo). The `LLMProvider` interface makes this a config change, not a code change.

### 9.2 Embeddings — Bedrock Titan Text Embeddings V2 @1024 (unchanged)
- ⚠️ **BLOCKED as of 2026-08-03.** Being Amazon's own model does not help: invoke fails with `ValidationException: Operation not allowed` at the account level. There is **no one-click fix** — it needs account activation. Candidate 1024-dim substitutes if it has not cleared by Day 5: `mxbai-embed-large`, `bge-large-en-v1.5`, `qwen3-embedding:0.6b` (probed by `scripts/verify_ollama.py` PROBE F).
- `{"inputText": …, "dimensions": 1024, "normalize": true}`; adapter asserts `len(vec) == 1024` and unit-norm (L2 ≈ 1.0) at startup (self-test, same as `verify_bedrock.py`).
- **Embed at write time only**; query embedding per incident; **fingerprint cache** (D9) dedupes repeat shapes.

### 9.3 Latency budgets (demo beat #1: incident #2 in ~8 s)

| Segment | p95 budget |
|---|---|
| Recall (ANN + re-rank) | < 300 ms |
| M3 first token / total | < 5 s |
| Falsification loop (≤ 3 MCP `explain_query`) | < 1.5 s |
| Ledger + measure | < 1 s |
| **Total** | **< 8 s** |

---

## 10. Core flows

### 10.1 Incident lifecycle (steady state)

```
EventBridge sweep-rule (5 min)
   │ SQS message {task_type: sweep}
   ▼
observe   → MCP introspection · probe EXPLAIN ANALYZE · CloudWatch trends · ccloud backup list
   │        anomaly detected → create tasks.incident + observations + entities (1 txn)
   ▼
recall    → fingerprint → embed (Titan) → scoped ANN → hybrid re-rank → context bundle
   │        (record scores + citations in decisions + telemetry)
   ▼
reason    → M3: hypothesis → falsify via MCP explain_query (≤3 rounds) → typed Proposal
   │        (decision row + proposal persisted; tool_calls rows per call)
   ▼
gate      → ONE txn: decisions(intent) + remediation_actions(proposed, idempotency_key)
   │        + approvals(pending)  →  dashboard SSE "needs approval"
   │        ── human approves via dashboard ──► approvals.status = approved (Lambda)
   ▼
act_measure → re-verify backup gate (ccloud) → ledger-first apply (D4) → measure before/after
              → ONE txn: outcome + procedure stats + episode memory + audit rows
   │
   └─► consolidator (1 h) may induct a procedure from ≥3 tight episodes (human confirm #1)
```

### 10.2 Kill-and-resume (demo beat #2)

```
1. Agent mid-Act: ledger row committed (status=applied), DDL not yet applied.
2. aws ecs stop-task  →  SIGTERM: checkpoint + release lease (25 s grace)  [or SIGKILL]
3. ECS replaces task (service desiredCount=1) → new holder:
   a. load checkpoint (thread_id=task_id) from memory cluster
   b. acquire lease: SELECT … FOR UPDATE; fence_token 1→2
   c. reconcile (D4): ledger says applied → probe target for index
        missing  → apply recipe (idempotent, IF NOT EXISTS) → outcome txn
        present  → mark noop, outcome txn
   d. exactly one remediation_actions row (UNIQUE idempotency_key made it impossible to double)
4. AS OF SYSTEM TIME replay shows what the agent believed at death (audit).
```

### 10.3 Memory consolidation & decay
- Consolidator: embed episode summaries → cluster (scoped ANN against procedure class) → ≥3 episodes, shared outcome → candidate procedure (draft) → human confirms → active.
- Decayer (nightly): `confidence = wilson(successes, attempts) · exp(−age/τ)`, τ=90 d; `status → retired` when < 0.15; soft-deletes by status (TTL is the hard delete).

---

## 11. Security

- **Blast radius (stated plainly, from strategy §15):** full compromise of the model yields: create an index on an allowlisted schema, after a human clicked approve, on a cluster with a verified recent backup. Cannot DROP, TRUNCATE, exfiltrate via control plane, or escalate.
- **Structural, not procedural:** model never emits SQL/shell (enum + typed params); MCP is `mcp:read` only; ccloud SA is Cluster Operator (ro) only; target DDL role allowlisted; IAM resource-pinned; secrets never in the frontend.
- **Prompt-injection defense:** tool outputs are marked untrusted; JSON schemas validated with pydantic; provenance recorded (`model_id`, tool call ids, citations) so a wrong action is attributable.
- **Network:** private subnets, no public ingress, egress allowlist, TLS everywhere.
- **Audit:** every decision/tool-call row → replayable via `AS OF SYSTEM TIME`.

---

## 12. Observability

| Metric | Source | Alarm |
|---|---|---|
| `recall_hit_rate` | agent (put) | < 0.5 for 30 min → warn |
| `time_to_remediation` | agent (put) | > 60 s (demo target 8 s) |
| `memory_recall_latency_p99` | agent (put) | > 500 ms |
| `blocked_by_backup_gate` | agent (put) | any → info (demo beat) |
| `exactly_once_conflicts_detected` | agent (put) | any → info (demo beat) |
| `llm_latency_ms` / `llm_failures` / `llm_token_usage` | agent (put) | failures > 5 in 10 min → circuit open |
| `queue_depth` | SQS (built-in) | > 10 → page |
| `task_restarts` | ECS (built-in) | any during demo → warn |

Logs: structured JSON, one event per node transition, always carrying `task_id`, `decision_id`, `model_id`. Traces: OTel span per node with attributes `scope_id`, `retrieved_count`, `score_top1`, `latency_ms`.

---

## 13. Environments, deployment, CI

| Env | Where | DB | Models |
|---|---|---|---|
| dev | laptop / docker-compose | free clusters (shared) | Ollama Cloud + Titan (real) |
| staging | same CDK stack, suffix `-stg` | free clusters | same |
| **demo** | **frozen tag `demo-freeze`** | `engram-memory` + `engram-target-sandbox` | same |

CI: `test` (unit + integration + fencing) → `build` (Docker → ECR) → `deploy` (CDK) — manual approval on the demo tag. The demo environment is **never** redeployed over during the judging window.

---

## 14. Risks & mitigations (updated for the model swap)

| Risk | L | Mitigation |
|---|---|---|
| Ollama Cloud outage / rate limit during demo | M | Circuit breaker + retries; fallback ladder (§9.1); Pro plan; freeze tag |
| M3 thinking-mode token bleed / schema drift | M | Strip thinking before validation; strict JSON probes in `verify_ollama.py` before Day-4 freeze |
| M3 latency breaks the 8 s beat | M | Latency probe from Fargate-like env on Day 1 (P0-B1 replacement); if > budget, tune `num_predict`/prompt or move `reason` off the critical path |
| ~~Titan access not enabled~~ **Bedrock invoke blocked account-wide** | **H — REALISED** | Not a one-click fix. Reasoning already routed to Ollama; **embeddings have no provider, so Phase 2 is blocked.** Decision deadline Day 5: commit to a 1024-dim substitute *before seeding*, since the switch is one-way. |
| Bedrock quota/throttle on embeddings | L | Fingerprint cache + exponential backoff; batch writes |
| MCP limits (10 KiB/20 s/LIMIT 25) | M | Adapter enforces 15 s client timeout + summarizer (already designed) |
| RU overrun | L | Budgeted 100× headroom; SSE cursor; no per-panel polling |
| Vercel function timeout vs SSE | M | Server-side cursor poll 5 s inside 60 s maxDuration; 12 pushes per connection |
| Demo nondeterminism (LLM variance) | M | Incident simulator is deterministic; M3 temperature 0.1; recipes fixed; measured before/after from DB not LLM |
| Schema churn post-freeze | M | Day-3 freeze + changelog gate (CLAUDE.md process) |

---

## 15. Judging-criteria traceability

| Criterion | What we demonstrate | Where |
|---|---|---|
| Agentic Memory Design | 8 memory types in one cluster; hybrid retrieval; confidence decay; induction; forgetting in DDL; belief replay | §5.2, §10, LLD §6 |
| Technological Implementation | 4 CockroachDB tools + 8 AWS services; leases/fencing; exactly-once across two clusters | §7–§10 |
| Real-World Impact | Ops toil → remembered procedures; backup gate; approval safety | §10 |
| Product Readiness | IaC, CI, secrets, observability, least privilege, blast radius | §7, §11–§13 |
| Creativity & Originality | "kill the agent, not the database"; one-transaction decision+action; memory as the product | §1, §10 |

---

## 16. What we deliberately did NOT do

- No multi-agent (one graph, five nodes) — concurrency via leases.
- No AgentCore Runtime (schedule risk; `stop-task` is the demo).
- No changefeeds (RU), no per-panel polling, no `cluster disruption` (Advanced-only).
- No SageMaker/training, no custom fine-tunes.
- No embedding model switch (vector-space identity is a schema invariant).
