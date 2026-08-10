# External constraints — the measured record

> Extracted from `CLAUDE.md` §4 on **2026-08-10** so that the always-loaded project-memory file stays inside its context budget. **Nothing here was dropped; it was moved.** `CLAUDE.md` §4 keeps the one-line rules and points here for the evidence.
>
> Items marked **MEASURED/VERIFIED** were run against real services; raw logs in `docs/_raw/`. **Trust the measurements over vendor docs.** Items marked **UNVERIFIED** are assumptions with a name attached — they are not facts, and no design decision should treat them as such.

---

## 1. Managed MCP Server — `https://cockroachlabs.cloud/mcp`

**MEASURED 2026-08-03**, `docs/_raw/p0-b2.log`.

| Property | Measured value |
|---|---|
| Server / protocol | `cockroachdb-cloud 1.0.0` / `2025-11-25` |
| Connect + initialise | ~1.3 s |
| Query timeout | **20 s** — observed firing at 20.8 s |
| Default `SELECT` row cap | **exactly `LIMIT 25`** when no limit is given |
| SQL length cap | **16,384 chars** → `query exceeds maximum length of 16384 characters` |
| Deny-listed schema | `query references a restricted schema: access to "X" is blocked for security reasons` |
| Response size ceiling | **NOT VERIFIED** — see below |
| `SHOW` 100-row cap | **UNTESTED** |

**Response ceiling is unfound.** No probe response exceeded 4,302 B (400 rows × 3 narrow columns), so the documented ~10 KiB truncation boundary was never reached. Budget **~900–1,000 rows of that shape** and assume it **truncates rather than errors** until proven otherwise.

**Tool parameters are `{database, query}` — NOT `sql`.** An unrecognised property makes the server reply `must contain exactly one statement`, which reads like a refusal rather than a schema error. This produced a **false pass** in the first verification run. Do not repeat it.

**12 tools are exposed, not 9.** `create_database`, `create_table` and `insert_rows` **are callable** with our `mcp:read`-intended key. The earlier assumption that write tools are simply absent was **wrong**. Therefore `agent/tools/mcp_tool.py` must be a **deny-by-default allowlist** — a passthrough is a prompt-injection hole. This is a *measured* row in the blast-radius table, not a theoretical one.

**The nine-tool read allowlist:** `list_clusters`, `get_cluster`, `list_databases`, `list_tables`, `get_table_schema`, `select_query`, `explain_query`, `show_statement`, `show_running_queries`.

**It is a control plane, not a data plane.** Hot-path memory reads go through psycopg3. MCP is for introspection, self-diagnosis and human interrogation.

Useful specifics:
- `show_statement` and `get_table_schema` **do** report vector indexes correctly — they were the P0-P1 artifact path.
- `explain_query` works and returns CockroachDB's **index recommendations** section. That is the pre-gate falsification signal the Gate node depends on.

**Auth:** service-account API key as `Authorization: Bearer`, pinned with the `mcp-cluster-id` header. Scope `mcp:read`, role Cluster Operator.

---

## 2. ccloud CLI — verified against **ccloud 0.6.12**, 2026-08-03

- `-o json` is a global flag. Error codes distinguish permission-denied / not-found / rate-limited.
- Our service account holds **Cluster Operator (read-only)** only.
- **`ccloud cluster disruption` is Advanced-tier only** — unusable on our free Basic cluster. Do not build the resilience demo on it: **we kill the agent, not the database.**
- **`ccloud cluster backup list` DOES NOT EXIST.** The available `cluster` subcommands are exactly: `list, info, create, delete, sql, update, regions, nodes, networking, user`.
- `cluster list` reports `plan: "SERVERLESS"` while the REST API reports `plan: "BASIC"` for the same cluster. **Adapters must tolerate both.**
- Used for: `cluster info`/`list` (entity memory), `audit list` (reconciliation).
- **The model never emits a command string.** It selects from an allowlisted enum; the adapter builds `argv`.

### 2.1 The backup gate must use the Cloud REST API

`GET https://cockroachlabs.cloud/api/v1/clusters/{id}/backups` → `200 {"backups": [...]}` — **verified working on a Basic cluster**, captured in `fixtures/cloudapi-backups-basic.json`.

- Requires a service-account role with backup read — **Cluster Admin, not Cluster Operator** — **scoped to the target cluster**. Our current key returns `403 unauthorized` for the target and `200` for the memory cluster.
- On a fresh Basic cluster the list is **empty**, so the gate's default outcome is **refuse**. That is the demo beat we want — but **do not claim the allow-path was tested unless it was.**

---

## 3. Reasoning providers — ladder is **Ollama Cloud → Groq → Together AI** (D13, 2026-08-11)

### 3.0 Ollama Cloud — primary, **VERIFIED 2026-08-11** (`scripts/verify_ollama.py`, real key, gate PASS)

- Wire shape resolved: **native `POST https://ollama.com/api/chat`**, not the OpenAI-compatible `/v1/chat/completions` — that's what the script actually calls and what passed. Rungs 2–3 (Groq, Together) stay OpenAI-compatible; the `LLMProvider` ABC absorbs the shape difference, so this does **not** unify wire formats across the ladder as once hoped, it just settles rung 1.
- Auth: `Authorization: Bearer $OLLAMA_API_KEY` — confirmed working against a real issued key.
- **Measured, replacing the prior "unverified" list:**
  - Probe A (auth + `/api/tags` listing): the exact tag `minimax-m3:cloud` does **not** appear in the returned list (18 models listed, only bare `minimax-m3` among them) — yet chat/tool calls against `minimax-m3:cloud` return HTTP 200 with sensible output every time. **Treat this tag as confirmed by behavior, not by the listing** — do not add a check that rejects the tag for being absent from `/api/tags`.
  - Probe B (plain chat round-trip): **1.45s** — inside the 5s reasoning budget.
  - Probe C (strict-JSON tool call, required `reasoning` field): **8.93s**, `reasoning` field populated with 846 chars of coherent rationale. Gate gate = PASS (A+B+C).
  - Probe E (multi-turn tool-result continuation): **1.61s**, handled correctly.
  - Probe F (embeddings availability): **no usable embedder** — `/api/embed` → 401 unauthorized, `/api/embeddings` → 404 not found, across 5 candidate model names. Confirms (does not newly decide) that Ollama Cloud has no fallback path for embeddings; Cohere remains the sole provider, §4.
  - Full transcript: `docs/phase0-verification.md` §3.2.
- Free tier is demo-grade; budget a paid tier from the day the demo is first rehearsed end-to-end.
- A circuit breaker parks the agent after N consecutive failures. Moving down the ladder is a config change. **If Ollama Cloud breaks the latency budget, promote Groq** (config-only, no code change).

### 3.1 Never depend on a vendor "thinking" channel

**Correction 2026-08-11 — the 2026-08-03 measurement below is contradicted by a real-key run, recorded not deleted.** Probe D of `scripts/verify_ollama.py` (`think=true`) shows `minimax-m3:cloud` **does** return `message.thinking` as its own field, and no `<mm:think>` tag leakage into `content` was observed. The specific empirical justification for tag-stripping no longer holds. **The design principle is retained anyway, as forward-looking robustness, not because of this measurement:** a vendor "thinking" channel is not a stable, audit-grade rationale surface across providers or model versions, so audit-grade rationale still lives in the tool schema's **required `reasoning` field**, enforceable by the pydantic validator. Tag-stripping logic can stay in the adapter as defense-in-depth; it is no longer covering an observed failure.

**Superseded — original 2026-08-03 (Session 2) claim, kept for the record:** *"`minimax-m3:cloud` never returned `message.thinking`, and `<mm:think>` tags leaked into `content`."* Session 2's own "next action" list still named `verify_ollama.py` as not-yet-run at the time (`docs/changelog-archive.md`) — no `OLLAMA_API_KEY` had been issued yet, so this was a recorded assumption, not a measurement, and is now known to be wrong for this provider/model as of the 2026-08-11 verified run.

### 3.2 Groq API — ladder rung 2, demoted (was D11's one-day primary, 2026-08-10)

- `POST https://api.groq.com/openai/v1/chat/completions`, `Authorization: Bearer $GROQ_API_KEY`, `stream: false`, OpenAI-compatible `tools` + `tool_choice`, temperature 0.1.
- **Unverified:** the model id, its availability on our tier, tool-calling fidelity, rate limits, latency. Any benchmark figure quoted in the design docs is a **vendor claim**, not a measurement of ours.
- Same OpenAI-compatible wire format as rung 1's preferred shape — the fastest rung to promote to if Ollama Cloud breaks the latency budget.

---

## 4. Cohere Embed — embeddings provider, **UNVERIFIED as of 2026-08-10**

- `POST https://api.cohere.com/v2/embed`, `Authorization: Bearer $COHERE_API_KEY`, `model=embed-english-v3.0`, `embedding_types=["float"]`.
- **`embed-english-v3.0` is documented as natively 1024-dimensional.** That native width — no truncation, no padding, no projection layer — is the entire reason it was selected over alternatives, because `VECTOR(1024)` is a schema invariant.
- **`input_type` is required for v3 models:** `search_document` on the write path, `search_query` on the recall path. Both live in **one** vector space and are designed to be compared against each other; the asymmetry is intentional. Collapsing it to a single `input_type` **degrades recall silently rather than erroring** — the same failure class that invariant #3 exists to prevent.
- **Unverified:** batch ceiling, rate limits, latency against the recall budget, and whether returned vectors are unit-norm. Cosine distance does not require unit norm, but the startup assertion should record what was actually observed rather than assuming.
- The adapter asserts `len(vec) == 1024` at startup and refuses any other length. **This assertion is what makes the documented dimension true for us.**

### 4.1 The vector space is now pinned

Invariant #2 pins the **dimension**; it cannot pin the **vector space**. Titan-1024, Cohere-1024 and any other 1024-dim model produce **mutually incomparable** vectors. From 2026-08-10, `embed-english-v3.0` **is** the space. Changing it means a full re-embed of `memory_items` and `embedding_cache`. There is **no fallback ladder for embeddings**, by design — unlike reasoning, an embedding swap is not a config change.

---

## 5. Amazon S3 — the AWS service on the agent's own path

- Bucket **`engram-agent-artifacts`**, accessed with **`boto3`**. Holds task logs, execution traces, EXPLAIN bundles and plan diffs.
- SSE-S3 encryption, versioning on, lifecycle rule to expire demo artifacts.
- Task-role policy grants `s3:PutObject` / `s3:GetObject` **scoped to that bucket ARN** — never `s3:*`, never `Resource: "*"`.
- Serves invariant #11: rows store the `s3://` URI plus a content hash, never the blob. This is what protects the 10 GiB free-tier budget.

---

## 6. Bedrock — the blocker, retained for the record

Every `InvokeModel` / `Converse` call returned `ValidationException: Operation not allowed`. This affected `claude-sonnet-5`, both `us.` and `global.` inference profiles, `claude-3-haiku` (ON_DEMAND, no access form) **and** `amazon.titan-embed-text-v1`/`v2` — Amazon's own first-party models.

**IAM was not the cause.** `ListFoundationModels` returned 119 models, and IAM denials surface as `AccessDeniedException`, a different exception class. The cause is **account-level**: a new AWS account still completing activation / payment verification. The support ticket belongs under *Account and billing → Activation*.

**Status: de-scoped, not fixed.** Nothing in the design invokes Bedrock any more. `scripts/verify_bedrock.py` now documents a blocker rather than testing a dependency. **Do not re-introduce a Bedrock dependency in order to "use more AWS"** — S3 is the AWS anchor on this path.

---

## 7. Free tier and submission rules

**CockroachDB Cloud free tier:** 50M RU + 10 GiB per month per org. **No changefeeds** — their RU cost is the reason the dashboard uses SSE + a cursor instead. No per-panel polling.

**Devpost hard rules:**
- Repo public, with an **Apache-2.0 `LICENSE` in the first commit**, detectable in GitHub's About sidebar.
- Project must be **newly created during the submission period** — the commit graph is the evidence.
- The demo URL must be testable **without our credentials**.
- Video **under 3 minutes**, public, and must show the memory layer at work.
