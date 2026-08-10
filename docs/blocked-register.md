# Broken / blocked register — full diagnoses

> `CLAUDE.md` §8 keeps the one-line status of every row so the always-loaded file stays inside its context budget; the full symptom, evidence and cause live here. **Nothing was dropped; it was moved.**
>
> **A row is removed only when it is genuinely fixed. De-scoped is not fixed.** That rule is unchanged.

---

## 1 — Bedrock invoke blocked account-wide · [BRAINS] · **DE-SCOPED 2026-08-10 — still broken, no longer blocking**

**Symptom.** Every `InvokeModel` / `Converse` call returned `ValidationException: Operation not allowed`. Affected `claude-sonnet-5`, both the `us.` and `global.` inference profiles, `claude-3-haiku` (ON_DEMAND, no access form required) **and** `amazon.titan-embed-text-v1` / `v2` — Amazon's own first-party models.

**Cause.** Account-level activation / payment verification on a new AWS account — *not* per-model approval and *not* IAM. `ListFoundationModels` returned 119 models, and IAM denials surface as `AccessDeniedException`, a different exception class. The support ticket belongs under *Account and billing → Activation*.

**Consequence.** No design path invokes Bedrock any more. `scripts/verify_bedrock.py` documents a blocker rather than testing a dependency. **Do not re-introduce a Bedrock dependency in order to "use more AWS"** — S3 is the AWS anchor on the agent's own path. See `docs/external-constraints.md` §6.

---

## 2 — Embeddings had no working provider · [PLUMBER] · **RESOLVED 2026-08-10**

**Symptom.** The embedding path had no callable provider, because Titan V2 was blocked by row 1.

**Cause.** Row 1, plus a planning error: HLD §14 rated Titan model access as "L / one-click enable". That was empirically wrong and is now recorded as a **realised High** risk with a permanent mitigation.

**Resolution.** Cohere `embed-english-v3.0`, natively 1024-dimensional — no truncation, padding or projection against `VECTOR(1024)`. Chosen **before any corpus was seeded**, so no re-embed was owed; the one-way pre-seed decision closed 5 days after its Day-5 deadline, which was survivable only because nothing was seeded. What remains is a **probe, not a decision**: batch ceiling, rate limits, latency and unit-norm behaviour are all unverified (`docs/external-constraints.md` §4).

---

## 3 — Local TCP 26257 blocked by a transparent proxy · [PLUMBER] · **OPEN — blocks all of Phase 1**

**Symptom.** psycopg3 and `cockroach sql` against port 26257 fail with `invalid response to SSL negotiation: H`. A raw probe of the same host:port returns `403 Forbidden` with `Server: squid/4.13` — the `H` is the first byte of `HTTP/1.1 403`. Ports 22 and 443 are open; no `HTTP_PROXY` / `HTTPS_PROXY` variables are set in the environment.

**Cause.** A transparent Squid proxy on the local network whose port allowlist omits 26257. Network-side, not ours to fix in code.

**Workaround used in Phase 0.** Everything was executed through the CockroachDB Cloud Console SQL Shell and the managed MCP server, both over 443 — hence `db/console/*.sql` exists as 14 pasteable chunks.

**Real fixes, in order of preference.** A different network (mobile hotspot) · an admin request to allowlist 26257 · move the dev loop onto an EC2 instance in the same region as the clusters.

---

## 4 — `LICENSE` absent from the existing first commit · [ILLUSIONIST] · **RESOLVED 2026-08-11**

**Symptom.** A first commit exists (`4304008`) with no `LICENSE` file.

**Cause.** Design documents were written ahead of execution; the repo was initialised without the licence.

**Correction to the original diagnosis.** The plan here and in `CLAUDE.md` §8/§9 read Devpost's rule as "licence in the first commit." Re-reading `research/hackathon.txt` directly: the actual rule is that the licence be **currently detectable and visible in the About sidebar** — a separate, already-satisfied rule requires the first commit to be dated after the submission window opened, which is about proving the project is new, not about where the licence file lands. By the time this was checked, `origin` already existed and `main` was already pushed, so a root-commit amend would have been a history rewrite on a public repo for no rule that actually required it.

**Fix applied.** Apache-2.0 `LICENSE` added as a normal commit and pushed to `main`. No rebase, no force-push.

---

## 5 — Cited design companions do not exist · [BRAINS] · **OPEN**

`design/03-adr.md` and `design/architecture.svg` are referenced by both design documents (ADR-001 … ADR-008) but were never written. Decisions are captured inline in HLD §3 instead. **ADR-001 (Ollama-primary reasoning) is reinstated in substance by D13 (`CLAUDE.md` §2.1, 2026-08-11); ADR-002 (Titan V2 embeddings) remains superseded by the Cohere decision.** Either write the companions or delete the citations.

---

## 6 — `execution_roadmap.md` is present but **pre-pivot** · [BRAINS] · **OPEN**

Recorded here and in `CLAUDE.md` as *missing* until 2026-08-10; that was wrong. It exists at **`research/execution_roadmap.md`** (22.6 KB, the Day-1 four-phase plan) — not at the repo root, which is why the pointers looked dangling. Both pointers now name the real path.

**What is actually broken:** it is **pre-pivot**. Its Phase 0/1 tasks still name Bedrock and Titan V2, so any day planned off it will plan work that no longer exists (`P0-B1` in particular is now an Ollama Cloud + Cohere probe, not a Bedrock one). Retarget it before using it to plan, or delete the references. **Do not leave a stale plan behind the pointer in the file that is read first every session.**
