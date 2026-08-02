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
        ▼  SQS engram-commands (durable trigger, FIFO by fingerprint)
        ▼
ECS Fargate ── LangGraph agent ── 5 nodes:
  Observe → Recall → Reason → Gate → Act & Measure
        │         │        │       │
   Ollama      MCP(ro)  ccloud(ro)  CloudWatch
   Cloud +                          + API GW/Lambda
   embeddings                       (approvals, metrics)
        │
        ▼
MEMORY CockroachDB cluster  ←── the product
TARGET CockroachDB cluster  ←── the subject (MCP ro + probe/operator SQL roles)
        +  S3 (artifacts) · Secrets Manager · IAM (4 identities)
```

- **Agent core:** Python 3.12, LangGraph, `langchain-cockroachdb` (`AsyncCockroachDBSaver`), psycopg3 async pool, boto3, `mcp` client, httpx.
- **Models — CHANGED 2026-08-03, see §7 Session 2 and design ADR-001/002:**
  - **Reasoning: Ollama Cloud `minimax-m3:cloud`** via direct `POST https://ollama.com/api/chat`. Replaces Claude Sonnet 5 on Bedrock, which is unreachable (see §8). Never rely on a `message.thinking` channel — the tool-call schema carries a **required `reasoning` field** instead.
  - **Embeddings: 1024 dims, provider UNRESOLVED.** Target is Bedrock Titan V2 (`amazon.titan-embed-text-v2:0`), but Bedrock invoke is blocked account-wide (§8). **This blocks Phase 2, not Phase 1.**
  - **The embedding decision is one-way.** Invariant #2 pins the *dimension*; it cannot pin the *vector space*. Titan-1024 and any other 1024-dim model produce incomparable vectors, so mixing them in one index yields silently meaningless similarity scores. **Choose the embedding provider before seeding the corpus, or accept a full re-embed.** There is no fallback ladder for embeddings — unlike reasoning, where the `LLMProvider` ABC makes a swap a config change.
- **Host:** ECS Fargate — chosen because the agent is long-lived *and* because `aws ecs stop-task` is the demo's kill switch.
- **Lifecycle workers:** Lambda (consolidation, confidence decay, embedding backfill) — deliberately separate from the agent, because memory maintenance must survive agent death.
- **Dashboard:** Next.js App Router + Tailwind + shadcn/ui on Vercel; SSE over a read-only SQL role.
- **Two clusters, two roles.** Never conflate them. The memory cluster is what we are being judged on; the target cluster is the thing we operate on.

**Not multi-agent, on purpose.** One agent, five graph nodes. Concurrency is handled by `FOR UPDATE` leases + fence tokens, not by agent-to-agent messaging. Do not add agents.

---

## 3. Schema invariants — violating any of these breaks the submission

1. `SET CLUSTER SETTING feature.vector_index.enabled = true` is a prerequisite. **Seed rows BEFORE creating the vector index** — docs warn against large batch inserts into vector-indexed tables, and `IMPORT INTO` is unsupported on them.
2. Embeddings are `VECTOR(1024)`. Index is `VECTOR INDEX (scope_id, embedding vector_cosine_ops) WITH (min_partition_size=16, max_partition_size=128)`. **Verified 2026-08-03.** Two corollaries learned the hard way: the C-SPANN index **does not serve plain `scope_id` predicates** — a non-ANN scoped lookup full-scans, so `memory_items` needs its own btree index on `(scope_id, status)` *as well*; and this invariant pins the **dimension only, never the vector space** (see §2).
3. **Every ANN query must equality-constrain `scope_id`** (`=` or `IN`) and use `ORDER BY embedding <=> $1 LIMIT k`. Without the equality constraint the index is silently not used. This is the #1 way to ship a "vector search" that isn't one.
4. `remediation_actions.idempotency_key` is `UNIQUE`. **That constraint — not application logic — is what makes double-apply impossible.** Never work around a uniqueness violation; interpret it as "already intended" and reconcile against reality.
5. `agent_leases` acquisition takes a **row lock on `task_id`** and bumps a monotonic `fence_token`; stale holders are rejected at write time. *Wording amended 2026-08-03:* the LLD implements this as `UPDATE … WHERE task_id=$1 AND expires_at < now()` followed by `INSERT … ON CONFLICT DO NOTHING`, rather than a literal `SELECT … FOR UPDATE`. That satisfies the intent — the `UPDATE` takes the row lock, only an *expired* lease can be taken, and a live holder's token is never reset — and it removes a read-modify-write window. **The invariant is the row lock plus monotonicity, not the specific statement.**
6. **Decision + intent-to-act + side-effect record commit in ONE transaction.** This is the entire thesis of the project expressed as a `BEGIN`/`COMMIT`. If you find yourself writing them separately, stop.
7. Row-Level TTL: `working_memory` 7d, `observations` 30d, LangGraph checkpoint tables enabled. Forgetting is declared in DDL, not implemented as cron.
8. `AS OF SYSTEM TIME` is reserved for belief-state replay (the audit feature). Do not use it as a performance trick.
9. Retrieval is hybrid, never pure cosine: `0.45·similarity + 0.30·confidence + 0.15·recency + 0.10·entity_affinity`, hard-filtering `confidence < 0.15` and `status <> 'active'`.
10. Confidence is a **Wilson lower bound** on `successes/attempts`, time-decayed. A 1/1 procedure must not outrank a 47/50 one.
11. Large artifacts (EXPLAIN bundles, plan diffs) go to **S3**; the row holds URI + content hash. Protects the 10 GiB free-tier budget.

---

## 4. External constraints — **measured 2026-08-03**, do not rediscover these the hard way

> Everything below marked "measured/verified" was run against the real services and is
> logged in `docs/_raw/`. Three prior assumptions turned out **false** — they are called
> out inline. Trust the measurements over the vendor docs, and over this file's history.

**Managed MCP Server** (`https://cockroachlabs.cloud/mcp`) — **MEASURED 2026-08-03**, `docs/_raw/p0-b2.log`
- Server `cockroachdb-cloud 1.0.0`, protocol `2025-11-25`, connect+init ~1.3 s.
- **Confirmed empirically:** 20 s query timeout (fired at **20.8 s**) · `SELECT` defaults to **exactly `LIMIT 25`** · **16,384-char** SQL limit (`query exceeds maximum length of 16384 characters`) · deny-list refuses with `query references a restricted schema: access to "X" is blocked for security reasons`.
- **10 KiB ceiling: NOT verified.** No probe response exceeded 4,302 B (400 rows × 3 narrow columns), so the truncation boundary is unfound — budget as ~900–1,000 rows of that shape and assume it **truncates rather than errors** until proven.
- `SHOW` 100-row cap: still untested.
- **Tool parameter names are `{database, query}` — NOT `sql`.** An unrecognised property makes the server reply `must contain exactly one statement`, which reads like a refusal. This produced a false pass in the first verification run; do not repeat it.
- **12 tools are exposed, not 9. `create_database`, `create_table`, `insert_rows` ARE callable** on our `mcp:read`-intended key. The earlier assumption that write tools are simply absent was **wrong**. Therefore `agent/tools/mcp_tool.py` must be a **deny-by-default allowlist** of the nine read tools — a passthrough is a prompt-injection hole. This is a *measured* entry for the blast-radius table.
- **It is a control plane, not a data plane.** Hot-path memory reads go through psycopg3. MCP is for introspection, self-diagnosis, and human interrogation.
- Read tools (the allowlist): `list_clusters`, `get_cluster`, `list_databases`, `list_tables`, `get_table_schema`, `select_query`, `explain_query`, `show_statement`, `show_running_queries`.
- `show_statement` and `get_table_schema` **do** report vector indexes correctly (they were our P0-P1 artifact path); `explain_query` works and returns CockroachDB's *index recommendations* section, which is the pre-gate falsification signal.
- Auth: service-account API key as `Authorization: Bearer`, pinned with the `mcp-cluster-id` header. Scope `mcp:read`, role Cluster Operator.

**ccloud CLI** — verified against **ccloud 0.6.12**, 2026-08-03
- `-o json` is global. Error codes distinguish permission-denied / not-found / rate-limited.
- Service account holds **Cluster Operator (read-only)** only.
- `ccloud cluster disruption` is **Advanced-tier only** — unusable on our free Basic cluster. Do not build the resilience demo on it; we kill the agent, not the database.
- **`ccloud cluster backup list` DOES NOT EXIST.** Available `cluster` subcommands are only: `list, info, create, delete, sql, update, regions, nodes, networking, user`. The pre-flight backup gate (P3-P3) must use the **Cloud REST API** instead:
  `GET https://cockroachlabs.cloud/api/v1/clusters/{id}/backups` → `200 {"backups": [...]}` — verified working on a **Basic** cluster (`fixtures/cloudapi-backups-basic.json`).
  - Requires a service-account role with backup read (**Cluster Admin**, not Cluster Operator) **scoped to the target cluster** — our current key returns `403 unauthorized` for the target and `200` for the memory cluster.
  - On a fresh Basic cluster the list is **empty**, so the gate's default is **refuse**. That is the demo beat we want; do not claim the allow-path was tested unless it was.
- Used: `cluster info`/`list` (entity memory), `audit list` (reconciliation).
- `cluster list` reports `plan: "SERVERLESS"` while the REST API reports `plan: "BASIC"` for the same cluster. Adapters must tolerate both.
- The model **never emits a command string.** It selects from an allowlisted enum; the adapter builds `argv`.

**Ollama Cloud** (`https://ollama.com`) — reasoning provider as of 2026-08-03
- `POST /api/chat`, `Authorization: Bearer $OLLAMA_API_KEY`, `stream: false`, tools as JSON schema.
- **`message.thinking` is never returned** by `minimax-m3:cloud`, and `<mm:think>` tags can leak into `content`. The adapter strips them before JSON parsing; audit rationale lives in the schema's required `reasoning` field.
- **Unverified until `scripts/verify_ollama.py` runs:** the model id, its availability on the Cloud tier, rate limits, latency against the 8 s demo budget, and multi-turn tool-result handling. The benchmark figures quoted in the design docs are vendor claims, not measurements of ours.
- Free tier is demo-only; budget Pro from Day 1. Circuit breaker parks the agent after N consecutive failures.

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
PHASE:    0 — Hour One  →  closing out (5 of 6 tasks PASS)
DATE:     2026-08-03 (Day 3 of 17)
DONE:     P0-P1 ✅ vector index proven (4 confirmations + self-validating probe)
          P0-P2 ✅ both Basic clusters live, v26.2.1
          P0-P3 ✅ backup signal — via Cloud REST API, NOT ccloud (§4)
          P0-B2 ✅ MCP limits measured (§4)
OPEN:     P0-B1 ❌ Bedrock invoke blocked ACCOUNT-WIDE  → reasoning moved to
                    Ollama Cloud; EMBEDDINGS STILL BLOCKED (see §8 #2)
          P0-I1 ⬜ repo + Apache-2.0 LICENSE — NOT done despite design §1
                    claiming otherwise. One commit exists with NO LICENSE.
BLOCKING: two, ranked:
          1. LICENSE-in-first-commit — Devpost hard requirement, and the
             first commit already exists. Amend it before adding a remote.
          2. Local egress on TCP 26257 (squid proxy) — blocks ALL of Phase 1
             (kill-and-resume cannot be driven from a browser SQL shell).
          Bedrock is NOT the critical path; it has ~3 days of slack.
```

**Next action, in order:** (1) `LICENSE` into the amended first commit; (2) unblock 26257; (3) `scripts/verify_ollama.py` to close P0-B1's reasoning half; (4) begin P1-P1 migrations — pure SQL authoring, needs no cluster connection to write.

**Decision deadline — 2026-08-05 (Day 5):** if Bedrock invoke still fails, commit to a non-Bedrock 1024-dim embedder *before any corpus is seeded* (see §2 — the choice is one-way).

---

## 7. Changelog

Reverse-chronological. One entry per session. Never delete entries.

### 2026-08-03 — Session 2 · Phase 0 executed · reasoning provider swapped
- **Built:** `db/phase0_vector_probe.sql` + `db/console/*.sql` (14 console-pasteable chunks, needed because local 26257 is proxy-blocked) · `scripts/verify_mcp.py` (8 probes) · `scripts/verify_bedrock.py` · `scripts/run_sql.py` (psycopg3 runner) · `scripts/requirements-verify.txt` · `docs/phase0-verification.md` (the evidence record) · `.env`/`.env.example` · `.gitignore` · fixtures from the Cloud REST API. Reviewed `design/01-high-level-design.md` + `design/02-low-level-design.md`.
- **Verified working:** **P0-P1 PASSES** — `VECTOR INDEX (scope_id, embedding vector_cosine_ops)` on free Basic v26.2.1; plan shows a `vector search` operator on `vec_probe_scope_cos` with `prefix spans` on `scope_id`, reads **11 of 400 rows**, **6 ms / 5.576 RU**; probe vector was built to equal row 200's embedding, so recovering `id=200` at distance `2.39e-07` is self-validating. Negative control (no `scope_id` predicate) correctly shows `FULL SCAN`. P0-P2, P0-P3, P0-B2 also pass (see §4 for measured limits).
- **Currently broken:** Bedrock invoke blocked account-wide (§8 #1) → **embeddings have no provider** (§8 #2). Local TCP 26257 blocked by a squid proxy (§8 #3). `LICENSE` absent from the existing first commit (§8 #4).
- **Decisions locked:** reasoning → **Ollama Cloud `minimax-m3:cloud`** (ADR-001), no dependence on a thinking channel · embeddings stay 1024-dim and the provider choice is **one-way, pre-seed** · backup gate → **Cloud REST API**, not ccloud · MCP adapter → **deny-by-default allowlist** because write tools are exposed.
- **Corrections to prior belief:** `ccloud cluster backup list` does not exist · MCP exposes 3 write tools · MCP params are `{database, query}` not `{sql}` · C-SPANN does **not** serve plain `scope_id` predicates, so `memory_items` needs its own btree index · two claims about CockroachDB introspection were made and retracted (see `docs/phase0-verification.md` §1.2 — the console `Internal error` was real but unreproduced; there is **no** omission defect).
- **Next action:** LICENSE into an amended first commit; unblock 26257; `verify_ollama.py`; P1-P1 migrations.

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
| 1 | Every Bedrock `InvokeModel`/`Converse` → `ValidationException: Operation not allowed`. Affects `claude-sonnet-5`, `us.`/`global.` profiles, **and** `claude-3-haiku` (ON_DEMAND, no form) **and** `amazon.titan-embed-text-v1/v2` (Amazon first-party). IAM is fine — `ListFoundationModels` returns 119 models, and IAM denials return `AccessDeniedException`, a different class. | **Account-level**, not model approval. New AWS account still completing activation / payment verification. Ticket belongs under *Account and billing → Activation*. | [BRAINS] | OPEN — reasoning routed around it (Ollama); embeddings still blocked. Decision deadline Day 5. |
| 2 | **Embeddings have no working provider.** The Ollama swap fixes reasoning only; the design still routes embeddings to Bedrock Titan, which is inside blocker #1. Design HLD §14 rates this risk "L / one-click enable" — **empirically wrong.** | Same as #1. | [PLUMBER] | OPEN — **blocks P2-P1 and therefore all of Phase 2.** Candidate 1024-dim substitutes: `mxbai-embed-large`, `bge-large-en-v1.5`, `qwen3-embedding:0.6b`. Must be chosen **before** seeding (§2). |
| 3 | psycopg3/`cockroach sql` to either cluster on :26257 → `received invalid response to SSL negotiation: H`. Raw probe returns `HTTP/1.1 403 Forbidden / Server: squid/4.13`. Ports 22 and 443 are open; no `*_PROXY` vars set. | Transparent Squid proxy with a port allowlist that omits 26257. Network-side, not ours. | [PLUMBER] | OPEN — **blocks all of Phase 1.** Phase 0 was completed via the Console SQL Shell + MCP over 443. Fix: different network, admin request, or move the dev loop onto EC2. |
| 4 | `LICENSE` does not exist, but a first commit already does (`4304008`). Devpost requires Apache-2.0 **in the first commit**, detectable in the About sidebar. HLD §1 claims "already done". | Design doc written ahead of execution. | [ILLUSIONIST] | OPEN — amend the root commit **before** adding a remote; trivial now, history rewrite later. |
| 5 | `design/03-adr.md` and `design/architecture.svg` are cited by both design docs (ADR-001…008) but do not exist. | Companion artifacts not yet written. | [BRAINS] | OPEN — decisions are captured inline in HLD §3 for now. |

---

## 9. Definition of done — the submission checklist

- [ ] Public repo, **Apache-2.0 `LICENSE` detectable in GitHub About sidebar**, first commit dated after 2026-06-30
      — ⚠️ **AT RISK:** commit `4304008` already exists with no `LICENSE`. Amend the root commit *before* adding a remote; after a push this becomes a history rewrite.
- [ ] Written statement: which **AWS** services and how — note that moving reasoning off Bedrock removes the flagship AWS AI service. Remaining: ECS Fargate (load-bearing — `stop-task` is the demo), Lambda, EventBridge, SQS, API Gateway, S3, Secrets Manager, CloudWatch, IAM. **Say this plainly rather than implying Bedrock does the reasoning.**
- [ ] README: quickstart, architecture diagram, four-identity + blast-radius tables, measured numbers (recall latency, beam-size trade-off, MTTR before/after), falsifiability paragraph
- [ ] **Functional demo URL, testable by a stranger with no credentials**, alive through judging
- [ ] Video < 3 min, public on YouTube/Vimeo, memory layer visibly on screen for most of the runtime
- [ ] Written statement: which CockroachDB tools + **what the agent actually did with them** (draft in strategy §14.5)
- [ ] Written statement: which AWS services and how
- [ ] Optional but do it: architecture diagram + tool feedback (strategy §21)

**Never cut, whatever slips:** kill-and-resume · the backup gate refusal · the two-incident contrast · the license · the guest-accessible demo URL.
