# Engram

**An autonomous database reliability engineer for CockroachDB — whose entire competence is its
memory.** Engram watches production CockroachDB clusters, diagnoses regressions, remediates
behind human approval, measures the fix, and writes the outcome back as a scored, reusable
procedure. It is built for the on-call engineer who should not be the last line of defense
against a bad query plan.

**Live demo:** [dashboard-five-chi-90.vercel.app](https://dashboard-five-chi-90.vercel.app) —
no login, no credentials required.

**License:** Apache-2.0 (see [`LICENSE`](./LICENSE)).

**Demo video:** script finalized and the live footage recorded (a two-narrator voiceover over a
single continuous take, 2:59 runtime); public link to be added here once the edited video is
exported.

---

## The two things this project exists to prove

1. **It remembers.** A second incident against a familiar query shape resolves faster than the
   first, with the recalled procedure, similarity score, and confidence visible on screen —
   not narrated, not claimed. Real measured recall similarity across separate incidents:
   **62%, 50%, 51%, 61.6%**.
2. **It survives.** Killing the agent mid-remediation (`aws ecs stop-task`, no warning) does not
   duplicate or lose the fix. A replacement ECS task reclaims the lease, resumes from a real
   LangGraph checkpoint, and the ledger shows **exactly one** `remediation_actions` row for the
   whole episode — not two.

---

## Quickstart

**To see it work:** open the [live dashboard](https://dashboard-five-chi-90.vercel.app). It's a
single-screen, read-only, server-sent-events view over the real memory cluster — a Task Feed,
Action Feed, Memory Inspector (similarity + confidence + provenance), Approval Queue, and a
Metrics panel, all streaming live via a dedicated read-only SQL role. No credentials are required
and none are ever sent to the browser.

**To run the agent yourself** (development, not required to see the demo):

```bash
git clone https://github.com/Sandipan-87/ENGRAM.git
cd ENGRAM
pip install -r requirements.txt -r workers/requirements.txt
cp .env.example .env   # fill in real DSNs/keys — see below
python -m pytest tests/    # 226 unit tests, no live credentials needed
python -m agent.main       # long-polls ENGRAM_QUEUE_URL and runs the 5-node loop
```

`python -m agent.main` (not `python agent/main.py`) — the agent uses absolute `agent.*` imports,
so it must run as a module with the repo root on `sys.path`.

Required `.env` values (see `.env.example` for the full list): two CockroachDB Cloud DSNs
(memory + target clusters — Engram never conflates them), a Cohere API key (embeddings), an
Ollama Cloud API key (reasoning), an AWS credential pair with S3/Secrets Manager/CloudWatch
access, and a `CCLOUD_TOKEN` (Cluster-Admin-scoped to the target cluster, for the backup gate).
None of the above are Bedrock — see the AWS services table below.

The dashboard itself lives in `dashboard/` (Next.js; its own `README.md` has setup steps), the
Lambda workers live in `workers/`, and the CDK infrastructure lives in `infra/`.

---

## Architecture

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                    AWS control plane                     │
                    │  EventBridge (5-min sweep · lifecycle rules, disabled)   │
                    │           │                                              │
                    │           ▼                                              │
                    │   SQS engram-commands.fifo (durable, dedup by fingerprint)│
                    │           │                                              │
                    │           ▼                                              │
                    │   ECS Fargate — the agent (long-lived, kill-and-resume)  │
                    │  ┌───────────────────────────────────────────────────┐  │
                    │  │  Observe → Recall → Reason → Gate → Act & Measure  │  │
                    │  │  (LangGraph, 5 nodes, checkpointed every step)     │  │
                    │  └───────────────────────────────────────────────────┘  │
                    │      │             │             │             │        │
                    │  SQL probe     Cohere embed  Ollama Cloud  CloudWatch   │
                    │  (target, ro)  (1024-dim)    (reasoning)   S3 · Secrets │
                    └──────┼─────────────────────────────────────────Manager─┘
                           │
                           ▼
     ┌──────────────────────┐  ┌───────────────────────┐
     │  MEMORY cluster      │  │  TARGET cluster       │
     │  (the product)       │  │  (the subject)        │
     │  8 memory types,     │  │  schema/data the agent│
     │  VECTOR + C-SPANN    │  │  operates on via:     │
     │  recall, checkpoints,│  │  · probe (read-only)  │
     │  leases, Row-Level   │  │  · operator (allow-   │
     │  TTL                 │  │    listed DDL only)   │
     └───────────────┬──────┘  │  · backup-gate REST   │
                     │         │    API                │
                     │         └───────────────────────┘
                     │  (read-only SSE, cursor-based, no changefeeds)
                     ▼
     Next.js dashboard (Vercel) ── API Gateway + Lambda (approvals/metrics)
```

**Not multi-agent, on purpose.** One long-lived agent; concurrency is leases + fence tokens, not
agent messaging. Lifecycle maintenance (consolidation, decay, embedding backfill) runs on
separate Lambdas specifically so memory upkeep survives agent death.

---

## Why this architecture

- **Memory is the product, not the LLM.** The entire graded artifact is the CockroachDB memory
  layer: remove it and Engram has no procedures, no checkpoints, no safety ledger — it stops
  working correctly. The LLM is a swappable component behind typed JSON schemas; every decision,
  tool call, citation, and outcome is a database row with provenance.
- **Two clusters, two roles, never conflated.** The memory cluster is what the agent writes to;
  the target cluster is what it operates on — reachable only through a read-only probe role, an
  allowlisted DDL role (`CREATE INDEX` / `ANALYZE`, never `DROP`/`TRUNCATE`/`GRANT`), and the
  backup-freshness REST API.
- **One agent, five nodes, checkpointed every step.** Concurrency is `FOR UPDATE` leases plus
  monotonic fence tokens, not agent-to-agent messaging — which is what makes kill-and-resume a
  database guarantee rather than an orchestration promise.
- **Exactly-once across two clusters.** The decision, the intent-to-act, and the side-effect
  record commit in one transaction on the memory cluster; the DDL itself is idempotent
  (`CREATE INDEX IF NOT EXISTS`) and reconciled against the target on resume. A crash in any
  window — before, between, or after — yields exactly one `remediation_actions` row.
- **AWS provides runtime, orchestration, and durability — not the AI.** ECS Fargate hosts the
  long-lived agent (and is the demo's kill switch), SQS + EventBridge provide durable
  fingerprint-deduplicated triggers, Lambda keeps memory maintenance alive across agent death,
  S3 holds large artifacts, and IAM keeps every identity least-privileged. Reasoning and
  embeddings are third-party cloud APIs (Ollama Cloud, Cohere) because Bedrock `InvokeModel` is
  blocked account-wide on this AWS account — an account constraint that was measured, recorded,
  and designed around, not a gap.
- **Safety is structural, not procedural.** The model never emits SQL or shell — it selects from
  an allowlisted enum with typed parameters, and the adapter builds the statement. No DDL is
  applied without a fresh backup and a human approval. Full compromise of the model yields:
  *create an index on an allowlisted schema, after a human clicked approve, on a cluster with a
  verified recent backup* — nothing more (see the blast-radius table below).

---

## Written statement — AWS services (no Bedrock, said plainly)

Reasoning and embeddings are third-party cloud APIs — **Ollama Cloud** (`minimax-m3:cloud`) and
**Cohere** (`embed-english-v3.0`) — because Bedrock `InvokeModel` is blocked account-wide on this
AWS account. AWS's job here is runtime, orchestration, and durable artifacts, not the AI itself:

| Service | What it actually does here |
|---|---|
| **ECS Fargate** | Hosts the long-lived agent. Load-bearing: `aws ecs stop-task` *is* the resilience demo. |
| **Lambda** | Lifecycle workers (consolidation, decay, embedding backfill) — separate from the agent on purpose, so memory maintenance survives agent death. Also backs the approvals/metrics/webhooks API. |
| **EventBridge** | 5-minute sweep rule + lifecycle-worker schedules (currently disabled — see Product Readiness below). |
| **SQS** (`engram-commands.fifo`) | Durable trigger, FIFO deduplicated by incident fingerprint. |
| **API Gateway** | Approval callbacks, metrics proxy, webhook ingestion — the dashboard never holds a DB or IAM credential. |
| **S3** (`engram-agent-artifacts`) | Task logs, EXPLAIN bundles, plan diffs — the row holds the URI + content hash, never the blob. |
| **Secrets Manager** | Provider keys and both cluster DSNs. |
| **CloudWatch** | The demo metrics (recall hit rate, LLM latency/token usage, time-to-remediation, sweep cycle, blocked-by-backup-gate). |
| **IAM** | Four least-privilege identities (below) — no identity holds a `bedrock:*` action anywhere; the statement is deleted, not narrowed. |

---

## Written statement — CockroachDB tools, and what the agent actually did with them

- **Distributed Vector Indexing** — the load-bearing memory mechanism. Query-shape fingerprints,
  episode summaries, and induced procedures live in `memory_items` as `VECTOR(1024)` under a
  C-SPANN index (`scope_id, embedding vector_cosine_ops`). Recall is scoped ANN
  (`WHERE scope_id = … ORDER BY embedding <=> $1 LIMIT 20`), re-ranked by a hybrid score
  (similarity · confidence · recency · entity affinity) — never pure cosine. Phase 0 verified the
  index is silently unused without the `scope_id` equality constraint, so every ANN query goes
  through one choke-point module.
- **Cloud Managed MCP Server** — measured empirically in Phase 0 (`scripts/verify_mcp.py`): a
  20 s query timeout, a 10 KiB response ceiling, an implicit `LIMIT 25` on unbounded `SELECT`s,
  and deny-listed system schemas. Those measurements are why MCP is deliberately kept off the
  agent's hot path — memory reads and `EXPLAIN` measurements go straight through `psycopg3`
  instead, where the agent needs execution statistics MCP does not return. MCP is used as the
  control-plane, capability-discovery tool it is designed to be, not as a data plane.
- **`ccloud` CLI** — used during development (v0.6.12) to empirically confirm two real
  constraints that shaped the architecture: `cluster disruption` is Advanced-tier-only (unusable
  on a free Basic cluster — hence the resilience demo kills the *agent*, never the database),
  and no `cluster backup list` subcommand exists at all — which is why the backup gate calls the
  Cloud REST API directly instead.
- **CockroachDB Cloud REST API (the backup gate)** — before any DDL is applied, the agent calls
  `GET /v1/clusters/{id}/backups` with a Cluster-Admin-scoped key and refuses — parking the task
  and emitting a `blocked_by_backup_gate` metric — if no backup exists within the safety window.
- **Row-Level TTL** — forgetting is declared in DDL (`working_memory` 7d, `observations` 30d,
  `tasks`/checkpoints 90d), not implemented as an application cron job. Confidence itself decays
  via a time-weighted Wilson lower bound, so a 1/1 procedure never outranks a 47/50 one.
- **`AS OF SYSTEM TIME`** — reserved for belief-state replay: showing precisely what the agent
  believed at the instant it was killed (the audit feature), not used as a performance trick.
- **`psycopg3` on the hot path** for both clusters — `SqlProbe` (read-only `EXPLAIN ANALYZE`
  against the target) and `SqlOperator` (allowlisted DDL only: `CREATE INDEX IF NOT EXISTS` /
  `ANALYZE`, never `DROP`/`TRUNCATE`/`GRANT`).

---

## Four IAM identities + blast radius

| Identity | Used by | Permissions |
|---|---|---|
| `engram-agent-task-role` | ECS task | `s3:PutObject`/`s3:GetObject` scoped to the `engram-agent-artifacts` bucket ARN only (never `s3:*`); Secrets Manager `GetSecretValue` (pinned ARNs); CloudWatch `PutMetricData` + logs; SQS receive/delete. No `bedrock:*` anywhere. |
| `engram-worker-role` | Lambda lifecycle workers | Same shape, minus SQS. No `ecs:RunTask` — workers never start tasks. |
| `engram-api-role` | API Gateway Lambdas | Secrets Manager (reader DSN only); CloudWatch `GetMetricData` (metrics proxy only); writes to `approvals` via a dedicated CAS-scoped DSN. |
| `engram-ci-role` | GitHub Actions (OIDC) | ECR push, CDK deploy, ECS update-service. |

No identity holds both memory-cluster write and target-cluster operator credentials except the
agent task itself — and even there they are separate secrets, fetched independently, so only the
credential the current graph node actually needs is ever resolved.

**Blast radius, stated plainly:** full compromise of the reasoning model yields — create an
index on an allowlisted schema, after a human clicked approve, on a cluster with a verified
recent backup. It cannot `DROP`, `TRUNCATE`, exfiltrate via the control plane, or escalate
privilege. This is structural, not procedural: the model never emits SQL or shell — it picks
from an allowlisted enum with typed parameters, and the adapter builds the statement.

---

## Measured numbers (real, not illustrative)

| Metric | Value | Source |
|---|---|---|
| Recall latency (400-row corpus, scoped ANN) | 6 ms / 5.576 RU | Phase 0 verification |
| A real applied fix | 27ms → 1ms | First closed graph run |
| A real applied fix | 1100.0ms → 19.0ms (success) | Live rehearsal |
| A real applied fix | 1600.0ms → 4.0ms (success) | Live rehearsal |
| Kill-and-resume, applied fix | 1400.0ms → 4.0ms (success) | Live rehearsal, exactly-once confirmed at the DB level |
| Final demo recording — incident #1, applied fix | 1400.0ms → 4.0ms (success) | Final video recording take (friend cluster) |
| A correct, honest failure (measured regression) | 143.0ms → 155.0ms (`ANALYZE` alone can't fix a missing index) | Re-plan-edge live test |
| Recall similarity across separate incidents on a familiar shape | 62%, 50%, 51%, 61.6% | Live rehearsal (incl. friend-cluster final-recording rehearsal) |
| Proposed-fix confidence, friend-cluster final-recording rehearsal | 0.97 | `create_index` proposal |
| Time to remediation | 15.6s | Live rehearsal |
| LLM latency (Ollama Cloud round-trip) | 9.2s | Live rehearsal |
| Sweep cycle | 1.62s | 1h dashboard window |

---

## Falsifiability — what would prove the memory layer is *not* working

If the memory layer were a facade rather than a real mechanism, any of the following would be
observable and none of them would be explainable away:

- A second incident against a familiar query shape would show **zero citations** in the Memory
  Inspector, or citations with similarity scores indistinguishable from random (not the
  consistent 50–62% band actually measured).
- `remediation_actions.idempotency_key` collisions would produce **duplicate rows** instead of
  reconciling onto the existing one — checkable directly by counting rows after a kill-and-resume
  cycle (it stays at exactly one).
- Confidence would not decay, or a 1/1 procedure would consistently outrank a 47/50 one —
  checkable by inspecting `procedures.confidence` over time against the Wilson lower-bound
  formula.
- The backup gate would apply DDL even when the target cluster's backups list came back empty —
  checkable directly against the CockroachDB Cloud REST API alongside the resulting
  `remediation_actions` row (it should stay `parked`/refused, never `applied`).
- Killing the ECS task mid-run would produce a task with `status='failed'` and no resumed
  completion, rather than a replacement task picking the same lease back up and finishing it.

Every one of these is checkable directly against the live memory cluster or the live dashboard —
none of it depends on trusting a claim in this README.

---

## Product readiness

- **Infrastructure as code** (AWS CDK, Python) for both the dashboard's API Gateway/Lambda stack
  and the agent's SQS/EventBridge/ECS stack — both deployed live.
- **CI:** GitHub Actions builds and pushes the agent's container image on push.
- **Secrets:** never committed, never in the frontend — Secrets Manager (AWS side) and a
  gitignored `.env` (local dev), least-privilege IAM throughout (see the four identities above).
- **Observability:** CloudWatch metrics (recall hit rate, LLM latency/token usage,
  time-to-remediation, sweep cycle, blocked-by-backup-gate) and OpenTelemetry spans on every
  graph node.
- **Deliberately disabled, stated not hidden:** the 5-minute sweep rule and the three
  lifecycle-worker rules (consolidation, decay, embedding backfill) are built, deployed, and
  live-verified, but stay `enabled=False` in EventBridge — flipping them on starts a real,
  ongoing, unattended cost (Cohere/Ollama calls on every tick), a decision this project treats
  as the operator's to make deliberately, not a default to leave on silently.

---

## Repository layout

- `agent/` — the LangGraph agent (5 nodes: observe, recall, reason, gate, act_measure), providers
  (Ollama Cloud, Cohere), and tools (SQL probe/operator, backup-gate REST client).
- `dashboard/` — Next.js read-only SSE dashboard (Vercel); a single screen with five live panels
  (Task Feed, Action Feed, Memory Inspector, Approval Queue, Metrics).
- `workers/` — Lambda functions (approvals, metrics, webhooks, sweep enumerator, lifecycle
  workers).
- `infra/` — AWS CDK (Python) for both stacks.
- `db/` — SQL migrations, split by cluster (`db/migrations/` = memory cluster, `db/target/` =
  target cluster) — the two clusters are never conflated, by design.
- `design/` — high-level and low-level design documents.
- `docs/` — operational documentation: external constraints, invariants, coding conduct, the
  submission checklist, and this project's full session-by-session changelog (`CLAUDE.md`).
- `scripts/` — bootstrap, verification, and live-demo-running scripts.
- `tests/` — unit tests (226, no live credentials needed).
