# CockroachDB × AWS Hackathon — Strategy, Architecture & Execution Plan

> **Status:** research complete, decision made. This document is the team's idea record, architecture spec, implementation roadmap, judging strategy, and demo plan.
> **Researched:** 2026-08-01. All technical claims below were verified against live CockroachDB / AWS / Devpost documentation on that date; sources are listed in the appendix.

---

## TL;DR — the decision

**Build `Engram`: an autonomous database reliability engineer whose entire competence is its memory.**

It watches production CockroachDB clusters, diagnoses incidents, proposes and applies remediations behind human approval gates, measures whether the fix worked, and writes the outcome back into a typed memory system so the *next* incident of that shape is solved in seconds instead of minutes. Its memory — working, episodic, semantic, entity, procedural, transactional, operational, and audit — lives in a single CockroachDB cluster. Its execution loop is checkpointed into that same cluster, so you can kill the agent mid-remediation and it resumes without re-applying the change.

Two demo beats, in order:

1. **"It remembers."** Incident #2 is resolved in ~8 seconds because incident #1 taught it a procedure, retrieved by scoped vector search with a confidence score and a citation back to the original episode.
2. **"It survives."** `aws ecs stop-task` kills the agent on camera mid-remediation. A new task reclaims the lease, resumes from the CockroachDB checkpoint, and **does not** re-run the DDL. Then `AS OF SYSTEM TIME` rewinds the memory to show precisely what the agent believed at the instant it died.

**The one sentence we want judges to remember after 50 demos:**

> *Engram is the agent whose memory you can kill mid-task — it comes back, finishes the job without redoing it, and solves the next incident in seconds because it remembered the last one.*

**Clock: submissions close 2026-08-18 17:00 ET. That is 17 days from today.** Section 19 is the day-by-day plan.

---

# 1. The hackathon, deconstructed

## 1.1 Hard requirements (from `hackathon.txt` + the Devpost rules page)

| Requirement | Detail | Consequence for us |
|---|---|---|
| Core challenge | An **agentic application** using CockroachDB as its **persistent memory layer**, deployed on AWS | The memory layer is the graded artifact, not the app around it |
| CockroachDB tools | **≥2 of 4**: Managed MCP Server, Distributed Vector Indexing, ccloud CLI, Agent Skills Repo | We use **all four**, each load-bearing (§14) |
| AWS services | **≥1** of Bedrock / Lambda / ECS / EKS / S3 / SageMaker / Bedrock Agents / other | We use 8, each justified (§15) |
| Repository | Public, open source, **MIT or Apache-2.0 license detectable in the GitHub About sidebar** | `LICENSE` at repo root, correct SPDX so GitHub's licensee detector fires |
| Novelty | **"Projects must be newly created by the Entrant during the Submission Period"** (2026-06-30 → 2026-08-18) | Fresh repo, first commit dated after Jun 30. No pre-existing code. Commit history is visible evidence |
| Demo | **Functional demo URL**, free to access and test throughout judging | Public URL with a read-only guest mode and a scripted-incident button — judges must not need our credentials |
| Video | **< 3 minutes**, public on YouTube/Vimeo, must demonstrate **"the CockroachDB memory layer at work"** | The video's job is to show *memory*, not features. Storyboard in §20 |
| Written submission | Name which CockroachDB tools and which AWS services were used **and what the agent actually did with them** | §14 and §15 are drafted to be pasted almost verbatim |
| Optional | Architecture diagram; feedback on CockroachDB AI tooling | Do both. They are free points and signal engagement (§21) |
| Team | ≤ 5 people, 18+; English materials | Fine |
| Prizes | $5,000 / $2,500 / $1,250 + blog feature; one prize per project | — |

## 1.2 Judging structure

**Stage 1** is pass/fail viability. **Stage 2** scores five criteria that the rules page states are **equally weighted**:

1. Agentic Memory Design
2. Technological Implementation
3. Real-World Impact
4. Product Readiness
5. Creativity & Originality

Two strategic consequences:

- **Equal weighting means the floor matters more than the ceiling.** A submission that is brilliant on memory design and vague on production readiness scores worse than one that is strong-but-not-dazzling across all five. Most hackathon projects collapse on *Product Readiness* — it is the least fun to build and the easiest to fake badly. It is worth 20%. **This is the cheapest 20% on the board and we should treat it as a first-class feature, not documentation.**
- **"Creativity & Originality" is worth exactly as much as "Technological Implementation."** A technically dazzling RAG chatbot loses a fifth of the available points on arrival.

## 1.3 The competitive field

2,697 registrants. The project gallery is unpublished, so competitor recon is impossible — plan against the base rate, not against observed rivals. Historical Devpost distributions suggest 3–8% of registrants submit, so expect roughly **80–200 real submissions**, of which the plurality will be: a chat assistant with pgvector RAG, a "second brain," a support-ticket bot, a coding assistant with conversation history. Our moat analysis (§23) is built on that assumption.

## 1.4 Reading "memory is not an afterthought, it is the thing that makes an agent useful in production"

This sentence is the rubric in disguise. Architecturally it asserts four things:

**(a) Memory is a write-path problem, not a read-path problem.** RAG treats memory as a read: fetch documents, stuff context, answer. The sponsor's framing is the opposite — agents "spawn autonomously, write constantly." The interesting engineering is in *what the agent decides to persist, when, with what confidence, and how that record is later corrected*. A submission that only reads from the database has misread the prompt.

**(b) Memory is the agent's identity across process boundaries.** If the process dies and a replacement can reconstruct intent, progress, and side-effect status, then "the agent" was never the process — it was the memory. This is the sentence *"an agent whose memory goes offline doesn't degrade gracefully, it stops"* stated positively. It also implies the converse test: **an agent whose *process* goes offline should degrade gracefully, because its memory did not.**

**(c) Memory has a lifecycle.** Human-scale apps store rows forever or delete them on request. Agent memory must be *created, consolidated, contradicted, decayed, superseded, and forgotten* — otherwise it accumulates into noise and the agent gets worse over time, not better. Almost nobody will build the forgetting half.

**(d) Memory must be transactional with the actions it justifies.** If "I decided to create this index" and "I created this index" are not in one atomic unit, then a crash between them produces an agent that either repeats destructive work or loses the record of it. This is exactly the guarantee a distributed SQL database provides and a vector store plus Redis cannot. **This is the strongest single argument for CockroachDB in this hackathon, and it is not a performance argument — it is a correctness argument.**

## 1.5 The four-category ladder

The brief implicitly ranks submissions. We must be unambiguously in category 4.

| # | Category | Signature | Failure mode |
|---|---|---|---|
| 1 | **AI app that happens to use CockroachDB** | CRDB is the users/sessions table; the LLM call is stateless | Swap Postgres in, nothing changes. Zero points on Memory Design |
| 2 | **RAG app with a database** | Embeddings + similarity search; memory = document corpus | Memory is read-only and externally authored. The agent learns nothing |
| 3 | **An actual agentic system** | Multi-step planning, tool use, loops, autonomy | State lives in the process or the context window. Kill it and the task dies |
| 4 | **Agentic system where persistent memory is constitutive** | The agent's competence, continuity, and safety are all properties of the database | — |

**The falsifiable test we will state in our README:** *remove the CockroachDB memory layer and the product does not degrade — it cannot function.* For Engram: without memory it has no procedures to recall (so every incident is solved from scratch, badly), no lease (so two instances race to apply the same DDL), no idempotency ledger (so a restart double-applies changes), no entity model of the target cluster, and no audit trail (so no human would ever grant it write access). Every one of the five things that make it *safe to run in production* is a row in CockroachDB.

## 1.6 Hidden constraints most teams will miss

1. **The managed MCP server is a control plane, not a data plane.** Verified limits: 10 KiB maximum response, 20-second query timeout, `SELECT` defaults to `LIMIT 25` (explicit max 10,000), `SHOW` capped at 100 rows, and `system`, `crdb_internal`, `pg_catalog`, `information_schema`, `pg_extension` are all deny-listed. Any architecture that routes hot-path memory retrieval through MCP will hit a wall in week two. Correct split: **psycopg3 for the memory data plane; MCP for introspection, self-diagnosis, and human interrogation.** Saying this out loud in the README is itself a signal of engineering judgment.
2. **`ccloud cluster disruption` is Advanced-tier only.** The obvious resilience demo — have the agent inject a cluster fault — is unavailable on the free Basic cluster. Build the resilience demo on *agent* death instead (§13); it is free, more visually credible, and more on-thesis.
3. **Vector index prefix columns must be equality-constrained.** This looks like a limitation and is actually the design hint: scope every ANN search by tenant/entity. Global unscoped similarity search is the naive usage.
4. **"Newly created during the submission period" is checkable.** Judges can read the commit graph. Do not import a pre-existing project.
5. **The demo must be testable without us.** "Email us for a login" fails the requirement. Guest mode is mandatory.
6. **Free-tier economics are real.** 50M RUs + 10 GiB/month per org. Changefeeds, background jobs, and automatic statistics all consume RUs. A chatty polling dashboard can burn the budget mid-judging. Budget explicitly (§18).

---

# 2. Thinking like the judges

Judges will be Cockroach Labs and AWS engineers watching dozens of 3-minute videos and cloning a handful of repos. They reward *correct, non-obvious use of technology they built* and punish claims the repo does not support. Below, per criterion: what average / strong / winning looks like, what loses points, and what specifically impresses.

## 2.1 Agentic Memory Design (20%)

- **Average:** one `messages` table plus an `embeddings` table. Memory = chat transcript. Retrieval = top-k cosine on everything.
- **Strong:** several memory *types* with different retrieval strategies; hybrid semantic + recency + metadata ranking; per-user scoping.
- **Winning:** memory as a **system with a lifecycle** — typed classes with distinct write triggers and TTLs; a consolidation job promoting raw episodes into reusable, scored procedures; explicit **provenance** (which run, which tool call, which observation produced this belief); **confidence** that decays and is updated by outcome; **contradiction handling** when a remembered procedure stops working; **forgetting** as a designed feature; and **memory that is transactional with the action it justifies**.
- **Loses points:** memory that is write-only (stored but never retrieved) or read-only (retrieved but never written); "we store embeddings" as the entire memory story; no answer to "what happens after 10 million rows"; no answer to "what happens when a memory is wrong."
- **Impresses:** a `procedures` table with `attempts`/`successes`/`confidence` where the demo visibly increments them. `AS OF SYSTEM TIME` used to reconstruct past belief state — very few databases can do this, and it makes agent memory *auditable in time*. Row-Level TTL on working memory so the design has a garbage collector.

## 2.2 Technological Implementation (20%)

- **Average:** MCP configured in Cursor during development and mentioned in the README; `ccloud` used once to create the cluster; the agent is a single prompt with a while-loop.
- **Strong:** clean data-access layer, real connection pooling, retries, a proper agent framework, working vector index with sensible dimensions, tools called through validated schemas.
- **Winning:** the agent calls MCP **at runtime** as an actual tool in its reasoning loop, with a read-only service account and pinned cluster; ccloud invoked through a **command allowlist** with parsed `-o json` output and typed error handling; vector index built with prefix columns and tuned `vector_search_beam_size`; transactions used where they matter (`SELECT … FOR UPDATE` leases, decision+action atomicity); idempotency keys on every side effect; the whole thing runs from `docker compose up` or one `make deploy`.
- **Loses points:** string-concatenated SQL; shelling out to `ccloud` with unvalidated model-generated arguments (a judge will spot this instantly and it also fails Product Readiness); embeddings recomputed on every request; "we used MCP" where MCP was only a dev-time convenience; secrets in the repo.
- **Impresses:** correct handling of the MCP response limits (pagination/summarisation rather than pretending they don't exist); using `explain_query` so the agent validates its own proposed index *before* asking for approval; a `vector_search_beam_size` tuning note showing measured recall/latency trade-off.

## 2.3 Real-World Impact (20%)

- **Average:** a plausible-sounding persona and no evidence anyone has this problem.
- **Strong:** a specific workflow, a named role, a before/after time saving.
- **Winning:** a problem the *judges personally have*, quantified, with the agent's output being the artifact a professional would actually action — plus an honest statement of what it must not be trusted to do alone.
- **Loses points:** "this could revolutionise healthcare." Breadth claims with no depth. Ignoring that the human in the loop exists.
- **Impresses:** naming the failure mode of the *current* practice (3am pages, tribal knowledge leaving with the senior DBRE, the same index added and reverted twice in six months) and showing the memory system as the fix for institutional amnesia.

## 2.4 Product Readiness (20%) — *the criterion that decides this hackathon*

- **Average:** a README section titled "Future Work: security."
- **Strong:** environment-based secrets, some logging, a Dockerfile, IAM roles.
- **Winning:** distinct least-privilege identities per capability (read-only introspection ≠ mutation ≠ control plane); **MCP pinned to `mcp:read` on a Cluster-Operator service account** so the agent structurally cannot mutate through it; destructive actions behind a **typed approval gate** with a recorded approver and expiry; idempotency keys; leases preventing split-brain; exponential backoff with jitter; a **pre-flight safety gate** that refuses to mutate unless ccloud confirms a recent backup exists; CockroachDB metrics exported into CloudWatch so agent logs and database metrics sit in one pane; structured audit rows for every decision and tool call; **prompt-injection quarantine** for untrusted text ingested from target databases; and a written blast-radius statement.
- **Loses points:** an agent with a Cluster Admin key and a shell tool. A public demo that lets anonymous visitors run DDL. No story for "what if the LLM hallucinates a destructive command."
- **Impresses:** a table of **what the agent is structurally incapable of doing**, mapped to the IAM/RBAC mechanism that enforces it. Judges from a database company will read that table first.

## 2.5 Creativity & Originality (20%)

- **Average:** a known product category with an LLM bolted on.
- **Strong:** a familiar domain with one genuinely new mechanism.
- **Winning:** a mechanism that demonstrates *insight into what makes agentic systems different from traditional apps* — the brief's own words. Time-travel over agent belief. Procedures that earn confidence. Memory that forgets on purpose. Exactly-once side effects across agent death.
- **Loses points:** novelty of *domain* mistaken for novelty of *mechanism*. ("An AI agent for beekeeping" is not original; it is a chatbot in a hat.)
- **Impresses:** an idea whose *demo* could not be faked with ChatGPT plus a vector store in a weekend — because the interesting behaviour only appears across a failure or across time.

## 2.6 Inferred profile of the winner

1. Solves an operationally serious problem with real side effects, so safety and exactly-once semantics *matter* rather than being decoration.
2. Has a typed memory system with a visible lifecycle, including consolidation and forgetting.
3. Uses three or four CockroachDB tools where each is genuinely required by the product.
4. Makes resilience **visible on camera** in under 30 seconds.
5. Shows measurable *improvement over time* — the agent is better on task #2 than task #1, and you can see why in the database.
6. Has a security model a database engineer would sign off on.
7. Ships a public demo a stranger can drive.

Engram is designed backwards from this list.

---

# 3. CockroachDB capability analysis

## 3.1 Managed MCP Server — the agent's introspection organ

**Verified surface.** Endpoint `https://cockroachlabs.cloud/mcp`, HTTP transport. Auth by OAuth 2.1 + PKCE (scopes `mcp:read`, `mcp:write`) or a service-account API key as `Authorization: Bearer …`. Optional `mcp-cluster-id` header pins one cluster. Requires the Cluster Admin or Cluster Operator role. Read-only by default; write tools appear only with explicit `mcp:write` consent; `DROP`/`TRUNCATE` are never exposed. Every call emits `mcp`-tagged structured logs (tool name, cluster/org, redacted SQL shape, latency, response size).

| Tool | Scope | How an autonomous agent uses it |
|---|---|---|
| `list_clusters`, `get_cluster` | read | Discover the fleet it is responsible for; detect a new cluster and start building entity memory for it |
| `list_databases`, `list_tables`, `get_table_schema` | read | Build and refresh **entity memory** of the target's schema; detect drift since last observation |
| `select_query` | read | Sample data distribution and cardinality to justify an index recommendation |
| `explain_query` | read | **Validate its own proposed fix before asking a human**: does the plan actually use the new index? |
| `show_running_queries` | read | Live diagnosis: what is running right now, is the cluster under load, is it safe to mutate |
| `show_statement` | read | Recover the full text of the offending statement for fingerprinting into memory |
| `create_database`, `create_table`, `insert_rows` | `mcp:write` | *Deliberately not granted.* Our mutation path is separate and approval-gated |

**The insight.** The response limits (10 KiB, 20 s, `LIMIT 25`) and the `crdb_internal` deny-list make it unmistakable: this is not a bulk data interface. It is (a) a safe, audited introspection surface for an agent reasoning about a database it does not own, and (b) a natural-language interface for humans to interrogate a database. We exploit **both**:

- **Runtime, agent-facing:** the diagnosis sub-graph calls MCP tools through an adapter that enforces our own timeout and truncates/summarises before the response reaches the model.
- **Judge-facing, and this is the trick:** we register **our memory cluster** as a second MCP connection so a judge can open Claude Code, point it at Engram's memory, and ask *"which procedures has this agent learned, and what is its confidence in each?"* — getting answers straight from CockroachDB with no code from us. That converts a required integration into the most persuasive verification path a judge could ask for.

Checkbox vs. meaningful: configuring MCP in your IDE while developing is a checkbox. An agent calling `explain_query` to falsify its own hypothesis, at runtime, under a read-only service account, is meaningful.

## 3.2 Distributed Vector Indexing — scoped, typed recall

**Verified surface.** `VECTOR(n)` columns. `CREATE VECTOR INDEX ON t (prefix…, embedding vector_cosine_ops)`, or inline `VECTOR INDEX (…)` in `CREATE TABLE`. Opclasses: `vector_l2_ops` (default, `<->`), `vector_cosine_ops` (`<=>`), `vector_ip_ops` (`<#>`). Requires `SET CLUSTER SETTING feature.vector_index.enabled = true`; adding to a non-empty table needs `SET sql_safe_updates = false`. Reads tuned with `SET vector_search_beam_size` (default 32); build tuned with `WITH (min_partition_size=16, max_partition_size=128, build_beam_size=8)`. The index is used only for `ORDER BY embedding <op> $1 … LIMIT k`, and **every prefix column must be equality-constrained**. Algorithm is **C-SPANN** — hierarchical k-means partitions with SPFresh/ScaNN lineage, maintained transactionally like any other secondary index. Known limits: avoid large batch vector inserts; `IMPORT INTO` unsupported on vector-indexed tables; no index recommendations; `vector_l1_ops`/`bit_hamming_ops`/`bit_jaccard_ops` unimplemented.

**Why this matters beyond "we did RAG."** Two properties are genuinely hard to get from a bolt-on vector store:

1. **Transactional consistency between vectors and the operational rows they describe.** A procedure's embedding, its success counter, and the audit row proving it worked all commit in one transaction. With a separate vector database there is a window where the agent can retrieve a procedure that the operational store has already invalidated — an agent acting on a stale memory it believes is current. That is not a latency problem, it is a *correctness* problem, and it is the cleanest technical argument in our whole submission.
2. **Prefix-scoped ANN.** `(scope_id, embedding)` means recall is partitioned by tenant/cluster/entity. Multi-tenant agent memory without the leakage risk of filtering after retrieval, and search cost that does not grow with unrelated tenants' data.

Our vector usage, concretely — three separate embedded memory classes, not one blob:

| Vector | Dim | Prefix cols | Query |
|---|---|---|---|
| Query-shape fingerprint (normalised SQL + plan shape) | 1024 | `(cluster_id)` | "Have I seen a query shaped like this before on this cluster?" |
| Episode summary (what happened during a past incident) | 1024 | `(org_id)` | "What past incidents resemble this one, anywhere in the fleet?" |
| Procedure description (a reusable remediation recipe) | 1024 | `(org_id, kind)` | "Which recipe applies, and how confident am I?" |

Embeddings from **Amazon Titan Text Embeddings V2 at 1024 dimensions**, with `vector_cosine_ops`. We will record measured recall/latency at `vector_search_beam_size` ∈ {8, 32, 64} in the README — cheap, and it demonstrates we actually operated the index rather than declaring it.

## 3.3 ccloud CLI — the agent's hands on the control plane

**Verified surface.** `-o json` is a **global** flag on every command; consistent noun-verb structure so an agent can discover operations from `--help`; machine-parseable error codes distinguishing permission-denied / not-found / rate-limited; full Cloud API coverage. RBAC: **Cluster Operator** = read-only (summaries, backups, logs, metrics); **Cluster Admin** = adds configuration and backup mutation, scoped per cluster. Notable groups: `auth`, `cluster create|list|info|delete|sql|versions`, `cluster database`, `cluster user`, `cluster backup list|config`, `cluster restore list|create`, `cluster networking allowlist|egress-rule|private-endpoint`, `cluster version-deferral`, `cluster blackout-window`, `cluster maintenance`, **`cluster disruption` (Advanced tier only)**, `cluster cmek`, `cluster log-export`, `cluster metric-export cloudwatch|datadog|prometheus`, `audit list`, `organization get`, `service-account …`, `service-account api-key …`, `billing invoice`, `folder …`, `replication …`, `quickstart create`.

**Genuinely agentic uses we will implement:**

1. **Pre-flight backup gate (the best one).** Before *any* mutating remediation, the agent runs `ccloud cluster backup list --cluster <id> -o json`, parses the newest backup timestamp, and **refuses to proceed** if it is older than the configured RPO — writing a `blocked` decision with the reason into memory. This is the single most professional behaviour in the whole project: an agent that declines to act because the safety precondition is not met. It reads as production judgment, and it makes ccloud structurally required rather than decorative.
2. **Infrastructure awareness feeding entity memory.** `ccloud cluster list -o json` and `cluster info` on a schedule populate the `entities` table with region topology, node count, plan tier, and CockroachDB version — so the agent's advice is version-aware, and a version change invalidates cached procedures.
3. **Audit reconciliation.** `ccloud audit list -o json` is pulled after every mutation and cross-checked against our own `audit_log`. A control-plane action present in Cockroach's audit log but absent from ours means something acted outside the agent's ledger — the agent raises that as an anomaly. This is memory used for *self-verification*, which almost nobody will think to do.
4. **Observability wiring.** `ccloud cluster metric-export cloudwatch enable` puts target-cluster metrics into the same CloudWatch account as the agent's own traces, so the "did my fix work?" measurement uses first-party metrics rather than our own instrumentation.
5. **Documented but unused, honestly:** `cluster blackout-window` (change freeze during migration) and `cluster disruption` (fault injection) are the natural next uses; both need Advanced tier and we will say so plainly rather than implying we used them.

**Safety.** The model never emits a ccloud command string. It selects from an enum of **parameterised, allowlisted operations**; the adapter builds `argv` itself, appends `-o json`, and validates against a JSON schema. The service account holds **Cluster Operator** only — read-only on the control plane. Mutating ccloud calls (metric-export enable) are performed once at setup by a human, not by the agent. Blast radius: an Engram control-plane exploit yields read access to cluster metadata and nothing else.

## 3.4 Agent Skills — the agent's expertise, versioned

**Verified surface.** `github.com/cockroachlabs/cockroachdb-skills`, Apache-2.0, installed with `npx skills add cockroachlabs/cockroachdb-skills`; `skills/<name>/SKILL.md` with CI-validated frontmatter; nine domains: onboarding & migrations, application development, performance & scaling, operations & lifecycle, resilience & DR, observability & diagnostics, security & governance, integrations & ecosystem, cost & usage.

Most teams will install these into their IDE and call it an integration. We do something materially different: **we make skills a first-class, retrievable capability of the running agent.**

- At build time, a loader parses the vendored `skills/` tree, chunks each `SKILL.md`, embeds it, and stores it in `memory_items` with `class='skill'`, `source='cockroachdb-skills@<git-sha>'`.
- At runtime, the diagnosis node performs a scoped vector search over `class='skill'` and injects only the two or three relevant skills into the reasoning context — so a query-plan problem retrieves the performance-and-scaling skill, a replication problem retrieves the resilience skill. **This is semantic memory whose contents are external expertise rather than the agent's own experience**, which is a clean and defensible distinction to draw in the README.
- Every decision records which skill SHAs informed it. When we bump the vendored version, procedures derived from superseded skills are marked `stale` and re-validated. **That is provenance-aware, version-aware memory** — and it is the most sophisticated thing anyone is likely to do with this repo.

Checkbox vs. meaningful: `npx skills add` in your dev environment is a checkbox. Skills retrieved by embedding at runtime, cited in the decision record, and version-invalidated is meaningful.

---

# 4. AWS architecture rationale

One rule: **every service must have a job no other component can do.** Complexity for its own sake reads as inexperience.

| Service | Job | Why it, specifically |
|---|---|---|
| **Amazon Bedrock** | Reasoning (Claude Sonnet 5) + embeddings (Titan Text Embeddings V2 @ 1024-d) | Required-list service; one API for both reasoning and embeddings; no key management for models; Titan V2 at 1024-d is a natural fit for `VECTOR(1024)` and keeps index partitions small |
| **Amazon ECS (Fargate)** | Long-lived agent runtime | The agent runs for hours; Lambda's 15-minute ceiling doesn't fit. Fargate needs no cluster management. Decisive extra reason: **`aws ecs stop-task` is a one-command, on-camera kill switch, and service auto-restart is the resume trigger.** Our headline demo is a native property of the runtime |
| **AWS Lambda** | Memory-lifecycle workers: consolidation, confidence decay, embedding backfill, alert webhook ingestion | Bursty, scheduled, independently scalable, and correctly *separate from* the agent — memory maintenance must survive agent death, which is exactly the point we're making |
| **Amazon EventBridge** | Scheduled triggers (5-min observation sweep, hourly consolidation, nightly decay) and alert fan-in | Gives the agent a heartbeat, so it is genuinely long-lived and autonomous rather than request-response. Decouples "something happened" from "an agent is running" |
| **Amazon S3** | Large artifacts: EXPLAIN bundles, plan diffs, before/after metric snapshots, approval evidence | Keeps multi-hundred-KB blobs out of the memory cluster; CockroachDB stores the S3 URI plus a content hash. Correct separation of *memory* from *artifact*, and it protects our 10 GiB free-tier budget |
| **AWS Secrets Manager** | CockroachDB DSNs, ccloud service-account API key, MCP bearer token | Rotation and IAM-scoped access. No secrets in env files or the repo |
| **Amazon CloudWatch** | Agent logs, custom memory metrics, traces; destination for `ccloud cluster metric-export` | One pane for agent behaviour *and* database health. Makes "did the fix work?" measurable from first-party data |
| **AWS IAM** | Distinct task roles: agent, consolidator, dashboard-BFF | Enforces the blast-radius table in §16 in the infrastructure rather than in prose |

**Considered and deliberately rejected** (saying so is a maturity signal):

- **Bedrock AgentCore Memory** — the road not taken, and we address it head-on. AgentCore Memory is a managed short/long-term memory service, and using it would have been the path of least resistance. We don't, for reasons we can defend: our memory must be **transactional with the side effects it authorises** (decision + action + idempotency key in one commit), **queryable in SQL by humans and auditors** (joins across procedures, decisions, and outcomes), **time-travelable** for audit (`AS OF SYSTEM TIME`), and **the same store as our operational data** so there is no consistency gap. Those are properties of a distributed SQL database, not of an opaque managed memory API. *This paragraph belongs in the video and the README; it proves we chose CockroachDB rather than defaulted to it.*
- **AgentCore Runtime / Gateway** — genuinely attractive (8-hour workloads; zero-code MCP tool creation from Lambdas). Rejected for a 17-day build purely on schedule risk, and named as the first post-hackathon migration. Honest, and it shows we know the platform.
- **Step Functions** — durable orchestration is the one thing we are *deliberately* implementing in CockroachDB, because that is the entire thesis. Using Step Functions would outsource our own headline feature to AWS. Worth stating explicitly; a sharp judge will wonder.
- **SageMaker** — no custom training. Bedrock covers inference.
- **EKS** — Kubernetes for three services is unjustifiable at this scale.
- **API Gateway** — the dashboard's Next.js route handlers are the API; adding a gateway in front of one service is decoration.

---

# 5. The idea pool — 24 candidates

Generated against one filter: **persistent memory must create a capability that would otherwise be impossible or unreliable.** Ideas are numbered for reference in the scoring matrix (§8). Depth is deliberately uneven — the eight strongest get full treatment; weaker ones get enough to justify their score and their rejection. Pretending to evaluate a 4/10 idea in 400 words would be padding, not rigour.

### 5.1 — `Engram` · Autonomous database reliability engineer ⭐
- **Pitch:** An agent that diagnoses and remediates production database incidents, and gets measurably faster at it because every incident becomes a scored, reusable procedure.
- **Problem / users:** Database reliability knowledge is tribal. The senior DBRE who knows "that table always needs a covering index after a bulk load" leaves, and the org relearns it at 3am. Users: platform/DBRE/on-call engineers at any company running distributed SQL.
- **Autonomous behaviour:** Sweeps target clusters on a schedule; detects regressions; forms hypotheses; validates them with `explain_query`; recalls similar past incidents; proposes remediation with a confidence score; requests human approval; verifies backup freshness; applies the change exactly-once; measures p99 before/after; writes the outcome back, updating the procedure's confidence up or down.
- **Why memory is essential:** Competence (procedures), continuity (checkpoints across death), safety (idempotency ledger + lease), context (entity model of each cluster), and accountability (audit) are *all* database rows. Remove the memory layer and it is not a worse agent, it is an unsafe one.
- **Memory types:** all eight (§7) — this is the only idea in the pool that legitimately needs the full taxonomy.
- **CockroachDB:** entities, episodes, procedures + confidence, decisions, tool_calls, approvals, remediation_actions (unique idempotency key), observations, LangGraph checkpoints, audit_log; three vector classes; TTL on working memory; `AS OF SYSTEM TIME` for belief replay.
- **Vector / MCP / ccloud / Skills:** scoped ANN over query-shapes, episodes, procedures, and vendored skills · runtime read-only introspection + self-falsification via `explain_query` + judge-facing NL interrogation of memory · pre-flight backup gate, entity refresh, audit reconciliation, CloudWatch metric export · skills embedded and retrieved at runtime, cited in decisions, version-invalidated. **All four, all load-bearing.**
- **AWS:** Bedrock, ECS Fargate, Lambda, EventBridge, S3, Secrets Manager, CloudWatch, IAM.
- **Killer demo:** incident #2 solved in ~8s by recall; then kill the container mid-remediation and watch exactly-once resume; then time-travel the memory to the instant of death.
- **Impact / difficulty / originality:** high (universal ops pain, quantifiable MTTR) / high / moderate domain but high mechanism novelty.
- **Biggest technical risk:** reliably reproducing a *convincing* performance regression on a free Basic cluster inside 3 minutes.
- **Biggest product risk:** judges reading it as "a tool for DBAs" rather than a general capability. Mitigation: frame as institutional memory for operations, with the DB as the first vertical.

### 5.2 — `Ratchet` · Long-horizon fleet migration pilot
- **Pitch:** An agent that drives a multi-day schema or dependency migration across dozens of services, learning a reusable recipe as it goes.
- **Autonomy:** plans per-service migration order from a dependency graph; executes each step; pauses at approval gates; resumes after failure; abandons and rolls back a service that fails validation, remembering why.
- **Why memory is essential:** the strongest "cannot fit in a context window" argument in the pool — a 50-service migration spans days and hundreds of decisions. State *must* be external, and side effects must never repeat.
- **Memory types:** procedural, transactional, entity, episodic, audit.
- **CockroachDB:** migration_plans, service entities, per-step state machine rows, idempotency ledger, recipe vectors ("services shaped like this one used recipe X").
- **Tools:** vector (recipe reuse) · MCP (target schema introspection pre/post) · ccloud (backup gate, blackout-window on Advanced) · Skills (onboarding & migration domain — a near-perfect fit).
- **Demo weakness:** the value is visible over days. A 3-minute video must simulate elapsed time, which weakens the strongest claim.
- **Risks:** technical — building a believable 20-service fixture; product — hard to make a judge *feel* a multi-day win in 180 seconds.

### 5.3 — `Casefile` · AML / fraud investigation agent with case memory
- **Pitch:** A long-running investigator that accumulates typology knowledge and can never double-file a regulatory report.
- **Autonomy:** triages alerts, pulls entity history, matches against remembered typologies, drafts a narrative, escalates or closes, learns from the analyst's override.
- **Why memory is essential:** case state is legally consequential and must be ACID; "we filed this SAR twice because a container restarted" is a regulatory incident, not a bug. Exactly-once is a compliance requirement, not an optimisation.
- **Memory types:** transactional (dominant), entity, episodic, semantic, audit.
- **CockroachDB:** cases, alerts, entity graph, typology vectors, filings with unique idempotency keys, immutable decision log; `AS OF SYSTEM TIME` is genuinely valuable for regulator questions ("what did the system know on the 14th?").
- **Tools:** vector (typology + similar-case recall) · MCP (read-only case-store introspection — thinner) · ccloud (retention/backup evidence, audit list — posture rather than action) · Skills (security & governance, marginal).
- **Weakness:** two of the four CockroachDB tools become evidentiary rather than operational. Technological Implementation caps out below Engram.
- **Risks:** technical — synthetic financial data convincing enough to be credible; product — judges cannot verify domain correctness, so they discount it.

### 5.4 — `Watchpost` · SOC alert triage with institutional memory
- Same shape as 5.3 in the security domain: remembers which alert patterns were previously benign in *this* environment, so it stops re-escalating the known-noisy backup job. Analyst feedback is the training signal; memory is the model.
- **Strength:** the "false-positive fatigue" pain is universally understood and the memory value is obvious in one sentence.
- **Weakness:** "AI SOC analyst" is one of the most-attempted agent demos of the last two years. Originality ≈ 5. ccloud has almost no honest role.

### 5.5 — `Followthesun` · Multi-region on-call handoff agent
- **Pitch:** Incident context that follows the sun. The agent instance in Frankfurt picks up, with complete memory, the incident that Virginia was working when the shift and the region changed.
- **Why interesting:** the only idea where **multi-region CockroachDB is a product feature rather than a brag** — memory domiciled per region, readable globally, survivable when a region is lost.
- **Demo:** genuinely spectacular — kill the region, not the process; the agent continues mid-sentence from another continent.
- **Weakness:** feasibility. Multi-region Basic clusters exist (≤6 regions, primary region selected) but latency-realistic multi-region behaviour, RU cost, and a credible region-failure simulation are a lot to land in 17 days on a free tier. **We adopt its mechanism as Engram's documented stretch goal rather than its identity.**

### 5.6 — `Provenance` · Flight recorder for agent fleets
- **Pitch:** A black box for other people's agents. Every decision, tool call, retrieved memory, and model version recorded with provenance, so when an agent misbehaves you can *replay what it believed* rather than guess.
- **Autonomy:** ingests decision traces from arbitrary agents; clusters them; detects behavioural drift; surfaces "this agent's confidence in procedure X has collapsed"; proposes causes.
- **Why memory is essential:** the memory *is* the product; there is nothing else.
- **Standout mechanism:** `AS OF SYSTEM TIME` replay of agent belief state — a genuinely novel capability that most databases cannot offer, and a perfect showcase of why the memory layer being a real database matters.
- **Weakness:** it is infrastructure for a market that barely exists yet, so Real-World Impact scores lower; and "we built an observability tool" risks reading as adjacent to CockroachDB's own product rather than an application of it.
- **Verdict:** **most original idea in the pool.** We absorb its time-travel replay into Engram (§12.7) rather than building it standalone.

### 5.7 — `Quartermaster` · Agent governance & budget control plane
- Transactionally enforced quotas, capability grants, and spend limits for a fleet of agents; every grant and revocation is an auditable memory. ACID matters because two agents must not both consume the last unit of a budget.
- **Strength:** excellent Product Readiness story; genuinely needed as agent fleets grow.
- **Weakness:** invisible in a demo (the exciting moment is a *denial*), and impact is speculative.

### 5.8 — `Deadman` · Durable exactly-once runner for regulated batch operations
- Payroll runs, settlement batches, billing cycles: multi-hour jobs with irreversible side effects that must survive worker death without double-paying anyone.
- **Strength:** the purest expression of transactional agent memory; Product Readiness 9/10; highly feasible.
- **Weakness:** barely agentic — this is durable-execution middleware with an LLM planner bolted on. Creativity and Memory Design both suffer because there is no learning loop and no semantic memory. **We absorb its idempotency discipline into Engram.**

### 5.9 — `Thermostat` · Autonomous FinOps agent that learns from its own changes
- Acts on cloud spend, waits, measures the effect, remembers whether the change actually saved money, and revises its beliefs. `ccloud billing invoice` and `metric-export` fit naturally. Genuine observe→act→learn loop.
- **Weakness:** the feedback loop is days long, which is fatal for a 3-minute video unless simulated — and simulated cost data undercuts the credibility that is the whole point.

### 5.10 — `Culvert` · Autonomous data-pipeline healer
- Remembers every schema drift and every fix; when an upstream partner changes a column type at 2am, it recognises the shape and repairs the pipeline. Good impact, natural procedural memory, decent demo.
- **Weakness:** mechanically similar to Engram with a weaker CockroachDB-tool story (MCP and ccloud have little to do). Strictly dominated.

### 5.11 — `Attestor` · Continuous compliance evidence agent
- Collects control evidence continuously, maps it to frameworks, remembers gaps and who closed them, produces an immutable audit memory. `ccloud audit list` and backup-config evidence are *genuinely* the product here.
- **Weakness:** the demo is a spreadsheet filling in. Demo Quality 5.

### 5.12 — `Obligor` · Contract obligation tracker
- Extracts obligations and deadlines, builds counterparty entity memory, escalates. Real value, but it is fundamentally extraction + reminders: closer to document Q&A than to an agentic system with a learning loop.

### 5.13 — `Priorauth` · Prior-authorisation agent with payer-rule memory
- Highest raw human impact in the pool (US prior-auth is a well-documented source of both cost and harm). Remembers each payer's evolving, undocumented rules from past outcomes — an excellent "memory as institutional knowledge" story.
- **Weakness:** we cannot obtain real payer behaviour, judges cannot verify clinical correctness, and a wrong answer in this domain is not a bug but a harm. Feasibility 5.

### 5.14 — `Bedside` · Clinical shift-handoff agent
- SBAR handoffs with longitudinal patient context. Real memory need. Rejected on feasibility and on the ethics of demoing synthetic patients to judges who cannot validate it.

### 5.15 — `Relay` · Disaster-response resource coordination
- Cross-region, partition-tolerant coordination memory; genuinely life-critical, and CockroachDB's survivability story is thematically perfect.
- **Weakness:** requires building an entire simulated world to demo. Feasibility 4.

### 5.16 — `Manifest` · Supply-chain disruption replanner
- Supplier entity memory plus a library of past replans. Solid but generic; there is no mechanism here a vector database could not fake, and the tool story is thin.

### 5.17 — `Bench` · Autonomous lab notebook that refuses to repeat failed experiments
- Hypothesis → experiment → result memory; the agent's key behaviour is *declining* to run something it already knows fails. Charming and genuinely original as a mechanism (negative memory as a first-class citizen).
- **Weakness:** narrow audience, weak ccloud/MCP story, hard to make legible in 3 minutes.

### 5.18 — `Quorum` · Multi-agent review with contested memory
- Several reviewer agents write to a **shared** memory, disagree, and the system records dissent and resolution rather than a single consensus answer. Conflict resolution and provenance are the product.
- **Strength:** original; a real showcase for transactional shared state between concurrent agents.
- **Weakness:** the value is philosophically interesting but operationally soft, and "did the agents disagree correctly?" is unjudgeable in 3 minutes.

### 5.19 — `Escrow` · Multi-agent marketplace with transactional shared state
- Agents bid and settle against shared inventory; ACID prevents double-spend under concurrency. Technically pretty (a real serialisation demo), but a toy economy has no Real-World Impact.

### 5.20 — `Cartographer` · Living codebase map with staleness and confidence
- Maintains ownership, dependency, and intent maps that *decay* when unverified. The decay model is a nice memory mechanism.
- **Weakness:** drifts toward code Q&A; and the honest version needs a large real repository to be impressive.

### 5.21 — `Flakehound` · CI flake archaeologist with months-long memory
- Correlates intermittent test failures with infrastructure events across months — a window no context can hold. Highly feasible and instantly sympathetic to engineer judges.
- **Weakness:** modest impact ceiling; no meaningful ccloud or MCP role; not really autonomous (it observes but barely acts).

### 5.22 — `Curriculum` · Longitudinal learner model with real memory decay
- Spaced repetition implemented as literal confidence decay in the database. The mechanism is defensible, but the category is "AI tutor," which the brief effectively pre-rejects, and judge appeal is low.

### 5.23 — `Onboarder` · Org knowledge accretion for new hires
- **Rejected.** This is document Q&A with a nicer name. Buildable in a weekend with an off-the-shelf stack. Included to be explicit about what the filter kills.

### 5.24 — `Second Brain` / meeting assistant / support bot / travel planner / resume helper
- **Rejected as a class.** All are named in the brief's own list of overused concepts. Every one fails the §6 test. Listed so our rejection is on the record rather than implied.

---

# 6. The originality filter, applied

**The test:** *could someone build basically the same thing with ChatGPT plus a vector database in a weekend?*

| Idea | Weekend-clonable? | Verdict |
|---|---|---|
| 5.23 Onboarder, 5.24 class | Yes, trivially | **Rejected** |
| 5.12 Obligor, 5.16 Manifest, 5.20 Cartographer, 5.22 Curriculum | Mostly yes — the differentiator is polish, not mechanism | **Rejected / demoted** |
| 5.4 Watchpost, 5.10 Culvert, 5.11 Attestor | Not quite, but a plausible imitation could be | **Demoted** |
| 5.2, 5.3, 5.5, 5.6, 5.7, 5.8, 5.18, 5.19 | No — each needs transactions, leases, or time travel | **Survive** |
| 5.1 Engram | No. The core behaviours (exactly-once resume across death, procedures that earn confidence from measured outcomes, belief-state time travel, an agent that refuses to act because a backup is stale) are *not implementable* on a vector store plus an LLM | **Survives, wins** |

Engram's behaviours are also **not fakeable in a demo**, which is a subtler advantage: the interesting behaviour only appears *across a failure* or *across time*. A judge watching cannot mistake it for prompt engineering.

Cross-check against the brief's list of favoured properties — Engram exhibits: long-lived autonomous agent ✅ · temporal memory ✅ · event-sourced memory ✅ · transactional agent state ✅ · self-improving workflows ✅ · infrastructure-aware agent ✅ · recovery after failure ✅ · memory consistency ✅ · conflict resolution ✅ · durable execution ✅ · provenance-aware memory ✅ · memory confidence ✅ · consolidation ✅ · forgetting/archival ✅ · autonomous operational decisions ✅. Not exhibited, and we will say so rather than fake it: multi-agent memory (unnecessary — see §11.4), cross-region agents and agent handoffs (documented stretch goal).

---

# 7. The memory taxonomy

Eight memory classes, each with a distinct **write trigger**, **retrieval strategy**, and **retention rule**. The design principle: *one cluster, many memory types* — the thing a bolt-on vector store cannot do.

| Class | Contents | Write trigger | Retrieval | Retention |
|---|---|---|---|---|
| **Working** | Current task state, active hypothesis, scratch reasoning | Every graph-node transition | By `task_id`, exact | Row-Level TTL, 7 days |
| **Episodic** | What happened during one past incident: timeline, tools, outcome | On task terminal state | Scoped ANN on episode summary + recency | 180 days, then summary-only |
| **Semantic** | Facts and external expertise, incl. vendored CockroachDB Agent Skills | Ingest / on skills version bump | Scoped ANN, filtered by `class` | Superseded on version change |
| **Entity** | Durable model of each cluster, database, table, query fingerprint | Observation sweep, MCP + ccloud refresh | Exact by key; joins | Indefinite; versioned attributes |
| **Procedural** | Reusable remediation recipes with attempts, successes, confidence | On measured outcome | Scoped ANN + confidence-weighted rank | Indefinite; confidence decays |
| **Transactional** | Task state machine, leases, approvals, idempotency ledger | Same transaction as the side effect | Exact, `FOR UPDATE` | Indefinite (legal/ops record) |
| **Operational** | Metrics snapshots, incident timelines, deployment and version events | Sweep + CloudWatch pull | Time-range + entity | Raw 30 days (TTL), aggregates kept |
| **Audit** | Why a decision was made, which memories and skills informed it, who approved | Every decision and tool call | Exact + time travel | Indefinite, append-only |

**Why one database instead of the usual four.** The conventional stack is Postgres (state) + Redis (working memory/leases) + Pinecone (vectors) + S3 (artifacts), and it has a specific, under-discussed failure mode: **there is no transaction spanning "the memory that justified an action" and "the record that the action happened."** A crash in that window produces an agent that repeats irreversible work or loses its justification for it. CockroachDB collapses seven of the eight classes into one transactional domain (only artifacts go to S3, by design). That is the architectural argument, and it is a correctness argument rather than a convenience one.

---

# 8. Critical scoring

**Weights.** The five Devpost criteria are equally weighted, so they get 15% each (75% total); Demo Quality and Hackathon Feasibility get 10% each because they gate whether the other five can be *shown* in 17 days; Judge Appeal gets 5% as a tiebreak for fit with this specific panel.

`Score = 0.15·(Memory + Technical + Creativity + Impact + Production) + 0.10·Demo + 0.10·Feasibility + 0.05·JudgeAppeal`

Scores are deliberately spread. If everything scored 7–9 the exercise would be worthless.

| # | Idea | Mem | Tech | Crea | Impact | Prod | Demo | Feas | Judge | **Total** |
|---|---|---|---|---|---|---|---|---|---|---|
| 5.1 | **Engram** | 9 | 9 | 7 | 7 | 9 | 9 | 7 | 10 | **8.25** |
| 5.6 | Provenance | 9 | 8 | 9 | 6 | 8 | 6 | 7 | 8 | **7.70** |
| 5.2 | Ratchet | 9 | 8 | 8 | 8 | 8 | 5 | 5 | 8 | **7.55** |
| 5.3 | Casefile | 8 | 7 | 6 | 9 | 9 | 7 | 6 | 8 | **7.55** |
| 5.5 | Followthesun | 8 | 8 | 8 | 6 | 7 | 9 | 5 | 9 | **7.40** |
| 5.7 | Quartermaster | 8 | 8 | 8 | 6 | 9 | 6 | 6 | 7 | **7.40** |
| 5.8 | Deadman | 7 | 8 | 6 | 7 | 9 | 7 | 7 | 7 | **7.30** |
| 5.4 | Watchpost | 8 | 7 | 5 | 8 | 8 | 7 | 6 | 7 | **7.05** |
| 5.10 | Culvert | 8 | 7 | 6 | 8 | 7 | 7 | 6 | 7 | **7.05** |
| 5.9 | Thermostat | 8 | 7 | 7 | 8 | 7 | 5 | 6 | 7 | **7.00** |
| 5.18 | Quorum | 8 | 7 | 8 | 6 | 6 | 7 | 6 | 7 | **6.90** |
| 5.11 | Attestor | 7 | 6 | 6 | 8 | 9 | 5 | 6 | 7 | **6.85** |
| 5.15 | Relay | 8 | 7 | 7 | 7 | 6 | 8 | 4 | 7 | **6.80** |
| 5.19 | Escrow | 7 | 8 | 8 | 5 | 7 | 7 | 5 | 7 | **6.80** |
| 5.13 | Priorauth | 7 | 6 | 6 | 9 | 7 | 6 | 5 | 6 | **6.65** |
| 5.20 | Cartographer | 8 | 6 | 6 | 7 | 6 | 6 | 6 | 6 | **6.45** |
| 5.21 | Flakehound | 7 | 6 | 6 | 6 | 6 | 6 | 8 | 6 | **6.35** |
| 5.17 | Bench | 8 | 6 | 8 | 6 | 5 | 5 | 5 | 6 | **6.25** |
| 5.14 | Bedside | 7 | 5 | 6 | 8 | 6 | 6 | 4 | 5 | **6.05** |
| 5.16 | Manifest | 7 | 6 | 5 | 7 | 6 | 6 | 5 | 5 | **6.00** |
| 5.12 | Obligor | 6 | 5 | 5 | 7 | 6 | 6 | 7 | 5 | **5.90** |
| 5.22 | Curriculum | 7 | 5 | 4 | 6 | 5 | 6 | 7 | 4 | **5.55** |
| 5.23 | Onboarder | 4 | 3 | 2 | 5 | 4 | 5 | 8 | 3 | **4.15** |
| 5.24 | Generic-assistant class | 3 | 3 | 1 | 4 | 3 | 5 | 9 | 2 | **3.90** |

**Honest notes on our own top score.** Engram is *not* a 10 on Creativity — the "AI for ops" domain is well-trodden and we should not pretend otherwise; its creativity is in the memory mechanism, not the premise. It is *not* a 10 on Impact — the immediate audience is teams operating distributed SQL, which is narrower than a fintech or healthcare play. Its 7 on Feasibility carries the project's real risk: the demo depends on reliably manufacturing a convincing performance regression. Its 10 on Judge Appeal is the one place it is unambiguously best in class, and that is worth only 5%.

---

# 9. Shortlist — the top 5

They beat the rest on one shared property: **each one breaks if you remove the database, and breaks in a way you can film.** Everything below them either survives on a vector store (5.10, 5.12, 5.16, 5.20), cannot be demonstrated inside 3 minutes (5.9, 5.11, 5.15), or is not really autonomous (5.21, 5.23, 5.24).

### 1. Engram (8.25)
- **Competitive advantage:** the only candidate where all four CockroachDB tools are required by the product rather than added to it. Two independent "wow" beats instead of one.
- **Memorable because:** an agent that gets visibly faster, and an agent you can kill on camera.
- **Why CockroachDB is uniquely relevant:** decision-and-side-effect atomicity, transactional consistency between vectors and the rows they describe, `AS OF SYSTEM TIME` belief replay, Row-Level TTL as the forgetting mechanism, `FOR UPDATE` leases. No bolt-on vector store offers any of these.
- **Why AWS is relevant:** Bedrock for reasoning + embeddings; Fargate because the agent is long-lived *and because `stop-task` is the demo*; EventBridge gives it a heartbeat; CloudWatch is where "did the fix work?" is measured.
- **Difficulty / demo strength:** high / very high.
- **Judge concerns → answers:** *"Is this only for DBAs?"* → institutional operational memory, with databases as vertical one; the memory engine is domain-agnostic and we ship it as a separate package. *"Would you let an agent touch prod?"* → §16's blast-radius table: it is structurally incapable of destructive action; humans approve every mutation. *"Is the improvement real or scripted?"* → the procedure row's `attempts`/`successes`/`confidence` are visibly incremented in the database during the demo, and a judge can query them over MCP themselves.

### 2. Provenance (7.70)
- **Advantage:** the most original mechanism in the pool — replaying an agent's *belief state* via time travel.
- **Concern → answer:** market barely exists; and it is uncomfortably close to being a database-vendor feature rather than an application. **Decision: harvest the mechanism, not the project** (§12.7).

### 3. Ratchet (7.55)
- **Advantage:** the most rigorous "memory cannot fit in context" argument, and the Agent Skills migration domain fits perfectly.
- **Concern → answer:** the payoff is measured in days. No 3-minute video does it justice without time compression that a judge will correctly discount. **Fatal for this deadline.**

### 4. Casefile (7.55)
- **Advantage:** best Real-World Impact and the most visceral exactly-once story ("never file the same SAR twice").
- **Concern → answer:** ccloud and Skills degrade into evidence rather than action, capping Technological Implementation; and synthetic financial data invites scepticism we cannot rebut.

### 5. Followthesun (7.40)
- **Advantage:** the best single visual in the entire pool — losing a region and continuing mid-incident from another continent.
- **Concern → answer:** free-tier multi-region plus a credible region-failure simulation is too much for 17 days. **Decision: adopt as Engram's documented stretch goal**, honestly labelled as not-yet-built.

---

# 10. The decision

| Category | Winner | Why |
|---|---|---|
| **Best overall** | **Engram (5.1)** | Highest weighted score; only candidate strong on all five equally-weighted criteria simultaneously; two independent demo climaxes; all four CockroachDB tools genuinely required |
| **Most technically impressive** | Ratchet (5.2) | Distributed, multi-day, exactly-once change management across a service fleet is the hardest engineering in the pool |
| **Most original** | Provenance (5.6) | Time-travel replay of agent cognition is a mechanism nobody else will show |
| **Most feasible high-impact** | Deadman (5.8) | Could ship in a week with a 9/10 production-readiness story — but it is middleware, not an agent |

**Final recommendation: build Engram, and deliberately absorb the best mechanism from each runner-up.**

- From **Provenance** → `AS OF SYSTEM TIME` belief-state replay, as a first-class audit feature (§12.7).
- From **Deadman** → the idempotency ledger and lease discipline (§12.6).
- From **Ratchet** → procedures as versioned, reusable recipes with success statistics (§12.5).
- From **Followthesun** → multi-region memory, documented as the next step and *labelled as not built*.

This is the whole strategy in one line: **one project, four mechanisms, five criteria covered.** Absorbing mechanisms rather than building four projects is what makes the 17-day deadline survivable.

---

# 11. Engram — product & agent specification

## 11.1 Product

- **Name:** Engram. *(An engram is the physical trace a memory leaves behind — the name is the thesis.)*
- **Tagline:** *The database reliability engineer that never forgets an incident.*
- **Elevator pitch:** Engram watches your production CockroachDB clusters, diagnoses regressions, and remediates them behind human approval — but the point isn't the diagnosis, it's that every incident is permanently converted into a scored, reusable procedure. The second time a problem of a given shape appears, Engram recognises it in seconds and tells you what worked last time, how confident it is, and why. Its memory, its execution checkpoints, and its idempotency ledger all live in one CockroachDB cluster, so you can kill it mid-remediation and it will resume without redoing the work.
- **Personas:**
  - *Priya, platform engineer, 40-person startup, on call 1 week in 4.* No dedicated DBRE. Her pain is 3am pages about problems the team already solved in March and forgot.
  - *Marcus, staff DBRE, 400-person company, 30 clusters.* His pain is being the single point of institutional knowledge, and knowing his team will regress the moment he goes on leave.
- **Pain point:** operational knowledge lives in Slack threads, individual memory, and closed tickets. It is not queryable, not scored, not attached to the systems it concerns, and it leaves when people do. The cost is repeated diagnosis of already-solved problems and, worse, repeated *bad* fixes nobody recorded as bad.
- **Current alternatives and why they fail:** APM and DB monitoring (Datadog, cluster console) detect but do not remember or act — they page a human whose context is empty. Runbooks rot and are not scoped to actual entities. LLM chat assistants have no durable memory, no idea what happened last quarter, and no ability to act safely. Generic AIOps ties remediation to static rules, so it never learns from outcomes.
- **Unique insight:** *the scarce resource in operations is not intelligence, it is memory.* A competent engineer with your incident history beats a brilliant one without it. So the correct product is not a smarter model — it is a memory system with an agent attached, and it must be transactional because its memories authorise irreversible actions.
- **User workflow:** connect a cluster (read-only service account) → Engram builds entity memory and a baseline → an anomaly appears → Engram posts a diagnosis with recalled precedent, a confidence score, and a proposed change → human approves, edits, or rejects in the dashboard → Engram verifies backup freshness, applies, and measures → outcome is written back and the procedure's confidence moves. Rejections are memories too: a rejected proposal with the human's reason lowers that procedure's confidence.
- **Key features:** typed memory system with lifecycle · scoped semantic recall over procedures, episodes, query shapes and vendored skills · confidence that is earned from measured outcomes · exactly-once remediation with an idempotency ledger · lease-based single-writer safety · approval gates with recorded approver and expiry · pre-flight backup gate · belief-state time travel for audit · natural-language interrogation of the agent's own memory over MCP.
- **Future potential:** multi-region memory domiciling; a domain-agnostic `engram-memory` package usable by any LangGraph agent; extension to any PostgreSQL-compatible target; cross-org anonymised procedure sharing (the network effect: every operator's incidents make everyone's agent smarter).

## 11.2 Agent architecture

**One agent, five graph nodes.** Not a multi-agent system — see §11.4.

| Node | Responsibility | Tools |
|---|---|---|
| **Observe** | Sweep target clusters; snapshot key metrics and running queries; detect deviation from remembered baseline; open a task | MCP `show_running_queries`/`select_query`, ccloud `cluster info`, CloudWatch `GetMetricData` |
| **Recall** | Fingerprint the anomaly; scoped ANN over query shapes, episodes, procedures, skills; assemble a ranked, cited context bundle | Memory data plane (psycopg3), Bedrock embeddings |
| **Reason** | Form a hypothesis; **falsify it** with `explain_query`; produce a typed `Proposal` (change, expected effect, risk, confidence, citations) | MCP `explain_query`/`get_table_schema`, Bedrock Claude Sonnet 5 |
| **Gate** | Evaluate policy: is this change class auto-approvable? Is a backup fresh enough? Does a lease exist? If human approval is needed, block and notify | ccloud `cluster backup list`, policy engine, approvals table |
| **Act & Measure** | Apply exactly-once under lease and idempotency key; wait; re-measure; write the outcome; update procedure confidence; consolidate the episode | Mutation role (separate SQL identity), CloudWatch, S3 |

- **Triggers:** EventBridge every 5 minutes (observation sweep); EventBridge hourly (consolidation) and nightly (decay) via Lambda; webhook ingest for external alerts; manual "run scenario" from the dashboard (this is the judge-facing entry point).
- **Planning loop:** hypothesis-first, not plan-first. Recall produces candidate procedures ranked by `similarity × confidence × recency`; Reason must either adopt one with a citation or explain why none fit. **An agent that has to justify ignoring its own memory is a better-behaved agent** — and it makes the memory visibly load-bearing in the trace.
- **Execution loop:** every node transition commits working memory and a LangGraph checkpoint in the same transaction as its side effects. Side effects go through the idempotency ledger. Nothing is ever applied without a valid lease.
- **Reflection loop:** post-measurement, the agent scores its own outcome (`improved` / `no_change` / `regressed`), writes an episode, and adjusts the procedure's confidence via Wilson lower bound on `successes/attempts` — so a procedure with 1/1 does not outrank one with 47/50. **This is the mechanism that makes "it gets better" a real property rather than a claim.**
- **Failure recovery:** see §13 in full.
- **Human approval boundaries:** *auto-approved* — read-only diagnosis, memory writes, metric collection. *Human approval required* — any DDL, any cluster setting, any control-plane mutation. *Never permitted, structurally* — `DROP`, `TRUNCATE`, `DELETE` without a bounded predicate, credential changes, network allowlist changes. Enforced by SQL role grants and the ccloud service-account role, not by prompt instructions (§16).

## 11.3 Interfaces

- **Agent core:** Python 3.12, LangGraph, `langchain-cockroachdb` (`AsyncCockroachDBSaver`, `CockroachDBEngine`), psycopg3 async pool, boto3 for Bedrock/CloudWatch/S3, `mcp` client for the managed server.
- **Dashboard:** Next.js (App Router) + Tailwind + shadcn/ui. Panels: live task timeline with checkpoint markers · **Memory Inspector** (retrieved memories with similarity and confidence, so recall is visible rather than asserted) · procedure ledger with attempts/successes/confidence · approval queue · audit stream · a **"Kill the agent"** button. Reads via SSE from a Next.js route handler over a read-only SQL role.
- **Repo layout:** `agent/` (Python) · `memory/` (schema, migrations, the reusable memory package) · `dashboard/` (Next.js) · `infra/` (Terraform/CDK) · `scenarios/` (the incident simulator) · `docs/` (architecture diagram, ADRs, tool feedback) · `LICENSE` (Apache-2.0).

## 11.4 On multi-agent architecture — explicitly unnecessary

We are not building multiple agents, and we will say so in the README. The work here is sequential and shares one memory; splitting it into "Observer Agent," "Diagnostic Agent," and "Remediation Agent" would add inter-agent messaging, distributed failure modes, and prompt overhead in exchange for a more impressive-looking diagram and worse reliability. **The graph nodes already provide the separation of concerns that agent boundaries would; what they don't add is a new class of bug.**

Where genuine concurrency exists — several tasks across several clusters at once — we handle it with the mechanism a database gives us: `agent_leases` with `SELECT … FOR UPDATE`, so N instances can run safely without any of them talking to each other. **Coordination through transactional shared state instead of agent-to-agent messaging is a deliberate architectural position, and it is more on-thesis for this hackathon than a multi-agent diagram would be.**

---

# 12. Memory architecture

The deepest section, and the one that wins criterion 1.

## 12.1 What counts as "memory"

Anything the agent must know that outlives the process that learned it. That includes things most designs leave in RAM or in the prompt: the current hypothesis, whether a side effect has already been performed, which lease is held, which skill version informed a decision, and what the agent believed at 03:14:22.

## 12.2 Schema

Illustrative DDL; column lists are trimmed to what matters.

```sql
SET CLUSTER SETTING feature.vector_index.enabled = true;

-- ENTITY MEMORY: durable model of the world the agent operates on
CREATE TABLE entities (
  entity_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        UUID NOT NULL,
  kind          STRING NOT NULL,           -- cluster | database | table | index | query_shape
  external_key  STRING NOT NULL,           -- cluster id, fully-qualified table name, fingerprint
  attributes    JSONB NOT NULL,            -- regions, node count, crdb version, row estimate
  first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_verified TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (org_id, kind, external_key),
  INDEX (org_id, kind, last_verified DESC)
);

-- TRANSACTIONAL MEMORY: the task state machine
CREATE TABLE tasks (
  task_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       UUID NOT NULL,
  cluster_id   STRING NOT NULL,
  kind         STRING NOT NULL,            -- diagnose | remediate | verify
  status       STRING NOT NULL,            -- pending|running|awaiting_approval|applying|measuring|done|failed|blocked
  trigger      JSONB NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (org_id, status, created_at DESC)
);

-- TRANSACTIONAL MEMORY: single-writer guarantee across agent instances
CREATE TABLE agent_leases (
  task_id     UUID PRIMARY KEY REFERENCES tasks(task_id),
  holder_id   STRING NOT NULL,             -- ECS task ARN
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  fence_token INT8 NOT NULL DEFAULT 1,     -- monotonic; stale holders are rejected
  INDEX (expires_at)
);

-- WORKING MEMORY: short-lived, forgotten by design
CREATE TABLE working_memory (
  task_id    UUID NOT NULL REFERENCES tasks(task_id),
  step_no    INT NOT NULL,
  node       STRING NOT NULL,
  state      JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, step_no)
) WITH (ttl_expiration_expression = $$created_at + INTERVAL '7 days'$$);

-- EPISODIC + SEMANTIC + PROCEDURAL text, unified, with scoped vector recall
CREATE TABLE memory_items (
  memory_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       UUID NOT NULL,
  scope_id     STRING NOT NULL,            -- vector index prefix: org, or org:cluster
  class        STRING NOT NULL,            -- episode | skill | fact | query_shape | procedure_desc
  entity_id    UUID REFERENCES entities(entity_id),
  content      STRING NOT NULL,
  summary      STRING,
  embedding    VECTOR(1024),               -- Titan Text Embeddings V2
  confidence   FLOAT NOT NULL DEFAULT 0.5,
  provenance   JSONB NOT NULL,             -- task_id, tool_call_ids, skill sha, model id
  supersedes   UUID REFERENCES memory_items(memory_id),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  use_count    INT NOT NULL DEFAULT 0,
  VECTOR INDEX mem_vec_idx (scope_id, embedding vector_cosine_ops)
      WITH (min_partition_size = 16, max_partition_size = 128),
  INDEX (org_id, class, created_at DESC),
  INDEX (entity_id, class)
);

-- PROCEDURAL MEMORY: recipes that earn confidence from measured outcomes
CREATE TABLE procedures (
  procedure_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        UUID NOT NULL,
  kind          STRING NOT NULL,           -- add_index | rewrite_query | adjust_setting | escalate
  title         STRING NOT NULL,
  recipe        JSONB NOT NULL,            -- parameterised, typed; never free-form SQL
  preconditions JSONB NOT NULL,
  attempts      INT NOT NULL DEFAULT 0,
  successes     INT NOT NULL DEFAULT 0,
  confidence    FLOAT NOT NULL DEFAULT 0.0,-- Wilson lower bound, time-decayed
  derived_from  UUID[] ,                   -- episode memory_ids
  skill_shas    STRING[],                  -- provenance into cockroachdb-skills
  status        STRING NOT NULL DEFAULT 'active', -- active | stale | retired
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (org_id, kind, status, confidence DESC)
);

-- AUDIT MEMORY: append-only, and the reason a human would grant write access
CREATE TABLE decisions (
  decision_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       UUID NOT NULL REFERENCES tasks(task_id),
  hypothesis    STRING NOT NULL,
  proposal      JSONB NOT NULL,
  cited_memories UUID[] NOT NULL,          -- exactly what informed this
  cited_skills  STRING[] NOT NULL,
  procedure_id  UUID REFERENCES procedures(procedure_id),
  confidence    FLOAT NOT NULL,
  model_id      STRING NOT NULL,
  outcome       STRING,                    -- improved | no_change | regressed | blocked | rejected
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (task_id, created_at)
);

CREATE TABLE tool_calls (
  call_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id     UUID NOT NULL REFERENCES tasks(task_id),
  surface     STRING NOT NULL,             -- mcp | ccloud | sql | bedrock | cloudwatch | s3
  tool        STRING NOT NULL,
  args_redacted JSONB NOT NULL,
  result_hash STRING,
  artifact_uri STRING,                     -- S3 for anything large
  latency_ms  INT,
  error_code  STRING,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  INDEX (task_id, created_at), INDEX (surface, created_at DESC)
);

-- TRANSACTIONAL MEMORY: the exactly-once guarantee
CREATE TABLE remediation_actions (
  action_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id         UUID NOT NULL REFERENCES tasks(task_id),
  idempotency_key STRING NOT NULL,         -- sha256(cluster_id || normalised_change)
  change          JSONB NOT NULL,
  state           STRING NOT NULL,         -- intended | applied | verified | failed | rolled_back
  fence_token     INT8 NOT NULL,
  applied_at      TIMESTAMPTZ,
  UNIQUE (idempotency_key)                 -- the single constraint that makes double-apply impossible
);

CREATE TABLE approvals (
  approval_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id     UUID NOT NULL REFERENCES tasks(task_id),
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at  TIMESTAMPTZ,
  decided_by  STRING,
  verdict     STRING,                      -- approved | rejected
  reason      STRING,                      -- becomes a memory: rejections lower confidence
  expires_at  TIMESTAMPTZ NOT NULL
);

-- OPERATIONAL MEMORY: raw metrics forgotten on a schedule, aggregates retained
CREATE TABLE observations (
  observation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id   UUID NOT NULL REFERENCES entities(entity_id),
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metrics     JSONB NOT NULL,
  INDEX (entity_id, observed_at DESC)
) WITH (ttl_expiration_expression = $$observed_at + INTERVAL '30 days'$$);
```

Plus the LangGraph checkpoint tables created by `AsyncCockroachDBSaver`, which we enable Row-Level TTL on so abandoned traces expire without a cleanup job.

**Why these indexes.** Vector indexes carry `scope_id` as a prefix because C-SPANN requires equality-constrained prefixes and because tenant-scoped recall must not be a post-filter. `procedures` is indexed `(org_id, kind, status, confidence DESC)` so the hot query — "best active recipe of this kind" — is an index scan with no sort. `remediation_actions.idempotency_key` is `UNIQUE` because that constraint, not application logic, is what makes double-application impossible.

## 12.3 Memory creation

Writes are typed and triggered, never "log everything and hope":

| Trigger | Writes |
|---|---|
| Observation sweep | `observations` (TTL'd), `entities.last_verified`, new entities on discovery |
| Node transition | `working_memory` + LangGraph checkpoint, same transaction |
| Tool invocation | `tool_calls` (args redacted, large results to S3 with hash in-row) |
| Hypothesis formed | `decisions` with `cited_memories` and `cited_skills` populated **before** action |
| Action intended | `remediation_actions` state `intended` + fence token, **in the same transaction as the decision** |
| Action applied | state → `applied`, `applied_at` set, atomically with the audit row |
| Measurement complete | `decisions.outcome`, episode into `memory_items`, `procedures` counters and confidence |
| Human verdict | `approvals`; a rejection with reason writes a negative-signal memory |

The load-bearing detail: **decision, intent, and side effect are one transaction.** That is the concrete meaning of "memory is not an afterthought."

## 12.4 Retrieval and ranking

Retrieval is hybrid and deliberately not pure cosine:

```sql
-- procedural recall, scoped and confidence-weighted
SELECT p.procedure_id, p.title, p.recipe, p.confidence,
       m.embedding <=> $2 AS distance
FROM memory_items m
JOIN procedures p ON p.procedure_id = m.entity_id::UUID
WHERE m.scope_id = $1          -- equality-constrained prefix: index is used
  AND m.class = 'procedure_desc'
  AND p.status = 'active'
ORDER BY m.embedding <=> $2
LIMIT 20;
```

The candidate set is then re-ranked in the agent:

`score = 0.45·similarity + 0.30·confidence + 0.15·recency + 0.10·entity_affinity`

with a hard filter dropping `confidence < 0.15` and `status <> 'active'`. `entity_affinity` boosts procedures previously successful on *this* cluster or a table of similar shape. Recency is an exponential decay over `last_used_at`, so knowledge that stopped being used stops being retrieved before it is formally retired.

Two retrieval modes coexist:
- **Semantic** — the ANN query above, with `SET vector_search_beam_size` raised to 64 for high-stakes remediation recall and left at the default for routine sweeps. A recall/latency table at beam ∈ {8, 32, 64} goes in the README.
- **Temporal** — "what happened on this cluster in the last 30 days" and "what did we believe at time T," the second via `AS OF SYSTEM TIME` (§12.7). Purely relational, no vectors, which is exactly why unifying both in one database matters.

## 12.5 Consolidation, decay, and forgetting

An hourly Lambda (`EventBridge` → `consolidator`) performs the work that turns experience into competence:

1. **Episode summarisation.** Closed tasks are compressed into one episode memory: what was observed, what was tried, what happened. Raw working memory is left to expire by TTL.
2. **Procedure induction.** When ≥3 episodes cluster tightly in embedding space and share an outcome, a candidate procedure is proposed. **Induction requires human confirmation the first time** — an agent that silently invents its own operating procedures is not a production system.
3. **Deduplication.** Near-duplicates (cosine < 0.05 within the same scope and class) are merged; the survivor inherits `use_count` and the loser is linked via `supersedes` rather than deleted, so provenance chains stay intact.
4. **Confidence update.** `confidence = wilson_lower_bound(successes, attempts, 0.95) × time_decay(updated_at)`. A recipe that worked once is not trusted like one that worked 47 times, and one that has not been exercised in months drifts down.
5. **Contradiction handling.** A procedure whose last two applications produced `regressed` is set `stale`, excluded from recall, and surfaced in the dashboard with its full history. Contradiction is a *transition*, not a delete, so the audit trail survives.
6. **Forgetting.** Row-Level TTL expires working memory (7d) and raw observations (30d) with no cron job of ours. Episodes older than 180 days keep summaries and shed detail. `retired` procedures stay for audit but never surface. **Forgetting is a designed feature with a stated retention policy, not neglect** — and it is the half of memory design almost nobody will build.

## 12.6 Conflict resolution and exactly-once

Three independent mechanisms, deliberately layered because any one of them can be defeated:

1. **Lease with fencing.** Acquisition is `SELECT … FROM agent_leases WHERE task_id = $1 FOR UPDATE`, then a bump of `fence_token`. A resurrected zombie holding an old token is rejected at write time. This is the standard defence against the failure mode where a "dead" process is only sleeping.
2. **Idempotency key.** `sha256(cluster_id || normalised_change)` with a `UNIQUE` constraint. A retry after an ambiguous crash hits a uniqueness violation, which the agent interprets as "already intended" and reconciles against the target's actual state rather than reapplying.
3. **Reconcile before act.** Before applying, the agent re-reads the target's real schema over MCP. If the index already exists, the action is marked `applied` without a mutation. The database is the source of truth about the database, not our memory of it.

## 12.7 Provenance, confidence, and time travel

Every decision row records the memory IDs, skill SHAs, model ID, and tool-call IDs that produced it. That makes two questions answerable in SQL, which is unusual for an agent:

- *"Why did the agent do that?"* → join `decisions` → `memory_items` + `procedures` + `tool_calls`.
- *"What did the agent believe at 03:14:22, before it learned it was wrong?"* →

```sql
SELECT m.class, m.summary, m.confidence
FROM memory_items AS OF SYSTEM TIME '2026-08-14 03:14:22'
  AS m
WHERE m.scope_id = $1 AND m.class = 'procedure_desc'
ORDER BY m.confidence DESC;
```

That second query is the one to put on screen in the video. **Reconstructing an agent's past belief state — not its logs, its beliefs — is a capability the database gives us for free and one that few competitors will even know is possible.**

## 12.8 Why CockroachDB's distributed architecture matters here

Stated honestly, without inflation:

1. **Atomicity across memory and action** — the correctness argument of §1.4(d). This is the real one.
2. **Vector/relational consistency** — a procedure's embedding, its confidence, and the audit row proving it worked commit together. No window in which the agent retrieves a memory the operational store has already invalidated.
3. **Survivability as an availability floor** — the memory layer has no maintenance window, so a rolling upgrade of the memory tier does not stop the agent. This is the brief's own thesis and we should demonstrate rather than repeat it.
4. **Time travel as audit** — MVCC gives belief replay for free.
5. **TTL as a lifecycle primitive** — forgetting is declared in DDL, not implemented as a fragile cron job.
6. **Scale headroom, claimed carefully.** We will not pretend our demo runs at billions of vectors. We will state what we measured (row counts, p50/p99 recall latency at our scale) and cite C-SPANN's design for the growth path. **Judges from a database company will trust a measured small number far more than an unmeasured large one.**

---

# 13. Failure recovery — the killer feature

The brief says an agent whose memory goes offline "doesn't degrade gracefully, it stops." We demonstrate the positive form of that claim: **an agent whose *process* dies degrades gracefully, because its memory did not.**

## 13.1 What we kill, and why

**We kill the agent, not the database.** Three reasons, and the first is the one that matters:

1. `ccloud cluster disruption` requires an Advanced-tier cluster and is unavailable on the free Basic tier. Building the headline demo on it would be a schedule bomb.
2. Killing the database would demonstrate *CockroachDB's* resilience, which Cockroach Labs already knows about. Killing the agent demonstrates **our architecture's** resilience, which is what is being judged.
3. `aws ecs stop-task` is one command, instantaneous, unfakeable, and the audience can see the container ID disappear.

## 13.2 The sequence, timed

| t | Event | Visible evidence |
|---|---|---|
| 0s | Task opens. Anomaly detected on `orders` — a query regressed from 12ms to 2.4s | Dashboard task appears; checkpoint #1 |
| 6s | Recall returns a procedure with confidence 0.31 and a cited episode | **Memory Inspector shows the retrieved rows, similarity and confidence** |
| 11s | `explain_query` confirms a full scan; proposal written; `remediation_actions` row created in state `intended`, atomically with the decision | Checkpoints #2–#4; audit stream |
| 14s | Human approves in the dashboard | `approvals` row, approver recorded |
| 16s | Backup gate: `ccloud cluster backup list -o json` → newest backup 22 minutes old, within RPO → proceed | tool_call row, surface `ccloud` |
| **18s** | **`aws ecs stop-task` — the container is killed between `intended` and `applied`.** The worst possible moment, chosen deliberately | Container vanishes; dashboard shows the agent offline; task frozen mid-flight |
| 24s | Fargate starts a replacement task | New task ARN appears |
| 27s | New instance scans for orphaned work, finds the expired lease, acquires it, **bumps the fence token**, and rehydrates from the LangGraph checkpoint | Lease row shows the new holder and `fence_token = 2` |
| 29s | Reconcile-before-act: re-reads the target schema over MCP. The index does **not** exist, so the action is genuinely unfinished → apply once | tool_call row proving it checked |
| 33s | Applied. `remediation_actions` → `applied`. **One row, not two.** | The unique idempotency key is on screen |
| 45s | Re-measured: 2.4s → 14ms. Procedure confidence 0.31 → 0.58; attempts 3→4, successes 2→3 | Procedure ledger updates live |

**The line to say out loud while the container dies:** *"the agent just died between deciding to act and acting — the single worst moment for any automation. Watch what it does not do: it does not apply the change twice."*

## 13.3 The second beat — "it remembers"

Immediately after, trigger a *different* table with the same problem shape. Total time to remediation: **~8 seconds**, because Recall returns the now-confident procedure and Reason adopts it with a citation instead of re-deriving it. Put both task timelines side by side. The contrast — 45 seconds vs 8 seconds, and *why*, visible in the Memory Inspector — is the single most persuasive thing in the submission, and it is the literal demonstration of *"memory is the thing that makes an agent useful in production."*

## 13.4 The third beat — time travel (12 seconds, if the video has room)

Run the `AS OF SYSTEM TIME` query from §12.7 against the moment of death, showing confidence 0.31; then run it against now, showing 0.58. *"This is not a log. This is the agent's belief state, reconstructed from MVCC."*

## 13.5 Additional resilience, tested but not filmed

- **Bedrock throttling** → exponential backoff with jitter; task parks in `awaiting_retry` with state intact.
- **MCP timeout** (real: 20s ceiling) → tool adapter returns a typed timeout; the agent degrades to relational-only diagnosis and records reduced confidence rather than guessing.
- **Approval expiry** → `approvals.expires_at` lapses; the task moves to `blocked` and requires re-proposal. An agent must not act on stale consent.
- **Split brain** → two instances given the same task deliberately; the second is fenced out. Include this as a test in CI so the claim is verifiable, not asserted.

## 13.6 Make it self-serve

The dashboard's **"Kill the agent"** button calls ECS StopTask on our own task. A judge who clones the repo — or just opens the demo URL — can perform the headline demo themselves in ten seconds without reading a word of our documentation. **Very few submissions will let a judge reproduce the wow moment personally.** That is worth more than a better video.

---

# 14. CockroachDB integration plan

All four tools, with the checkbox/meaningful boundary stated explicitly so the judges' question — *"what did the agent actually do with them?"* — is answered before they ask.

## 14.1 Distributed Vector Indexing

- **Vectors stored:** three classes in `memory_items`, all `VECTOR(1024)` from Titan Text Embeddings V2 — query-shape fingerprints, episode summaries, procedure descriptions — plus a fourth class holding chunked CockroachDB Agent Skills.
- **Index:** `VECTOR INDEX mem_vec_idx (scope_id, embedding vector_cosine_ops) WITH (min_partition_size=16, max_partition_size=128)`.
- **Queries:** scoped ANN with `ORDER BY embedding <=> $1 LIMIT 20`, `scope_id` equality-constrained so the index is actually used, then hybrid re-ranking (§12.4). Beam size raised to 64 for remediation recall, default for sweeps.
- **Why distributed vector search specifically:** because the embedding, the confidence counter, and the audit row commit in one transaction. Consistency between the vector and the operational row it describes is the requirement; ANN speed is secondary. We will state that in exactly those terms.
- **What we will report honestly:** our corpus is thousands of vectors, not billions. We publish measured p50/p99 recall latency at our scale and the beam-size trade-off table, and cite C-SPANN for the growth path rather than implying we tested it.

## 14.2 Managed MCP Server

- **Who connects:** (a) the agent's diagnosis node, at runtime, via the `mcp` Python client; (b) a human or judge in Claude Code, against the memory cluster.
- **What it queries:** `list_clusters`, `get_cluster`, `list_databases`, `list_tables`, `get_table_schema` (entity memory refresh, drift detection); `show_running_queries`, `show_statement` (live diagnosis and fingerprinting); `explain_query` (**hypothesis falsification and post-change plan verification**); `select_query` (bounded cardinality sampling).
- **Permissions:** service account with **Cluster Operator** role, `mcp:read` scope only, pinned to one cluster with the `mcp-cluster-id` header. `mcp:write` is never requested. The agent therefore *cannot* mutate through MCP — a structural guarantee, not a policy.
- **Handling the real limits:** the adapter enforces a 15-second client timeout inside the server's 20-second ceiling, requires explicit `LIMIT` on every `select_query`, and summarises results before they reach the model so the 10 KiB response cap never truncates mid-reasoning. **Naming these limits in the README and showing we designed around them is a stronger signal than any amount of throughput talk.**
- **The judge-facing use:** we ship a `docs/mcp-verification.md` with a config snippet and five natural-language questions a judge can ask our memory cluster directly (*"which procedures has this agent learned and how confident is it in each?"*). They verify our claims against the database with zero code from us.
- **Checkbox vs. meaningful:** configuring MCP in an IDE is a checkbox. `explain_query` used at runtime by the agent to try to *disprove its own hypothesis* is meaningful.

## 14.3 ccloud CLI

- **Operational actions the agent performs (all read-only):** `cluster backup list` (the pre-flight safety gate), `cluster info` + `cluster list` (entity memory: regions, tier, node count, CockroachDB version), `audit list` (post-mutation reconciliation against our own ledger).
- **The behaviour that sells it:** the agent **refuses to proceed** and writes a `blocked` decision when the newest backup is older than the configured RPO. An agent that declines to act because a safety precondition failed is the most production-mature behaviour we can show, and it takes about forty lines of code.
- **How dangerous actions are prevented:** the model never emits a command string. It chooses from an enum of allowlisted operations; the adapter builds `argv` itself, appends `-o json`, and validates the response against a JSON schema, mapping ccloud's documented error codes (permission-denied / not-found / rate-limited) to typed exceptions. The service account holds **Cluster Operator** only — read-only on the control plane. The one mutating call we use, `cluster metric-export cloudwatch enable`, is run once by a human at setup and is documented as such.
- **Named but not used, honestly:** `blackout-window` and `disruption` both need Advanced tier. We say so rather than implying coverage.

## 14.4 Agent Skills

- **Which skills:** performance & scaling (query plans, indexing), observability & diagnostics (what to measure), resilience & DR (backup and recovery posture), security & governance (least privilege), operations & lifecycle (version-aware advice).
- **How they improve behaviour:** vendored at a pinned git SHA, chunked, embedded, and stored as `class='skill'` memories. The diagnosis node retrieves the two or three semantically relevant skills per incident rather than stuffing all of them into context. Every decision records the skill SHAs that informed it; bumping the vendored version marks derived procedures `stale` for re-validation.
- **Why this is the strongest Skills story available:** it converts a documentation repository into **versioned, retrievable, provenance-tracked semantic memory**, which is precisely the distinction between semantic memory (external expertise) and episodic memory (the agent's own experience) that our §7 taxonomy claims. Most teams will run `npx skills add` and stop.

## 14.5 Summary for the submission form

> **Distributed Vector Indexing** — four classes of embedded memory in one `memory_items` table with a `(scope_id, embedding vector_cosine_ops)` C-SPANN index; the agent recalls prior incidents, reusable procedures, query-shape precedents, and CockroachDB skills through scoped ANN with confidence-weighted re-ranking, all transactionally consistent with the operational rows they describe.
> **Managed MCP Server** — the agent's read-only introspection organ at runtime: it refreshes its entity model of each target cluster, diagnoses live load, and uses `explain_query` to falsify its own hypothesis before proposing a change. Scoped to `mcp:read` on a Cluster-Operator service account, so it is structurally incapable of mutating through MCP.
> **ccloud CLI** — the agent's control-plane awareness: a pre-flight `cluster backup list` gate that blocks remediation when no backup is within RPO, `cluster info` for version-aware advice, and `audit list` reconciliation to detect any control-plane action outside its own ledger. Read-only service account; allowlisted, parameterised commands; `-o json` parsed against a schema.
> **Agent Skills** — vendored at a pinned SHA, embedded, and retrieved semantically at runtime; every decision cites the skill SHAs that informed it, and a version bump marks derived procedures stale for re-validation.

---

# 15. AWS architecture

```
                        ┌──────────────────────────────────────────┐
   Engineer / Judge ───▶│  Next.js dashboard (Vercel)              │
                        │  timeline · Memory Inspector · approvals  │
                        │  audit stream · [Kill the agent]          │
                        └───────┬──────────────────────┬───────────┘
                                │ SSE (read-only role) │ StopTask
                                ▼                      ▼
   EventBridge ──5 min──▶ ┌─────────────────────────────────────┐
   (sweep)                │  ECS Fargate service: Engram agent  │
   EventBridge ──1 hr──▶  │  LangGraph: Observe→Recall→Reason→  │
   (consolidate) ─┐       │            Gate→Act&Measure         │
   Alert webhook ─┤       └──┬────────┬────────┬────────┬───────┘
                  │          │        │        │        │
                  │      Bedrock   MCP     ccloud   CloudWatch
                  │   (Claude 5 +  (read-  (read-   (metrics,
                  │    Titan 1024) only)   only)     logs, traces)
                  │          │        │        │        │
                  ▼          │        ▼        ▼        │
        ┌──────────────┐     │   ┌──────────────────┐   │
        │   Lambda     │     │   │  TARGET CockroachDB │ │
        │ consolidator │     │   │  cluster(s)        │ │
        │ decay/backfill│    │   │  (the subject)     │ │
        └──────┬───────┘     │   └──────────────────┘   │
               │             │                          │
               ▼             ▼                          │
        ┌───────────────────────────────────────────┐    │
        │   MEMORY CockroachDB Cloud cluster        │◀───┘
        │   (the product)                           │  ccloud metric-export
        │   entities · tasks · leases · working_mem │  → CloudWatch
        │   memory_items + VECTOR INDEX · procedures│
        │   decisions · tool_calls · approvals      │
        │   remediation_actions · observations      │
        │   LangGraph checkpoints (TTL)             │
        └───────────────────────────────────────────┘
               ▲                        ▲
        S3 (EXPLAIN bundles,      Secrets Manager
        plan diffs, evidence)     (DSNs, API keys)
```

**The diagram's most important property:** CockroachDB appears **twice, in two clearly different roles** — the memory cluster (the product) and the target cluster (the subject). That single visual distinction communicates the entire architecture faster than any paragraph, and it forecloses the most likely judge misreading, which is that we simply pointed an agent at a database.

**Deployment.** Dashboard on Vercel (free, instant public URL, satisfies the demo-URL requirement without an ALB). Agent on Fargate in `us-east-1`, colocated with the memory cluster's primary region to keep memory-write latency low. Lambdas in the same region. One `infra/` IaC stack plus a `make demo-up` target so a judge can reproduce it.

---

# 16. Security & production readiness

This section exists to win 20% of the score, and it should be a page of the README, not an appendix.

## 16.1 Identities — four, not one

| Identity | Grants | Cannot |
|---|---|---|
| `engram_agent` (SQL, memory cluster) | CRUD on memory tables | `DROP`, `ALTER`, or touch any other database |
| `engram_reader` (SQL, memory cluster) | `SELECT` only; used by the dashboard BFF | Write anything, ever |
| `engram_mcp` (Cloud service account) | Cluster **Operator**, `mcp:read`, pinned cluster | Mutate anything on the control plane or through MCP |
| `engram_mutator` (SQL, target cluster) | `CREATE INDEX` on an allowlisted schema only | `DROP`, `TRUNCATE`, unbounded `DELETE`, role or grant changes |

**Blast radius, stated plainly:** full compromise of the agent's model — a perfect prompt injection, total loss of instruction-following — yields the ability to create an index on an allowlisted schema, after a human clicked approve, on a cluster with a verified recent backup. It cannot drop, truncate, exfiltrate through the control plane, or escalate. **This table is the first thing a database engineer will look for and the thing most submissions will not have.**

## 16.2 Controls

- **Secrets:** Secrets Manager, fetched by IAM task role at boot, never in env files or images. Rotation documented. CI secret-scanning on the repo.
- **Authorisation:** every memory query is scoped by `org_id` in the data layer, not in the prompt. Vector recall is prefix-scoped by `scope_id`, so cross-tenant leakage is prevented by the index rather than by a filter someone might forget.
- **Approval gates:** typed policy — change class × risk × confidence decides auto-approve vs. human. Approvals record approver, reason, and `expires_at`; expired consent is not consent.
- **Destructive-action safeguards:** an allowlist of change *kinds* with parameterised recipes. The model never emits raw SQL or shell strings; it selects a recipe and fills typed parameters that are validated before rendering. **This is the single most important safety decision in the project** — it makes a whole class of injection attack structurally impossible rather than merely discouraged.
- **Prompt-injection defence:** everything read from a target cluster (query text, table comments, error strings) and from external alerts is untrusted. It is wrapped in a quarantine envelope, never concatenated into the instruction section, stripped of imperative markup, and — critically — **retrieved memories are data, never instructions**. A memory that contains "ignore previous instructions" is rendered as a quoted string in a data block. We add an adversarial test with an injected table comment to CI, because a claim of injection resistance without a test is decoration.
- **Idempotency, retries, rate limits:** unique idempotency keys on side effects; exponential backoff with jitter on Bedrock and MCP; per-cluster action rate limits so a reasoning loop cannot become a mutation storm; a global circuit breaker that parks the agent after N consecutive failures instead of thrashing.
- **Observability:** structured JSON logs with `task_id`/`decision_id` correlation; OTel traces per graph node; custom CloudWatch metrics (`memory_recall_latency`, `recall_hit_rate`, `procedure_confidence_delta`, `blocked_by_backup_gate`, `exactly_once_conflicts_detected`) — note that these are *memory* metrics, which is what should be monitored if memory is the product. Target-cluster metrics arrive in the same account via `ccloud cluster metric-export cloudwatch`.
- **Backup/recovery:** the memory cluster's own managed backups are verified by a scheduled `ccloud cluster backup list` check, and we include a documented restore drill. **An agent whose memory is the product must be able to prove its memory is recoverable** — and it would be embarrassing to build a backup gate for other people's databases and not our own.
- **Demo-URL safety:** guest mode is read-only. The scripted scenario runs against a disposable sandbox cluster, never a target with real data. The "Kill the agent" button is rate-limited. No anonymous visitor can run DDL anywhere.

## 16.3 How this maps to "Product Readiness"

Judges ask: is it secure, observable, scalable; has the team thought about resilience, access control, and what happens when things go wrong? Our answers are all *demonstrable artifacts* rather than prose: the four-identity table and its enforcing grants; the blast-radius statement; the CI injection test; the fence-token split-brain test; the circuit breaker; the backup gate that visibly blocks a remediation in the demo; and memory-level metrics in CloudWatch. **We show a remediation being refused because a precondition failed. Nothing else we could film says "production" as economically.**

---

# 17. Observability & telemetry

*(`prompt.md` skips §17–§21 and §24; those sections are filled here with what its closing paragraph requires — the doc must also serve as roadmap, demo plan, and submission checklist.)*

| Layer | Instrument | Why it matters for judging |
|---|---|---|
| Agent behaviour | OTel span per graph node, attributes for `task_id`, `decision_id`, retrieved memory count, model + skill versions | Makes the reasoning loop inspectable rather than a black box |
| Memory health | `recall_hit_rate`, `memory_recall_latency_p99`, `vector_rows_total`, `stale_procedures_total`, `consolidation_lag_seconds` | These are the metrics you monitor **if memory is the product** — a distinctive and defensible dashboard |
| Safety | `blocked_by_backup_gate`, `approval_expired`, `injection_quarantine_hits`, `exactly_once_conflicts_detected` | Each is direct evidence for a Product Readiness claim |
| Target health | CockroachDB metrics via `ccloud cluster metric-export cloudwatch` | Lets "did the fix work?" be answered from first-party data, not our own instrumentation |
| Cost | RU consumption sampled from cluster metrics | Free-tier discipline (§18), and it is what an operator would actually watch |

One CloudWatch dashboard, screenshotted into the README. A single panel showing `recall_hit_rate` rising while `time_to_remediation` falls is the whole product thesis expressed as a chart — worth building deliberately.

---

# 18. Free-tier and cost discipline

Budget: **50M RUs + 10 GiB storage per month, per organisation.** Judging happens after submission, so the demo must still be alive on 2026-08-19 and beyond.

| Risk | Mitigation |
|---|---|
| Dashboard polling burns RUs | SSE over a single long-lived connection with a cursor; 2s minimum interval; no per-panel polling |
| Observation sweep every 5 min × N clusters | Sweep only registered clusters; cheap metric read, full introspection only on anomaly |
| Vector index maintenance during bulk seeding | Seed memories **before** creating the vector index; documented limit says avoid large batch vector inserts |
| Raw observations accumulate toward 10 GiB | Row-Level TTL at 30 days; large artifacts to S3 with only the URI + hash in-row |
| Changefeed to the dashboard | Explicitly **not** used — a nice-to-have that consumes RUs continuously; SSE + cursor is cheaper and simpler. Documented as a stretch |
| Free cluster reaped after inactivity | Only after 6 months of inactivity; our EventBridge heartbeat keeps it active regardless |

Two clusters total: `engram-memory` and `engram-target-sandbox` (the demo victim), both Basic. Bedrock cost is the only real spend — Sonnet 5 for reasoning, Titan V2 for embeddings, cached aggressively: **embeddings are computed once at write time and never recomputed at query time except for the incoming query itself.** Estimated well under $50 for the whole build and judging window.

---

# 19. 17-day execution plan (2–3 devs)

Roles: **A** = agent core (Python/LangGraph/Bedrock/MCP/ccloud). **B** = memory & data layer + infra. **C** = dashboard + demo + submission assets. With two devs, C's work is split with A taking the demo scenario and B taking deployment.

### Days 1–2 · De-risk the unknowns *before* designing around them
- **Day 1, hour 1, blocking:** create the free Basic cluster and confirm `SET CLUSTER SETTING feature.vector_index.enabled = true` succeeds and a `VECTOR INDEX` with a prefix column can be created and queried. Confirm Bedrock access to Claude Sonnet 5 **and** Titan Text Embeddings V2 in the chosen region. **These two checks take twenty minutes and can invalidate a week of design.**
- Create the public repo with `LICENSE` (Apache-2.0) in the first commit — the license must be detectable in GitHub's About sidebar, and the commit date is evidence of the newly-created requirement.
- Prove the managed MCP connection end-to-end with a service-account API key; call `list_clusters` and `explain_query` from Python. Confirm the response limits empirically.
- Prove `ccloud cluster backup list -o json` parses on a Basic cluster.

### Days 3–5 · Skeleton that survives death
- Full schema + migrations; `AsyncCockroachDBSaver` wired; the five-node LangGraph running end to end on a stub incident.
- **Kill-and-resume working by Day 5.** This is the headline; if it slips, the project has no climax. Fence tokens and the idempotency key land here, not later.

### Days 6–8 · Memory that works
- Embedding pipeline; vector index; scoped ANN + hybrid re-ranking; skills vendored, chunked, embedded.
- Incident simulator: a seeded table plus a workload that reliably produces a full-table-scan regression on demand. **Budget a full day for this** — the demo's credibility depends entirely on the regression being real and repeatable, and this is the most commonly underestimated task in the plan.
- MCP and ccloud tool adapters with allowlists, schema validation, and typed errors.

### Days 9–11 · Judgment and safety
- Approval gates; backup gate; policy engine; four identities and their grants; Secrets Manager; prompt-injection quarantine + the adversarial CI test; circuit breaker.
- Consolidation and decay Lambdas; Wilson-bound confidence; contradiction handling.

### Days 12–14 · Make it visible
- Dashboard: timeline, Memory Inspector, procedure ledger, approvals, audit stream, Kill button.
- Deploy: Fargate service, EventBridge rules, Vercel, `ccloud cluster metric-export cloudwatch enable`, CloudWatch dashboard.
- Run the full two-beat demo end to end at least ten times and record failure modes.

### Days 15–16 · Submission assets
- README: quickstart, architecture diagram, the four-identity and blast-radius tables, the §14.5 tool statements, measured numbers (recall latency, beam-size trade-off, MTTR before/after), and the falsifiability paragraph from §1.5.
- `docs/mcp-verification.md` so a judge can interrogate our memory cluster themselves.
- Record and edit the video (§20). Write the tool-feedback section (§21).

### Day 17 (2026-08-18, submit by 12:00 ET) · Buffer
- Submit with five hours of slack, not five minutes. Verify from a clean browser and an incognito session that the demo URL works, the video is public, and the license badge shows in the GitHub sidebar.

**Cut list, in order, if we fall behind:** time-travel beat in the video (keep the feature, cut the screen time) → CloudWatch dashboard screenshot → audit reconciliation → procedure induction from clustering (keep manual procedure creation) → Memory Inspector polish. **Never cut:** kill-and-resume, the backup gate, the two-incident contrast, the license, or the guest-accessible demo URL.

---

# 20. The 3-minute video

The video is scored as much as the code, and its brief is explicit: *demonstrate the CockroachDB memory layer at work.* Ours is 165 seconds with 15 seconds of slack.

| Time | Shot | Narration beat |
|---|---|---|
| 0:00–0:15 | Split screen: a 3am page next to the Memory Inspector | "Your team already solved this in March. Nobody remembers. Engram does." |
| 0:15–0:35 | Architecture diagram, **CockroachDB highlighted twice** | "One cluster is the agent's memory. The other is the database it operates. Everything the agent knows, and everything it has done, is in the first one." |
| 0:35–1:15 | Incident #1 live: detect → recall (Memory Inspector showing similarity + confidence 0.31) → `explain_query` → approve → backup gate → apply → 2.4s becomes 14ms | "It doesn't just retrieve. It cites the episode it learned from, and it checks a backup exists before it touches anything." |
| 1:15–1:45 | **The kill.** Terminal `aws ecs stop-task` mid-remediation → new task → lease reclaimed, fence token 2 → reconcile → **one** `remediation_actions` row | "Killed between deciding and acting. It resumes from a CockroachDB checkpoint — and does not apply the change twice." |
| 1:45–2:15 | Incident #2 on a different table, side by side with #1. 45s vs 8s | "Second time: eight seconds. Not because the model got smarter — because the memory got better. Watch the confidence go 0.31 to 0.58." |
| 2:15–2:35 | `AS OF SYSTEM TIME` query, then Claude Code interrogating the memory over MCP | "This is the agent's belief state at the moment it died. And you can ask its memory questions yourself." |
| 2:35–2:45 | The blast-radius table | "It cannot drop, truncate, or escalate. Every mutation is human-approved and backup-gated." |
| 2:45–2:55 | Closing card: repo, demo URL, the four tools | "Kill its memory and it stops. Kill the agent and it finishes the job." |

Rules: real screen recording, no slides pretending to be product; every number on screen must be reproducible from the repo; captions burned in (judges may watch muted); the CockroachDB memory layer must be visibly on screen for a clear majority of the runtime.

---

# 21. Feedback for Cockroach Labs (the optional section — write it, it is free credit)

Genuine observations gathered during the build, framed constructively. Draft points to expand from real experience:

1. **The managed MCP server's response limits (10 KiB / 20 s / `LIMIT 25`) are the right defaults but need to be prominent in the quickstart**, because they determine whether MCP belongs in an agent's hot path. We designed around them; a team that discovers them in week two loses days.
2. **`crdb_internal` being deny-listed is correct for safety but removes the most useful diagnostic surface** for an operations agent (`crdb_internal.cluster_queries`, statement statistics). A curated, safe, read-only projection of statement statistics over MCP would make MCP dramatically more useful to exactly the agentic use case this hackathon is about.
3. **`ccloud cluster disruption` being Advanced-only makes resilience demonstration inaccessible on free tiers** — a scoped, safe fault-injection primitive on Basic (even just a forced connection reset) would help the whole "prove it survives" story the platform is marketed on.
4. **Vector index + bulk load ergonomics:** "avoid large batch inserts" and "`IMPORT INTO` unsupported" are easy to trip over when seeding an agent's memory corpus; a documented recommended seeding order (load, then create index) would save time.
5. **`langchain-cockroachdb`'s `CockroachDBSaver` is the most valuable and least advertised asset in the ecosystem for this hackathon's theme.** It should be on the hackathon resources page directly — durable resumable agent execution is the sponsor's own thesis, and it already ships.
6. **Agent Skills would benefit from machine-readable applicability metadata** (which CockroachDB versions, which cluster tiers, which symptom classes) so agents can filter before embedding rather than relying on semantic similarity alone.

---

# 22. Judge mapping

Built for the rubric, not just for features. Every row's evidence is something a judge can see in the video or reproduce from the repo.

| Criterion | What we demonstrate | Demo evidence | Why it is strong |
|---|---|---|---|
| **Agentic Memory Design** | Eight typed memory classes in one cluster, each with its own write trigger, retrieval strategy, and retention rule; confidence earned from measured outcomes; provenance on every belief; consolidation, contradiction handling, and TTL-based forgetting; memory transactional with the actions it authorises | Memory Inspector showing retrieved rows with similarity + confidence; procedure ledger incrementing `attempts`/`successes`/`confidence` live; `AS OF SYSTEM TIME` belief replay; TTL declared in the DDL | Goes past storage into **lifecycle**. Almost every competitor will build the remembering half and skip the forgetting, scoring, and contradiction halves. And decision-plus-side-effect atomicity is a *correctness* argument that a bolt-on vector store cannot answer |
| **Technological Implementation** | Vector index with equality-constrained prefix + tuned beam size; MCP at runtime under `mcp:read` with the 10 KiB/20 s limits designed around; ccloud through an allowlist with `-o json` parsed to a schema and typed error mapping; Skills embedded, retrieved, and version-invalidated; `FOR UPDATE` leases with fence tokens; unique idempotency keys; LangGraph checkpointing via `AsyncCockroachDBSaver` | `docs/mcp-verification.md` lets judges query our memory over MCP themselves; CI tests for split-brain fencing and prompt injection; measured beam-size recall/latency table; `make demo-up` | Four tools, each doing work the product needs. The MCP limits and the Advanced-tier ccloud gaps are stated honestly, which reads as engineering judgment rather than marketing |
| **Real-World Impact** | Institutional operational memory: the org stops re-diagnosing problems it already solved, and knowledge stops leaving with people | Two incidents side by side: 45s vs 8s, with the *reason* visible in the database; MTTR before/after in the README | A problem the judges personally have. The win is quantified and attributable to memory, not to model quality |
| **Product Readiness** | Four least-privilege identities; blast-radius statement; MCP structurally unable to mutate; parameterised recipes so the model never emits SQL or shell; human approval with recorded approver and expiry; pre-flight backup gate; idempotency + fencing + circuit breaker; injection quarantine with an adversarial CI test; memory-level metrics in CloudWatch beside `ccloud`-exported DB metrics; verified backups of our own memory cluster | **A remediation visibly refused because the backup was stale**; the four-identity table; the fencing test; the CloudWatch dashboard | This is the criterion most submissions concede. We treat it as a feature and put a *refusal* on camera — nothing else says "production" in five seconds |
| **Creativity & Originality** | Procedures that earn confidence from measured outcomes; belief-state time travel via MVCC; forgetting as designed behaviour; exactly-once side effects across agent death; coordination through transactional shared state instead of agent-to-agent messaging | The kill-and-resume beat; the confidence delta; the `AS OF SYSTEM TIME` query | The novelty is in **mechanism**, not domain. None of it is reproducible with an LLM plus a vector store, and none of it can be faked in a demo because it only appears across failure or across time |

---

# 23. Competitive moat

## 23.1 What we expect to be up against

| Predicted submission | Why it loses ground | Where we are structurally different |
|---|---|---|
| RAG chatbot over docs with pgvector | Memory is read-only and externally authored; the agent learns nothing; Creativity ≈ 2 | Our memory is **written by the agent from its own measured outcomes** |
| "Second brain" / personal assistant | Named in the brief's own overused list; no autonomy; no side effects, so safety is moot | Real irreversible side effects make exactly-once and approval gates *necessary* rather than ornamental |
| Coding agent with conversation history | Memory = transcript. Nothing typed, scored, or consolidated | Eight classes with distinct lifecycles; procedures with confidence |
| Customer-support agent | Plausible impact, but memory is a ticket table and CockroachDB is interchangeable with Postgres | Remove our memory layer and the product is not worse, it is **unsafe** |
| Research / deep-research agent | Long-running, but state is in the context window; kill it and the task dies | Kill ours on camera and it finishes the job without redoing it |
| Multi-agent "swarm" demo | Impressive diagram, brittle behaviour, memory usually incidental to the choreography | We explicitly argue *against* unnecessary agents and coordinate through transactional shared state — a more sophisticated position, and more on-thesis |
| Genuine AIOps competitor (the real threat) | A few teams will build something adjacent. Most will stop at diagnosis and skip acting, because acting requires a safety model | Our differentiators against them: exactly-once across death, the backup gate refusal, confidence earned from outcomes, belief-state time travel, and all four CockroachDB tools genuinely used |

## 23.2 Why we will not look like another LLM wrapper

Four reasons, each verifiable from the repo rather than asserted:

1. **The interesting behaviour is not model output.** It is a `UNIQUE` constraint preventing a double-apply, a fence token rejecting a zombie, and a Wilson lower bound moving a confidence score. Swap the model out and the product still works. **That is the definition of not-a-wrapper.**
2. **The agent's competence changes over the demo, in the database, visibly.** A wrapper's behaviour is constant.
3. **It refuses to act.** Wrappers always answer. The backup-gate refusal is the clearest possible signal that judgment lives in the system, not in the prompt.
4. **The model cannot emit commands.** It selects typed recipes. That is an architectural boundary, not a system-prompt request, and it is visible in the code.

## 23.3 The sentence to leave behind

> **Engram is the agent whose memory you can kill mid-task — it comes back, finishes the job without redoing it, and solves the next incident in seconds because it remembered the last one.**

Say it in the first 10 seconds of the video and again in the last 10. Put it as the first line of the README and the first line of the Devpost description. After 50 demos, judges remember one sentence and one image: ours is *the container dying and one row appearing where two would have been.*

---

# 24. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The manufactured performance regression is unconvincing or flaky on Basic tier | **High** | **High** — it is the demo's spine | Budget a full day (Day 6–8). Pre-seed a large enough table, force a full scan on a non-indexed predicate, and record a verified fallback take. Have a deterministic fixture so the number is identical every run |
| `feature.vector_index.enabled` not settable on a free Basic cluster | Low | **Fatal** | **Verify in hour one of Day 1.** Fallback: brute-force cosine over a bounded candidate set, with the index path documented and tested against a Standard trial |
| Bedrock model access not granted in-region | Medium | High | Verify Day 1. Fallback region, or Claude via a different Bedrock region with documented latency |
| MCP response limits break the diagnosis flow | Medium | Medium | Already designed around: client timeout inside the server's, mandatory `LIMIT`, summarise before the model sees it |
| Scope creep from the four absorbed mechanisms | **High** | Medium | The §19 cut list is pre-agreed. Kill-and-resume must work by Day 5 or the project is re-scoped, not extended |
| Free-tier RU exhaustion mid-judging | Medium | High | §18 budget; SSE instead of polling; no changefeed; TTL on raw observations; monitor RU as a first-class metric |
| Demo URL down during judging | Medium | **High** | Vercel + Fargate service with desired-count 1 and auto-restart; a health-check alarm; a recorded fallback linked from the README |
| Judges read it as "a tool for DBAs" | Medium | Medium | Lead with institutional memory, not with databases; ship the memory engine as a separable package to prove generality |
| Agent takes an unintended action on camera | Low | **Severe** | Four identities, allowlisted recipes, human approval, backup gate. Blast radius is one index on one allowlisted schema |

---

# 25. Final recommendation

## BUILD THIS

**Project:** **Engram** — an autonomous database reliability engineer whose entire competence is its memory.

**One-line pitch:** Engram diagnoses and remediates production database incidents behind human approval, converting every incident into a scored, reusable procedure — so the second time a problem appears it is solved in seconds, and killing the agent mid-remediation loses nothing and repeats nothing.

**Core agent:** a single LangGraph agent, five nodes (Observe → Recall → Reason → Gate → Act & Measure), running long-lived on ECS Fargate, checkpointed into CockroachDB via `AsyncCockroachDBSaver`. Deliberately **not** multi-agent; concurrency is handled with `FOR UPDATE` leases and fence tokens rather than agent-to-agent messaging.

**Core memory innovation:** eight typed memory classes in one transactional store, where **the decision, the intent to act, and the side-effect record commit together** — plus procedures whose confidence is earned from measured outcomes, contradiction handling that retires knowledge that stopped working, TTL-based forgetting as a designed feature, and `AS OF SYSTEM TIME` replay of the agent's past *belief state*.

**CockroachDB tools:** all four. Distributed Vector Indexing (four embedded memory classes, `(scope_id, embedding vector_cosine_ops)` C-SPANN index, hybrid confidence-weighted re-ranking) · Managed MCP Server (runtime read-only introspection and hypothesis falsification via `explain_query`; plus judge-facing natural-language interrogation of our memory) · ccloud CLI (pre-flight backup gate that blocks remediation, version-aware entity memory, audit reconciliation) · Agent Skills (vendored at a pinned SHA, embedded, retrieved semantically, cited in decisions, version-invalidated).

**AWS services:** Bedrock (Claude Sonnet 5 + Titan Text Embeddings V2 @1024) · ECS Fargate (long-lived agent; `stop-task` is the demo) · Lambda (consolidation, decay, backfill) · EventBridge (heartbeat and alert fan-in) · S3 (EXPLAIN bundles and evidence) · Secrets Manager · CloudWatch (agent + `ccloud`-exported DB metrics in one pane) · IAM (four least-privilege identities).

**Main wow factor:** the container is killed on camera between "decided" and "applied" — the worst possible instant — and the replacement instance reclaims the lease, resumes from the checkpoint, reconciles against reality, and produces **one** action row where a naive agent would produce two. Then the same class of incident is resolved in 8 seconds instead of 45, with the confidence score moving in front of you.

**Why judges may love it:** it proves their own thesis rather than restating it; it uses four of their tools for reasons the product genuinely needs; it is strong on all five equally-weighted criteria instead of spiking on two; it puts a *refusal* on camera, which is the fastest way to communicate production maturity; and a judge can reproduce the headline moment themselves from the demo URL in ten seconds.

**Biggest risk:** manufacturing a convincing, repeatable performance regression on a free-tier cluster inside a 3-minute video.

**How we mitigate it:** a dedicated day (Days 6–8) for a deterministic incident fixture — pre-seeded table, forced full scan, identical numbers every run — plus a verified fallback recording, plus the whole demo scripted as a `make` target so it is a one-command replay rather than live improvisation.

---

## The first 10 things to build, in order

1. **De-risk in one hour.** Create the free Basic cluster. Run `SET CLUSTER SETTING feature.vector_index.enabled = true`, then create and query a `VECTOR(1024)` column with a prefix-column `VECTOR INDEX`. Separately confirm Bedrock access to Claude Sonnet 5 and Titan Text Embeddings V2 in your region. **If either fails, the architecture changes — so do this before writing anything else.**
2. **Create the public repo with `LICENSE` (Apache-2.0) in the very first commit.** Confirm GitHub's About sidebar shows the license badge. The commit date is your evidence for the newly-created-project rule.
3. **Ship the schema.** All tables from §12.2 as versioned migrations, including the `VECTOR INDEX` with prefix column and the two Row-Level TTL tables. Seed one target sandbox cluster.
4. **Wire `AsyncCockroachDBSaver` to a two-node LangGraph** and prove checkpoint-resume works: run, `kill -9`, restart, confirm it continues from the checkpoint. Nothing else is worth building until this works.
5. **Build the exactly-once core:** `agent_leases` acquisition via `SELECT … FOR UPDATE` with a fence-token bump, and `remediation_actions` with its `UNIQUE (idempotency_key)`. Write the split-brain test now, while the semantics are fresh — it is also a Product Readiness artifact.
6. **Connect the managed MCP server from Python** with a service-account API key scoped `mcp:read` and pinned via `mcp-cluster-id`. Build the tool adapter: 15-second client timeout, mandatory `LIMIT`, response summarisation under 10 KiB, typed errors. Wrap `explain_query`, `get_table_schema`, `show_running_queries` as agent tools.
7. **Build the memory data plane:** psycopg3 async pool, Bedrock embedding writer, and the scoped ANN + hybrid re-ranking retrieval from §12.4. Vendor `cockroachdb-skills` at a pinned SHA, chunk, embed, and store as `class='skill'`. **Create the vector index after seeding, not before.**
8. **Build the incident fixture.** A seeded table plus a workload that reliably regresses a query from milliseconds to seconds, and a verified index that fixes it. Deterministic, repeatable, one command. This is the demo's foundation and the most commonly underestimated task in the plan.
9. **Build the ccloud adapter and the backup gate.** Allowlisted parameterised commands, `-o json` parsed to a schema, error-code mapping. Then the gate itself: refuse to act and write a `blocked` decision when the newest backup exceeds RPO. Make sure the refusal is visible in the dashboard — it is your single best Production Readiness shot.
10. **Build the Memory Inspector panel first, before the rest of the dashboard.** It is the panel that makes recall visible — similarity, confidence, provenance, and the cited episode. It is also the panel the video depends on most. Everything else in the UI is supporting cast.

Then, in the order given by §19: approval gates and the four identities → consolidation, Wilson-bound confidence, decay and contradiction handling → the remaining dashboard → deploy and CloudWatch → the two-beat demo rehearsed ten times → README, video, submission.

---

## Appendix — sources

Verified 2026-08-01.

- [Hackathon rules](https://cockroachdb-ai.devpost.com/rules) · [resources](https://cockroachdb-ai.devpost.com/resources) · [project gallery](https://cockroachdb-ai.devpost.com/project-gallery) (unpublished as of this date)
- [Connect to the CockroachDB Cloud MCP Server](https://www.cockroachlabs.com/docs/cockroachcloud/connect-to-the-cockroachdb-cloud-mcp-server) · [Managed MCP Server announcement](https://www.cockroachlabs.com/blog/cockroachdb-ai-agents-managed-mcp-server/)
- [Vector Indexes](https://www.cockroachlabs.com/docs/stable/vector-indexes) · [VECTOR type](https://www.cockroachlabs.com/docs/v26.2/vector) · [C-SPANN](https://www.cockroachlabs.com/blog/cspann-real-time-indexing-billions-vectors/) · [Distributed vector indexing](https://www.cockroachlabs.com/blog/distributed-vector-indexing-cockroachdb/) · [CockroachDB and AI](https://www.cockroachlabs.com/docs/v26.2/cockroachdb-and-ai.html)
- [ccloud reference](https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-reference) · [ccloud get started](https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started) · [Agent-ready CLI](https://www.cockroachlabs.com/blog/cockroachdb-ai-agents-cli-database-automation/)
- [cockroachlabs/cockroachdb-skills](https://github.com/cockroachlabs/cockroachdb-skills) (Apache-2.0)
- [LangChain × CockroachDB](https://docs.langchain.com/oss/python/integrations/providers/cockroachdb) — `CockroachDBSaver` / `AsyncCockroachDBSaver`, `AsyncCockroachDBVectorStore`, `CockroachDBChatMessageHistory`
- [Row-Level TTL](https://www.cockroachlabs.com/docs/stable/row-level-ttl) · [CREATE CHANGEFEED](https://www.cockroachlabs.com/docs/v26.2/create-changefeed) · [Plan a Basic cluster](https://www.cockroachlabs.com/docs/cockroachcloud/plan-your-cluster-basic) · [Free trial](https://www.cockroachlabs.com/docs/cockroachcloud/free-trial)
- [Bedrock AgentCore overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html) · [AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html) · [AgentCore Gateway](https://aws.amazon.com/blogs/machine-learning/introducing-amazon-bedrock-agentcore-gateway-transforming-enterprise-ai-agent-tool-development/) · [AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html)

**Two facts to re-verify before Day 1 ends, because they are the only ones that could change the architecture:** that `feature.vector_index.enabled` is settable on a free Basic cluster, and that Bedrock grants Claude Sonnet 5 plus Titan Text Embeddings V2 in your chosen region.

