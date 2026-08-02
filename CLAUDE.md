# CLAUDE.md — Engram Project Memory

> **READ THIS FILE FIRST, BEFORE ANY OTHER ACTION IN A NEW SESSION.**
> **UPDATE THE CHANGELOG AND THE `CURRENT POSITION` POINTER AS THE LAST ACTION OF EVERY SESSION.** Not optional. A session that produced code but no changelog entry is an unfinished session.
>
> Detail lives elsewhere on purpose. Full rationale: `research/cockroachdb_aws_hackathon_strategy.md`. Day-by-day tasks: `execution_roadmap.md`. This file holds only what must never be re-derived.

---

## 1. Project identity

**Engram** — an autonomous database reliability engineer whose entire competence is its memory.

It watches production CockroachDB clusters, diagnoses regressions, remediates behind human approval, measures whether the fix worked, and writes the outcome back as a scored, reusable procedure — so the next incident of that shape is solved in seconds.

**The sentence everything serves:**
> Engram is the agent whose memory you can kill mid-task — it comes back, finishes the job without redoing it, and solves the next incident in seconds because it remembered the last one.

**Two demo beats (the product exists to produce these):**
1. **It remembers** — incident #2 resolved in ~8s vs #1's ~45s, with the recalled procedure, its similarity score, and its confidence visible on screen.
2. **It survives** — `aws ecs stop-task` mid-remediation; new task reclaims the lease, resumes from checkpoint, produces **one** action row where a naive agent produces two.

**Deadline: 2026-08-18 17:00 ET. Submit by 12:00 ET that day.** Hackathon: CockroachDB × AWS, five *equally weighted* criteria — Agentic Memory Design, Technological Implementation, Real-World Impact, Product Readiness, Creativity & Originality.

---

## 2. Core architecture

```
EventBridge (5min sweep / 1hr consolidate / nightly decay)
        │
        ▼
ECS Fargate ── LangGraph agent ── 5 nodes:
  Observe → Recall → Reason → Gate → Act & Measure
        │         │        │       │
   Bedrock    MCP(ro)  ccloud(ro)  CloudWatch
        │
        ▼
MEMORY CockroachDB cluster  ←── the product
TARGET CockroachDB cluster  ←── the subject (read via MCP)
        +  S3 (artifacts) · Secrets Manager · IAM (4 identities)
```

- **Agent core:** Python 3.12, LangGraph, `langchain-cockroachdb` (`AsyncCockroachDBSaver`), psycopg3 async pool, boto3, `mcp` client.
- **Models:** Claude Sonnet 5 (reasoning) + **Titan Text Embeddings V2 @ 1024 dims** (embeddings), both via Bedrock.
- **Host:** ECS Fargate — chosen because the agent is long-lived *and* because `aws ecs stop-task` is the demo's kill switch.
- **Lifecycle workers:** Lambda (consolidation, confidence decay, embedding backfill) — deliberately separate from the agent, because memory maintenance must survive agent death.
- **Dashboard:** Next.js App Router + Tailwind + shadcn/ui on Vercel; SSE over a read-only SQL role.
- **Two clusters, two roles.** Never conflate them. The memory cluster is what we are being judged on; the target cluster is the thing we operate on.

**Not multi-agent, on purpose.** One agent, five graph nodes. Concurrency is handled by `FOR UPDATE` leases + fence tokens, not by agent-to-agent messaging. Do not add agents.

---

## 3. Schema invariants — violating any of these breaks the submission

1. `SET CLUSTER SETTING feature.vector_index.enabled = true` is a prerequisite. **Seed rows BEFORE creating the vector index** — docs warn against large batch inserts into vector-indexed tables, and `IMPORT INTO` is unsupported on them.
2. Embeddings are `VECTOR(1024)`. Index is `VECTOR INDEX (scope_id, embedding vector_cosine_ops) WITH (min_partition_size=16, max_partition_size=128)`.
3. **Every ANN query must equality-constrain `scope_id`** (`=` or `IN`) and use `ORDER BY embedding <=> $1 LIMIT k`. Without the equality constraint the index is silently not used. This is the #1 way to ship a "vector search" that isn't one.
4. `remediation_actions.idempotency_key` is `UNIQUE`. **That constraint — not application logic — is what makes double-apply impossible.** Never work around a uniqueness violation; interpret it as "already intended" and reconcile against reality.
5. `agent_leases` is acquired with `SELECT … FOR UPDATE` and bumps a monotonic `fence_token`. Stale holders are rejected at write time.
6. **Decision + intent-to-act + side-effect record commit in ONE transaction.** This is the entire thesis of the project expressed as a `BEGIN`/`COMMIT`. If you find yourself writing them separately, stop.
7. Row-Level TTL: `working_memory` 7d, `observations` 30d, LangGraph checkpoint tables enabled. Forgetting is declared in DDL, not implemented as cron.
8. `AS OF SYSTEM TIME` is reserved for belief-state replay (the audit feature). Do not use it as a performance trick.
9. Retrieval is hybrid, never pure cosine: `0.45·similarity + 0.30·confidence + 0.15·recency + 0.10·entity_affinity`, hard-filtering `confidence < 0.15` and `status <> 'active'`.
10. Confidence is a **Wilson lower bound** on `successes/attempts`, time-decayed. A 1/1 procedure must not outrank a 47/50 one.
11. Large artifacts (EXPLAIN bundles, plan diffs) go to **S3**; the row holds URI + content hash. Protects the 10 GiB free-tier budget.

---

## 4. External constraints — verified 2026-08-01, do not rediscover these the hard way

**Managed MCP Server** (`https://cockroachlabs.cloud/mcp`)
- **10 KiB max response · 20 s query timeout · `SELECT` defaults to `LIMIT 25` (explicit max 10,000) · `SHOW` capped at 100 rows · 16,384-char SQL limit.**
- `system`, `crdb_internal`, `pg_catalog`, `information_schema`, `pg_extension` are **deny-listed**.
- **It is a control plane, not a data plane.** Hot-path memory reads go through psycopg3. MCP is for introspection, self-diagnosis, and human interrogation.
- Tools: `list_clusters`, `get_cluster`, `list_databases`, `list_tables`, `get_table_schema`, `select_query`, `explain_query`, `show_statement`, `show_running_queries`. Write tools exist but **we never request `mcp:write`.**
- Auth: service-account API key as `Authorization: Bearer`, pinned with the `mcp-cluster-id` header. Scope `mcp:read`, role Cluster Operator.

**ccloud CLI**
- `-o json` is global. Error codes distinguish permission-denied / not-found / rate-limited.
- Service account holds **Cluster Operator (read-only)** only.
- `ccloud cluster disruption` is **Advanced-tier only** — unusable on our free Basic cluster. Do not build the resilience demo on it; we kill the agent, not the database.
- Used: `cluster backup list` (the pre-flight gate), `cluster info`/`list` (entity memory), `audit list` (reconciliation).
- The model **never emits a command string.** It selects from an allowlisted enum; the adapter builds `argv`.

**Free tier:** 50M RU + 10 GiB/month per org. No changefeeds (RU cost) — SSE + cursor instead. No per-panel polling.

**Devpost rules:** repo public with **Apache-2.0 `LICENSE` in the first commit** (must show in GitHub's About sidebar); project must be newly created during the submission period (commit graph is the evidence); demo URL must be free to test *without our credentials*; video < 3 min, public, must show the memory layer at work.

---

## 5. Subagent roster

Every code-generation turn states which role is executing. Roles are domain boundaries — do not cross them without a changelog note.

| Role | Domain | Invariants it owns |
|---|---|---|
| **[BRAINS]** Agent Core & AI | Python, LangGraph, Bedrock, EventBridge, langchain-cockroachdb | The 5-node loop; strict JSON tool schemas (no free-form LLM output reaching a tool); `AsyncCockroachDBSaver` checkpointing; graceful Bedrock throttling + MCP timeout handling |
| **[PLUMBER]** Distributed Data & Infra | CockroachDB/psycopg3, SQL DDL, ccloud, IAM, Secrets Manager | ACID transaction boundaries; `FOR UPDATE` leases + fence tokens; `vector_cosine_ops` index; Row-Level TTL; least-privilege IAM; **kill-and-resume correctness at the DB level** |
| **[ILLUSIONIST]** Frontend & Telemetry | Next.js App Router, Tailwind, shadcn/ui, CloudWatch | Memory Inspector (similarity + confidence + provenance visible); SSE not polling (RU discipline); CloudWatch metrics `recall_hit_rate`, `time_to_remediation`, `memory_recall_latency_p99`, `blocked_by_backup_gate`, `exactly_once_conflicts_detected` |

**Frozen interface contracts** (changing one after its freeze date requires a changelog entry):

| Contract | Owner | Freeze | Consumers |
|---|---|---|---|
| SQL schema + migrations | [PLUMBER] | Day 3 | all |
| Tool-call JSON schemas | [BRAINS] | Day 4 | [PLUMBER] adapters |
| Read-only SSE query surface | [PLUMBER] + [ILLUSIONIST] | Day 5 | dashboard |

---

## 6. CURRENT POSITION

```
PHASE:    0 — Hour One (pre-build verification)
STEP:     0.1 — verify feature.vector_index.enabled on free Basic cluster
STATUS:   not started
BLOCKING: yes — steps 0.1 and 0.2 gate all design work
```

**Next action:** run Phase 0 checks in `execution_roadmap.md`. Nothing else starts until both pass.

---

## 7. Changelog

Reverse-chronological. One entry per session. Never delete entries.

### 2026-08-01 — Session 1 · Orchestration setup
- **Built:** `research/cockroachdb_aws_hackathon_strategy.md` (full strategy, 25 sections, research-verified). `CLAUDE.md` (this file). `execution_roadmap.md` (4-phase, 17-day plan with role ownership).
- **Verified working:** nothing executable yet — documents only.
- **Currently broken:** nothing built yet.
- **Decisions locked:** Engram over 23 alternatives · single agent, not multi-agent · Fargate over AgentCore Runtime (schedule risk; `stop-task` is the demo) · kill the agent, not the database (`cluster disruption` is Advanced-only) · no changefeeds (RU cost) · Apache-2.0.
- **Next action:** Phase 0 verification (vector index setting + Bedrock model access).

---

## 8. Broken / blocked register

Standing table so failures survive across sessions instead of being re-diagnosed. Remove a row only when it is genuinely fixed.

| # | Symptom | Suspected cause | Owner | Status |
|---|---|---|---|---|
| — | *(none yet)* | | | |

---

## 9. Definition of done — the submission checklist

- [ ] Public repo, **Apache-2.0 `LICENSE` detectable in GitHub About sidebar**, first commit dated after 2026-06-30
- [ ] README: quickstart, architecture diagram, four-identity + blast-radius tables, measured numbers (recall latency, beam-size trade-off, MTTR before/after), falsifiability paragraph
- [ ] **Functional demo URL, testable by a stranger with no credentials**, alive through judging
- [ ] Video < 3 min, public on YouTube/Vimeo, memory layer visibly on screen for most of the runtime
- [ ] Written statement: which CockroachDB tools + **what the agent actually did with them** (draft in strategy §14.5)
- [ ] Written statement: which AWS services and how
- [ ] Optional but do it: architecture diagram + tool feedback (strategy §21)

**Never cut, whatever slips:** kill-and-resume · the backup gate refusal · the two-incident contrast · the license · the guest-accessible demo URL.
