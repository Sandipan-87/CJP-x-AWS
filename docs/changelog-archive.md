# Changelog archive

> `CLAUDE.md` §7 keeps only the current phase's entries so the always-loaded file stays inside its context budget. **Entries are moved here, never deleted** — that rule is unchanged. Reverse-chronological, same format.

---

### 2026-08-11 — Session 4 · Reasoning primary swapped again: Ollama Cloud `minimax-m3:cloud` (D13) · **documentation only**

*`CLAUDE.md` §7 carries the short form of this entry; the long form lives here.*

- **Constraint honoured:** no Python, application logic or verification scripts were written or modified. Markdown and `.env.example` only.
- **Edited:** `CLAUDE.md` (§2.1 reasoning bullet, architecture diagram, roster, external constraints, top pointer line, §6 CURRENT POSITION, §7 changelog), `design/01-high-level-design.md`, `design/02-low-level-design.md`, `.env.example`. `docs/invariants.md` checked — schema-only, no provider content, **no edit needed**.
- **Decision locked (D13, supersedes D11):** Reasoning primary flips back to **Ollama Cloud `minimax-m3:cloud`**, ladder **Ollama Cloud → Groq → Together AI**, behind the unchanged `LLMProvider` ABC — a rung change stays config, not code. D11 (Groq primary, locked 2026-08-10) lived exactly one day. D13 reinstates ADR-001 in substance.
- **Why:** `scripts/verify_ollama.py` (probes A–F) was already written 2026-08-03; `scripts/verify_groq.py` was never written under D11. MiniMax M3 was ADR-001's original pick on agentic tool-use merit; Ollama Cloud hosting removes any local-daemon or AWS-model-access dependency.
- **New risk, now primary-path rather than defensive:** `minimax-m3` is a thinking model — `message.thinking` may be absent while `<mm:think>` tags leak into `content` (cited: ollama #16632, vLLM #45687, both unverified by us). The adapter must strip `<mm:think>` tags from `content` before JSON parsing; this is **load-bearing**, not a defensive nicety. The tool-call JSON schema's required `reasoning` field carries audit rationale regardless, per the standing "never depend on a vendor thinking channel" rule.
- **Open/unverified, flagged in every touched doc:** (a) endpoint shape — OpenAI-compatible `POST /v1/chat/completions` vs native `POST /api/chat`, to be settled by T9a, preference stated for OpenAI-compatible so ladder rungs 2–3 share wire format with rung 1; (b) model tag — `minimax-m3:cloud` vs `minimax-m3`. Both gate the Day-4 tool-schema freeze.
- **Latency risk elevated:** the `<5s` reasoning budget (part of the `<8s` end-to-end demo beat) is now the tightest, most at-risk budget under D13, since a thinking model's hidden tokens bill against it. Mitigation if it breaks: **promote Groq to primary** (config-only, no code change), not prompt tuning.
- **The three-file + env sweep, in detail:**
  - `design/01-high-level-design.md` — reasoning provider, ladder order and diagram references swapped Ollama-primary throughout (~21 edits).
  - `design/02-low-level-design.md` — file-tree adapter comments (`ollama_cloud_llm.py` now primary, `groq_llm.py`/`together_llm.py` demoted ladder siblings), env-var table rows, `ENGRAM_LLM_PROVIDER`/`ENGRAM_LLM_MODEL` UNVERIFIED flags, startup self-test description, `Proposal.reasoning` field comment (now naming minimax-m3 + `<mm:think>` leakage explicitly), the `reason(node)` step-2 narrative, a DDL comment, the big adapter table's primary row rewritten to `OllamaCloudLLM`, T3/T9a test rows, the rollout checklist, and the error table's rung-1 provider name (~18 edits). Grep-verified afterward: no stray Groq-primary framing survived.
  - `.env.example` — header comment + first key swapped to `OLLAMA_API_KEY`; the reasoning-provider block reordered (`ENGRAM_LLM_PROVIDER=ollama`, `OLLAMA_BASE_URL`, `ENGRAM_LLM_MODEL=minimax-m3:cloud` flagged UNVERIFIED inline) with `GROQ_API_KEY`/`TOGETHER_API_KEY` demoted to the "ladder rungs 2 and 3 — fill only when in use" block.
- **Adapter naming convention going forward:** `OllamaCloudLLM` is the primary adapter class; `GroqLLM`/`TogetherLLM` are ladder rung-2/3 siblings sharing the same OpenAI-compatible wire format as each other.
- **Not done — stated plainly:** model tag and endpoint shape remain **UNVERIFIED**; running `verify_ollama.py` is next session's first code, same open item as Session 3 left for Groq and never executed.
- **Still pending from this sweep, not yet done:** `docs/roster.md` (Ollama-first reorder in the [BRAINS] line), `docs/submission-checklist.md` ("Ollama Cloud, Cohere" in the AWS statement), `docs/external-constraints.md` §3 (Groq-framed heading needs Ollama-primary reframing), a grep-verify pass on `docs/blocked-register.md`.
- **Next action (as recorded then):** run `scripts/verify_ollama.py` (probes A–F) to confirm model tag + endpoint shape; then Cohere probe (T9b/T9c unchanged); then P1-P1 migrations from the LLD §6.2 fixed-state DDL.

---

### 2026-08-10 — Session 3 · Bedrock removed from the design · Groq + Cohere + S3 locked · **documentation only**

*`CLAUDE.md` §7 carries the short form of this entry; the long form lives here.*

- **Constraint honoured:** no Python, application logic or verification scripts were written or modified. Markdown and `.env.example` only.
- **Edited:** `CLAUDE.md` — the provider pivot applied throughout, a new §0 coding-conduct section added from `multica-ai/andrej-karpathy-skills`, and the file compacted from ~27 KB toward its 14 KB context budget. **No rule was dropped.** Bulk was *relocated*: measured external limits → the new `docs/external-constraints.md`; the unabridged conduct rules → the new `docs/coding-conduct.md`; Sessions 1–2 and the long form of this entry → this file. Also edited: `design/01-high-level-design.md`, `design/02-low-level-design.md`, `.env.example`.
- **Decisions locked:**
  - Reasoning → **Groq API primary**, ladder **Groq → Together AI → Ollama Cloud**, behind the existing `LLMProvider` ABC so a rung change stays a config change. **Bedrock removed from the ladder entirely.** Supersedes ADR-001 (Ollama-primary).
  - Embeddings → **Cohere `embed-english-v3.0`**, chosen because it natively emits exactly **1024** dimensions — no truncation, padding or projection against the `VECTOR(1024)` schema. Closes the one-way, pre-seed embedding decision **5 days after its Day-5 deadline**, which was survivable only because no corpus had been seeded. Supersedes ADR-002 (Titan V2).
  - AWS AI-path anchor → **S3 `engram-agent-artifacts`** via `boto3`, holding task logs and execution traces. This is what satisfies the hackathon's AWS-service requirement now that Bedrock is gone.
- **Corrections to prior belief:**
  - HLD §16 listed "no embedding model switch (vector-space identity is a schema invariant)" as a fixed constraint. That conflated **dimension** with **vector space**. The dimension is fixed at 1024 forever; the space changed exactly once, before seeding. Rewritten.
  - HLD §14 rated Titan access as "L / one-click enable". Empirically wrong — it is now recorded as a **realised High** risk with a permanent (not temporary) mitigation.
- **The four-file sweep, in detail:**
  - `.env.example` — rewritten to the mandated key schema (`GROQ_API_KEY`, `COHERE_API_KEY`, `EMBEDDING_MODEL=embed-english-v3.0`, `EMBEDDING_DIM=1024`, the four `AWS_*` keys with `AWS_S3_BUCKET_NAME=engram-agent-artifacts`, `DATABASE_URL`), plus the two-cluster DSNs, the MCP block and the ladder rungs. **No Bedrock model id remains; it stays a placeholder-only template.**
  - HLD — data flow and diagram now read Cohere → Groq → CockroachDB → S3; the fallback ladder is Bedrock-free on **every** rung (D11/D12); the 1024-dim constraint is stated as a schema constraint of `VECTOR(1024)` + C-SPANN `vector_cosine_ops`, not a tuning knob; the risk table carries Bedrock account access as **realised / High** with a permanent mitigation.
  - LLD — client-initialisation docs name the **`cohere` SDK (or httpx)** and **`boto3` for S3 uploads only**; new `GroqLLM` and `CohereEmbeddings` adapter rows (with `input_type` a required argument and a startup `len(vec) == 1024` assertion) alongside `S3Artifact` (`put/get/sha256`, bucket-scoped IAM); LLM exceptions made provider-neutral so promoting a ladder rung changes no `except` clause; new `EmbeddingDimensionError` — **never degrade, never write, park immediately**; tests retargeted as **T9a** `verify_groq.py`, **T9b** `verify_cohere.py`, **T9c** `verify_s3.py`, T11 asserts no `bedrock` string in any source file, and **T12** `test_ttl_reclaim.py` was added (it was cited by §6.2 note (b) but did not exist).
  - LLD §6.2 DDL — the three stated-but-unapplied defects are now the **fixed state**: `remediation_actions` is declared **before** `approvals` (`approvals.action_id` is a hard FK into it, and `remediation_actions.approval_id` deliberately carries no FK to avoid the cycle); every FK into a TTL'd parent carries an explicit `ON DELETE`, including **both** of `approvals`'; migration 002 adds `ALTER DEFAULT PRIVILEGES` so tables created by 003/004 are covered. `VECTOR(1024)`, the post-seed C-SPANN step, the companion btree and the `$2::VECTOR(1024)` casts are unchanged.
  - Least privilege **tightened, not loosened**: `engram_agent` keeps `SELECT, INSERT, UPDATE` and loses blanket `DELETE` (Row-Level TTL does the deleting), with one table-scoped exception — `DELETE ON agent_leases`, because §6.4's SIGTERM lease release is a real delete and stranding the lease for a 60 s expiry would slow the kill-and-resume demo.
- **Cross-document contradictions closed:** `docs/invariants.md` §7 listed the `ON DELETE` tables from memory and omitted `approvals` — reconciled, and the invariant is deliberately worded "every FK to a TTL'd parent" so it is **enumerated from the DDL, never from memory**. `CLAUDE.md` claimed `execution_roadmap.md` was missing; it exists at `research/execution_roadmap.md` (22.6 KB, Day-1 plan) but is **pre-pivot**, so its Bedrock/Titan tasks are stale — the pointer and §8 #6 now say so, and it was **not** rewritten, being outside the four target files.
- **Not done — stated plainly:** every Groq and Cohere property in these documents is **vendor-documented, not measured by us**. Model id, tool-calling fidelity, batch ceilings, rate limits, latency against the 5 s reasoning / 8 s end-to-end budget, and whether Cohere returns unit-norm vectors are all open. Those probes are the next session's first code.
- **Next action (as recorded then):** LICENSE into an amended first commit; unblock TCP 26257; live Groq + Cohere probes; P1-P1 migrations applying the three LLD §6.2 DDL fixes.

---

### 2026-08-03 — Session 2 · Phase 0 executed · reasoning provider swapped off Bedrock

- **Built:** `db/phase0_vector_probe.sql` + `db/console/*.sql` (14 console-pasteable chunks, needed because local 26257 is proxy-blocked) · `scripts/verify_mcp.py` (8 probes) · `scripts/verify_bedrock.py` · `scripts/run_sql.py` (psycopg3 runner) · `scripts/requirements-verify.txt` · `docs/phase0-verification.md` (the evidence record) · `.env` / `.env.example` · `.gitignore` · fixtures from the Cloud REST API. Reviewed `design/01-high-level-design.md` and `design/02-low-level-design.md`.
- **Verified working — P0-P1 PASSES.** `VECTOR INDEX (scope_id, embedding vector_cosine_ops)` works on a free Basic v26.2.1 cluster. The plan shows a `vector search` operator on `vec_probe_scope_cos` with `prefix spans` on `scope_id`, reading **11 of 400 rows** in **6 ms / 5.576 RU**. The probe vector was deliberately built to equal row 200's embedding, so recovering `id=200` at distance `2.39e-07` is self-validating rather than merely plausible. The negative control — the same query with no `scope_id` predicate — correctly shows a `FULL SCAN`. P0-P2, P0-P3 and P0-B2 also pass.
- **Currently broken (at the time):** Bedrock invoke blocked account-wide → embeddings had no provider. Local TCP 26257 blocked by a Squid proxy. `LICENSE` absent from the existing first commit.
- **Decisions locked (reasoning and embeddings since superseded by Session 3):** reasoning → Ollama Cloud `minimax-m3:cloud` (ADR-001), with no dependence on a thinking channel · embeddings stay 1024-dim and the provider choice is one-way, pre-seed · backup gate → **Cloud REST API**, not ccloud · MCP adapter → **deny-by-default allowlist**, because write tools turned out to be exposed.
- **Corrections to prior belief:** `ccloud cluster backup list` does not exist · MCP exposes 3 write tools, not 0 · MCP params are `{database, query}`, not `{sql}` · C-SPANN does **not** serve plain `scope_id` predicates, so `memory_items` needs its own btree index. Two claims about CockroachDB introspection were made and retracted — see `docs/phase0-verification.md` §1.2: the console `Internal error` was real but unreproduced, and there is **no** omission defect.
- **Measured limits from this session now live in `docs/external-constraints.md`.**
- **Next action (as recorded then):** LICENSE into an amended first commit; unblock 26257; `verify_ollama.py`; P1-P1 migrations.

---

### 2026-08-01 — Session 1 · Orchestration setup

- **Built:** `research/cockroachdb_aws_hackathon_strategy.md` (full strategy, 25 sections, research-verified) · `CLAUDE.md` · `execution_roadmap.md` (4-phase, 17-day plan with role ownership — it lives at **`research/execution_roadmap.md`**, not the repo root, which is why later sessions recorded it as missing; it is pre-pivot, see `CLAUDE.md` §8 #6).
- **Verified working:** nothing executable — documents only.
- **Decisions locked:** Engram chosen over 23 alternatives · single agent, not multi-agent · Fargate over AgentCore Runtime (schedule risk; `stop-task` is the demo) · kill the agent, not the database (`cluster disruption` is Advanced-tier only) · no changefeeds (RU cost) · Apache-2.0.
- **Next action (as recorded then):** Phase 0 verification — vector index setting and Bedrock model access.
