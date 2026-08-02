# Engram — 17-Day Execution Roadmap

> **Orchestrator's contract with the team.** Read `CLAUDE.md` before any session. Rationale for every decision here: `research/cockroachdb_aws_hackathon_strategy.md`.
>
> **Calendar.** Day 1 = **Sat 2026-08-01** (today) · Day 17 = **Mon 2026-08-17** · **Submit Tue 2026-08-18 by 12:00 ET**, five hours before the 17:00 ET close. Two weekends fall inside the window (Aug 1–2, 8–9, 15–16) — three of six weekend days are load-bearing, so plan accordingly.
>
> **Roles.** `[BRAINS]` agent core & AI · `[PLUMBER]` data & infra · `[ILLUSIONIST]` frontend & telemetry. Every task below has exactly one owner. Shared tasks name a lead.

---

## How to read this document

- **Exit gates are falsifiable.** Each phase ends with something you run or watch, never a percentage. A phase is not complete because the tasks are checked off; it is complete when the gate demonstrably passes.
- **Task IDs are stable** (`P1-B3` = Phase 1, Brains, task 3). Use them in commits and changelog entries.
- **"Expected output" is an artifact** — a file path, a passing test, or an observable behavior. If a task's output is "working code," the task is under-specified.
- **Dependencies are listed.** If your dependency isn't done, do the next task you own rather than waiting.

## The two orchestration problems this plan solves

1. **[ILLUSIONIST] would otherwise idle for eleven days** and then own the highest-visibility work at maximum deadline pressure. Every phase below gives them non-blocking work: the telemetry contract on Day 2, a dashboard scaffold against fixtures on Days 3–5, the Memory Inspector against seeded rows in Phase 2. **Phase 4 must be assembly, not construction.**
2. **Three interface contracts freeze early** or the roles block each other: SQL schema (Day 3, [PLUMBER]), tool-call JSON schemas (Day 4, [BRAINS]), read-only SSE surface (Day 5, [PLUMBER]+[ILLUSIONIST]). After the freeze date, a change requires a `CLAUDE.md` changelog entry naming what broke.

---

# PHASE 0 — Hour One (Day 1, blocking)

**Goal:** eliminate the two unknowns that could invalidate a week of architecture, before writing a line of design. Twenty minutes of work; do not skip it and do not do it second.

| ID | Role | Task | Expected output | Depends on |
|---|---|---|---|---|
| P0-P1 | [PLUMBER] | Create free CockroachDB Basic cluster `engram-memory`. Run `SET CLUSTER SETTING feature.vector_index.enabled = true`, then create a table with `VECTOR(1024)` + `VECTOR INDEX (scope_id, embedding vector_cosine_ops)` and run one `ORDER BY embedding <=> $1 LIMIT 3` query with `scope_id` equality-constrained | Terminal transcript pasted into `docs/phase0-verification.md`, showing the setting applied, the index created, and `EXPLAIN` confirming the **vector index is used** | — |
| P0-P2 | [PLUMBER] | Create second Basic cluster `engram-target-sandbox` (the demo victim) | Cluster ID recorded in `CLAUDE.md` §4 | — |
| P0-B1 | [BRAINS] | Confirm Bedrock access in the chosen region to **both** Claude Sonnet 5 **and** Titan Text Embeddings V2. Embed one string, assert the vector length is 1024 | Same verification doc; region recorded in `CLAUDE.md` | — |
| P0-B2 | [BRAINS] | Connect to the managed MCP server from Python with a service-account API key, `mcp:read`, pinned via `mcp-cluster-id`. Call `list_clusters` and `explain_query`. **Empirically confirm the 10 KiB / 20 s / `LIMIT 25` limits** rather than trusting the docs | Measured limits in the verification doc | P0-P2 |
| P0-P3 | [PLUMBER] | Confirm `ccloud cluster backup list --cluster <id> -o json` returns parseable JSON on a **Basic** cluster (not just Advanced) | Sample JSON committed as a test fixture | P0-P1 |
| P0-I1 | [ILLUSIONIST] | Create the public GitHub repo. **`LICENSE` (Apache-2.0) in the very first commit.** Confirm the license badge renders in the GitHub About sidebar | Repo URL + screenshot of the sidebar badge | — |

**EXIT GATE:** P0-P1 and P0-B1 both pass, and the license badge is visible. **If P0-P1 fails**, the fallback is brute-force cosine over a bounded candidate set with the index path documented and tested separately against a Standard trial — decide this on Day 1, not Day 8. **If P0-B1 fails**, switch region before anything is built against the wrong one.

**Risk:** the temptation to start schema design while waiting on cluster provisioning. Don't — provisioning is minutes, and designing against an unverified capability is how teams lose a week.

---

# PHASE 1 — The Immortal Skeleton (Days 1–5)

**Goal:** prove the single behavior the entire submission is built on — **kill the agent mid-task, resume from CockroachDB, apply the change exactly once.** Nothing else in the project matters if this doesn't work.

**Allocation:** [PLUMBER] leads (schema + transactional core). [BRAINS] builds the minimal graph. [ILLUSIONIST] scaffolds against fixtures and owns the telemetry contract — no dependency on the other two.

| ID | Role | Task | Expected output | Depends on |
|---|---|---|---|---|
| P1-P1 | [PLUMBER] | Write all tables from strategy §12.2 as versioned migrations: `entities`, `tasks`, `agent_leases`, `working_memory`, `memory_items`, `procedures`, `decisions`, `tool_calls`, `remediation_actions`, `approvals`, `observations` | `memory/migrations/001_*.sql` … applying cleanly from empty | P0-P1 |
| P1-P2 | [PLUMBER] | Row-Level TTL on `working_memory` (7d), `observations` (30d) | TTL visible in `SHOW CREATE TABLE`; a test that inserts a back-dated row and asserts expiry config | P1-P1 |
| P1-P3 | [PLUMBER] | **FREEZE THE SCHEMA (Day 3).** Publish `memory/schema.md` with every table, column, index, and its purpose | Frozen doc; changelog entry | P1-P1, P1-P2 |
| P1-P4 | [PLUMBER] | psycopg3 async pool + typed data-access layer. **`org_id` scoping enforced in the data layer, never in a prompt** | `memory/db.py` with pool lifecycle + a connection-loss test | P1-P1 |
| P1-P5 | [PLUMBER] | Lease acquisition: `SELECT … FOR UPDATE` + monotonic `fence_token` bump; stale-token writes rejected | `memory/leases.py` | P1-P4 |
| P1-P6 | [PLUMBER] | `remediation_actions` with `UNIQUE (idempotency_key)`; key = `sha256(cluster_id ‖ normalised_change)`. A uniqueness violation must be handled as "already intended → reconcile," never retried around | `memory/actions.py` | P1-P4 |
| P1-P7 | [PLUMBER] | **Split-brain test:** two workers given the same task; assert the second is fenced out and exactly one action row exists | `tests/test_fencing.py` passing in CI | P1-P5, P1-P6 |
| P1-B1 | [BRAINS] | Wire `AsyncCockroachDBSaver` to a two-node LangGraph. Prove resume: run → `kill -9` → restart → continues from checkpoint | `agent/graph.py` + transcript in `docs/phase1-kill-resume.md` | P1-P1 |
| P1-B2 | [BRAINS] | Expand to the five nodes (Observe → Recall → Reason → Gate → Act & Measure) with stubbed bodies and a typed state object | `agent/nodes/*.py`, all transitions checkpointed | P1-B1 |
| P1-B3 | [BRAINS] | **Every node transition commits working memory + checkpoint in the same transaction as its side effects.** Assert with a test that crashes between the two and shows no torn state | `tests/test_atomicity.py` | P1-B2, P1-P4 |
| P1-B4 | [BRAINS] | **FREEZE THE TOOL-CALL JSON SCHEMAS (Day 4).** Pydantic models for every tool input/output. The LLM selects from typed enums and fills validated parameters — **it never emits SQL or shell strings** | `agent/schemas.py` + `docs/tool-contracts.md` | P1-B2 |
| P1-I1 | [ILLUSIONIST] | **Telemetry contract (Day 2):** name and document every CloudWatch metric and every OTel span attribute before instrumentation exists | `docs/telemetry-contract.md` | — |
| P1-I2 | [ILLUSIONIST] | Next.js scaffold + shadcn/ui, routes and layout, driven entirely by a JSON fixture | `dashboard/` running locally against `fixtures/demo-state.json` | — |
| P1-I3 | [ILLUSIONIST] | **FREEZE THE SSE SURFACE (Day 5)** with [PLUMBER]: the read-only query set the dashboard needs, plus the `engram_reader` role's grants | `docs/sse-contract.md`, `engram_reader` grants in a migration | P1-P3 |

**EXIT GATE (Day 5, hard):**
```
1. Start a task. It reaches step 3 of 5.
2. kill -9 the process.
3. Restart. The new process reclaims the lease (fence_token 1 → 2),
   rehydrates from the checkpoint, and completes the task.
4. SELECT count(*) FROM remediation_actions WHERE idempotency_key = '<key>';
   → exactly 1.
```
**If this gate has not passed by end of Day 5, the project is re-scoped, not extended.** It is the demo's climax and Phases 2–4 are built on top of it. Re-scoping means cutting Phase 3's consolidation work and Phase 4's polish, never cutting this.

**Risks:** `AsyncCockroachDBSaver` semantics differing from expectations (mitigate by doing P1-B1 on Day 1–2, not Day 4). Schema churn after the Day-3 freeze cascading into [ILLUSIONIST]'s contract — hence the freeze.

---

# PHASE 2 — The Memory Engine (Days 6–8)

**Goal:** make the agent *recall*. By the end of Phase 2, incident #2 must demonstrably reuse what incident #1 taught it.

**Allocation:** [BRAINS] on retrieval + MCP + skills. [PLUMBER] on the embedding write path and — critically — the incident simulator. [ILLUSIONIST] builds the Memory Inspector against real seeded rows.

| ID | Role | Task | Expected output | Depends on |
|---|---|---|---|---|
| P2-P1 | [PLUMBER] | Embedding write path: Titan V2 → `memory_items.embedding`. **Seed the corpus, THEN create the vector index** (docs warn against batch inserts into vector-indexed tables) | `memory/embeddings.py`; documented seed order in `memory/schema.md` | P1-P4, P0-B1 |
| P2-P2 | [PLUMBER] | **The incident simulator — budget the full day.** A seeded table plus a workload that reliably regresses a query from ms to seconds, and a verified index that fixes it. Deterministic: identical numbers every run, one command | `scenarios/slow_query/` + `make scenario-1`; recorded before/after timings | P0-P2 |
| P2-P3 | [PLUMBER] | Second scenario on a *different* table, same problem shape — the "it remembers" beat needs two | `make scenario-2` | P2-P2 |
| P2-B1 | [BRAINS] | Scoped ANN retrieval: `ORDER BY embedding <=> $1 LIMIT 20` with `scope_id` equality-constrained. **Assert via `EXPLAIN` in a test that the vector index is actually used** | `memory/recall.py` + `tests/test_vector_index_used.py` | P2-P1 |
| P2-B2 | [BRAINS] | Hybrid re-rank: `0.45·similarity + 0.30·confidence + 0.15·recency + 0.10·entity_affinity`; hard-filter `confidence < 0.15` and `status <> 'active'` | Ranking unit tests with fixed inputs | P2-B1 |
| P2-B3 | [BRAINS] | MCP tool adapter: 15 s client timeout **inside** the server's 20 s ceiling; mandatory explicit `LIMIT`; summarise to under 10 KiB before the model sees anything; typed timeout/error results | `agent/tools/mcp.py` + a test that forces a timeout and asserts graceful degradation | P0-B2, P1-B4 |
| P2-B4 | [BRAINS] | Wire `explain_query` into Reason as **hypothesis falsification** — the agent tries to disprove its own diagnosis before proposing | Reason node emits a `Proposal` citing the EXPLAIN output | P2-B3 |
| P2-B5 | [BRAINS] | Vendor `cockroachlabs/cockroachdb-skills` at a **pinned git SHA**; chunk, embed, store as `class='skill'` with the SHA in `provenance` | `skills/` vendored + a loader; skill memories queryable | P2-P1 |
| P2-B6 | [BRAINS] | Retrieve 2–3 relevant skills per incident (not all of them); record `skill_shas` on every `decisions` row | Decision rows carry skill provenance | P2-B5, P2-B2 |
| P2-I1 | [ILLUSIONIST] | **Memory Inspector panel** — retrieved memories with similarity, confidence, provenance, and the cited episode. Built against real seeded rows, not fixtures | Panel renders live recall results | P2-B1, P1-I3 |
| P2-I2 | [ILLUSIONIST] | SSE stream with a cursor. **No per-panel polling; 2 s minimum interval** — RU discipline | `dashboard/app/api/stream/route.ts`; measured RU cost over 10 min | P1-I3 |

**EXIT GATE:** run `make scenario-1`, let the agent resolve it, then run `make scenario-2`. The second run must retrieve the procedure created by the first — visible in the Memory Inspector with a similarity score and a citation — and complete measurably faster. **Record both wall-clock times; they become the numbers in the README and the video.**

**Risks:** **P2-P2 is the most commonly underestimated task in the entire plan** and the demo's credibility rests on it. If the regression isn't convincing by end of Day 7, escalate — an unconvincing incident makes every other beat look staged. Second risk: the vector index silently not being used because a query forgot the `scope_id` equality constraint — which is exactly why P2-B1 ships with an `EXPLAIN` assertion.

---

# PHASE 3 — Judgment & Safety (Days 9–11)

**Goal:** win Product Readiness — 20% of the score and the criterion most submissions concede. The deliverable that matters most is **an agent that refuses to act.**

**Allocation:** [PLUMBER] on identities and the backup gate. [BRAINS] on approvals, consolidation, and injection defense. [ILLUSIONIST] on approval UI and the safety panels.

| ID | Role | Task | Expected output | Depends on |
|---|---|---|---|---|
| P3-P1 | [PLUMBER] | **Four least-privilege identities** with real grants: `engram_agent` (CRUD on memory only), `engram_reader` (SELECT only), `engram_mcp` (Cluster Operator, `mcp:read`, pinned), `engram_mutator` (`CREATE INDEX` on an allowlisted schema only — no DROP/TRUNCATE/unbounded DELETE) | Migration with grants + `docs/security.md` blast-radius table | P1-P3 |
| P3-P2 | [PLUMBER] | ccloud adapter: allowlisted parameterised commands, `argv` built by the adapter, `-o json` parsed to a schema, error codes mapped to typed exceptions | `agent/tools/ccloud.py` + tests for each error class | P0-P3, P1-B4 |
| P3-P3 | [PLUMBER] | **The pre-flight backup gate.** Before any mutation: `cluster backup list -o json`; if the newest backup exceeds RPO, **refuse**, write a `blocked` decision with the reason, and surface it | Demonstrable refusal; `tests/test_backup_gate.py` | P3-P2 |
| P3-P4 | [PLUMBER] | Secrets Manager for DSNs, ccloud API key, MCP bearer; fetched by IAM task role at boot. CI secret-scanning enabled | No secrets in repo; boot-time fetch working | P3-P1 |
| P3-P5 | [PLUMBER] | `ccloud audit list` reconciliation: cross-check control-plane actions against our own `audit_log`; flag anything present there but absent here | Reconciliation job + a seeded-discrepancy test | P3-P2 |
| P3-B1 | [BRAINS] | Approval gates: typed policy (change class × risk × confidence → auto vs. human). `approvals` records approver, reason, `expires_at`. **Expired consent is not consent** | `agent/gate.py` + expiry test | P1-B4 |
| P3-B2 | [BRAINS] | Parameterised recipe execution — the model selects a recipe kind and fills typed params, validated before rendering. **Never raw SQL from the model** | `agent/recipes/` with an allowlist | P1-B4 |
| P3-B3 | [BRAINS] | Consolidation Lambda: episode summarisation, near-duplicate merge via `supersedes` (not delete), procedure induction **requiring human confirmation the first time** | `workers/consolidate.py` on an hourly EventBridge rule | P2-B2 |
| P3-B4 | [BRAINS] | Confidence: Wilson lower bound × time decay. Contradiction handling — two consecutive `regressed` outcomes set a procedure `stale` and exclude it from recall | `memory/confidence.py` + tests showing 1/1 ranked below 47/50 | P3-B3 |
| P3-B5 | [BRAINS] | **Prompt-injection quarantine.** All target-cluster text (query text, table comments, error strings) and retrieved memories are data, never instructions. Adversarial CI test with an injected table comment | `tests/test_injection.py` passing | P2-B3 |
| P3-B6 | [BRAINS] | Resilience plumbing: exponential backoff with jitter on Bedrock/MCP, per-cluster action rate limits, circuit breaker parking the agent after N consecutive failures | Fault-injection tests | P2-B3 |
| P3-I1 | [ILLUSIONIST] | Approval queue UI: proposal, cited memories, confidence, diff, approve/reject with a reason (rejections lower confidence) | Working approval flow | P3-B1 |
| P3-I2 | [ILLUSIONIST] | Safety panels: procedure ledger (attempts/successes/confidence), audit stream, and a visible **blocked-by-backup-gate** state | Panels rendering live | P3-P3, P3-B4 |
| P3-I3 | [ILLUSIONIST] | Instrument the metrics from the Day-2 telemetry contract; build the one-panel CloudWatch view showing `recall_hit_rate` rising as `time_to_remediation` falls | CloudWatch dashboard + screenshot for the README | P1-I1 |

**EXIT GATE:** two things visible on screen — (1) a remediation **refused** because the newest backup is stale, with the reason recorded in `decisions`; (2) `tests/test_injection.py` and `tests/test_fencing.py` green in CI. Plus a procedure whose confidence *decreased* after a `regressed` outcome.

**Risks:** consolidation and confidence work (P3-B3/B4) is the most cuttable material here — if Days 9–11 slip, ship manual procedure creation and keep the gates. The gates are what score; automatic induction is what impresses.

---

# PHASE 4 — Visuals & Polish (Days 12–17)

**Goal:** a stranger reproduces the kill-and-resume beat from a public URL, and a judge watches 165 seconds that make the memory layer the star. **Sub-divided by day, because an undifferentiated six-day "polish" block is where hackathon projects die.**

### Day 12–13 — Dashboard completion · [ILLUSIONIST] leads
| ID | Role | Task | Expected output |
|---|---|---|---|
| P4-I1 | [ILLUSIONIST] | Task timeline with checkpoint markers; the kill/resume transition rendered as a visible discontinuity | Timeline panel |
| P4-I2 | [ILLUSIONIST] | **"Kill the agent" button** → ECS StopTask, rate-limited | Judge-operable kill switch |
| P4-I3 | [ILLUSIONIST] | Guest read-only mode + a "run scripted incident" button, so no anonymous visitor can run DDL anywhere | Guest mode verified in an incognito window |
| P4-B1 | [BRAINS] | EventBridge rules: 5-min sweep, hourly consolidation, nightly decay — the agent's heartbeat, so it is genuinely long-lived | Rules deployed and firing |
| P4-P1 | [PLUMBER] | S3 artifact path for EXPLAIN bundles and plan diffs; row holds URI + content hash | Artifacts landing in S3 |

### Day 14 — Deploy · all three
| ID | Role | Task | Expected output |
|---|---|---|---|
| P4-P2 | [PLUMBER] | Fargate service (desired-count 1, auto-restart), IAM task roles, `infra/` IaC | `make deploy` working from clean |
| P4-P3 | [PLUMBER] | `ccloud cluster metric-export cloudwatch enable` (run once, by a human, documented as such) | Target metrics in CloudWatch |
| P4-I4 | [ILLUSIONIST] | Dashboard to Vercel; public URL | Live URL |
| P4-P4 | [PLUMBER] | Verify our **own** memory cluster's backups + a documented restore drill — we built a backup gate for other people's databases; ours had better be recoverable | `docs/restore-drill.md` |

### Day 15 — Rehearse and measure · all three
| ID | Role | Task | Expected output |
|---|---|---|---|
| P4-A1 | all | Run the full two-beat demo **ten times end to end.** Log every failure mode and fix or route around each | `docs/demo-runbook.md` with 10 recorded runs |
| P4-A2 | [BRAINS] | Measure and record: recall latency p50/p99, `vector_search_beam_size` ∈ {8,32,64} recall/latency trade-off, MTTR incident #1 vs #2 | Numbers table for the README |
| P4-A3 | [PLUMBER] | RU burn check over a 24 h window against the 50M/10 GiB budget; confirm the demo survives past judging | RU projection in `docs/cost.md` |
| P4-I5 | [ILLUSIONIST] | README: quickstart, architecture diagram (CockroachDB shown **twice**, in both roles), four-identity + blast-radius tables, measured numbers, the falsifiability paragraph | README complete |

### Day 16 — Video and submission assets
| ID | Role | Task | Expected output |
|---|---|---|---|
| P4-A4 | all | Record and edit the video to the shot list in strategy §20. Real screen capture, burned-in captions, every on-screen number reproducible from the repo | < 3 min, public, unlisted-then-public |
| P4-B2 | [BRAINS] | `docs/mcp-verification.md` — config snippet + five natural-language questions a judge can ask our memory cluster over MCP directly | Judge self-verification path |
| P4-A5 | all | Devpost text: the §14.5 tool statements, AWS statements, and the tool-feedback section from strategy §21 | Draft ready to paste |

### Day 17 — Freeze · **no new features**
| ID | Role | Task | Expected output |
|---|---|---|---|
| P4-A6 | all | Code freeze. Only demo-breaking bug fixes | Tagged release |
| P4-A7 | all | Clean-browser + incognito verification: demo URL loads with no credentials, video is public, license badge visible in the GitHub sidebar | Verification checklist signed off |
| P4-A8 | all | **Submit on Day 18 (2026-08-18) by 12:00 ET** — five hours of slack, not five minutes | Submission confirmed |

**EXIT GATE:** hand the demo URL to someone outside the team with no instructions. They must be able to trigger an incident, watch the agent resolve it, kill the agent, and see it resume — without asking a question. **If they need help, the gate has not passed.**

---

# Cut list — pre-agreed, in order

Invoke from the top when a day slips. Deciding this now prevents a Day-15 argument.

1. Time-travel beat in the video (**keep the feature**, cut the screen time)
2. CloudWatch dashboard screenshot
3. `ccloud audit list` reconciliation (P3-P5)
4. Automatic procedure induction from clustering (keep manual procedure creation)
5. Memory Inspector visual polish (keep the data)
6. Second scenario table variety (keep two scenarios, accept similarity)

**Never cut, at any cost:** kill-and-resume · the backup-gate refusal · the two-incident contrast · the Apache-2.0 license in the first commit · the guest-accessible demo URL.

---

# Standing rituals

**Session start:** read `CLAUDE.md` → check `CURRENT POSITION` → check the broken/blocked register → announce the role executing.

**Session end (mandatory):** update `CLAUDE.md` §6 `CURRENT POSITION` → append a §7 changelog entry (built / verified working / **currently broken** / next action) → update §8 broken register. **A session with code and no changelog entry is unfinished.**

**Daily, 10 minutes, all three:** what's the current phase gate, who is blocked, has any frozen contract changed.

**Phase transitions:** the gate is demonstrated live to all three devs before the next phase starts. No self-certification.
