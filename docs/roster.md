# Subagent roster — domains and owned invariants

> `CLAUDE.md` §5 keeps the role names, their domains and the frozen-contract dates so the always-loaded file stays inside its context budget; what each role *owns* lives here. **Nothing was dropped; it was moved.**
>
> Every code-generation turn states which role is executing. Roles are **domain boundaries** — don't cross one without a note in `CLAUDE.md` §7.

---

## [BRAINS] — Agent Core & AI

**Domain:** Python, LangGraph, Ollama Cloud / Groq / Together AI, Cohere, EventBridge.

**Owns:**
- The 5-node loop `Observe → Recall → Reason → Gate → Act & Measure`.
- Strict JSON tool schemas — **no free-form LLM output ever reaches a tool.** Includes the required `reasoning` field that carries audit rationale (`CLAUDE.md` §2.1: never depend on a vendor "thinking" channel).
- `AsyncCockroachDBSaver` checkpointing — the half of kill-and-resume that lives in the agent.
- The provider-agnostic `LLMProvider` ABC, which is what keeps a ladder rung change a config change rather than a code change.
- Graceful provider-throttle and MCP-timeout handling (MCP's measured timeout is 20 s — `docs/external-constraints.md` §1).

## [PLUMBER] — Distributed Data & Infra

**Domain:** CockroachDB / psycopg3, SQL DDL, ccloud, IAM, Secrets Manager, S3.

**Owns:**
- ACID transaction boundaries — invariant #6 (decision + intent + side effect in ONE transaction) is this role's to defend.
- Leases and fence tokens — invariant #5, the row lock plus monotonicity.
- The `vector_cosine_ops` C-SPANN index and the companion btree index — invariants #2 and #3.
- Row-Level TTL and `ON DELETE` correctness — invariant #7.
- Least-privilege IAM across the four identities: `s3:PutObject` / `s3:GetObject` scoped to one bucket ARN, never `s3:*`, never `Resource: "*"`.
- **Kill-and-resume correctness at the DB level** — including invariant #4, `remediation_actions.idempotency_key UNIQUE`.

## [ILLUSIONIST] — Frontend & Telemetry

**Domain:** Next.js, Tailwind, shadcn/ui, CloudWatch.

**Owns:**
- The Memory Inspector, with **similarity, confidence and provenance visible on screen** — this is demo beat #1's evidence.
- SSE plus a cursor, **not polling** — RU discipline against the 50M RU / month free tier; no changefeeds.
- The five demo metrics: `recall_hit_rate`, `time_to_remediation`, `memory_recall_latency_p99`, `blocked_by_backup_gate`, `exactly_once_conflicts_detected`.

---

## Frozen contracts

Changing one after its freeze date needs a `CLAUDE.md` §7 changelog entry.

| Contract | Owner | Freeze |
|---|---|---|
| SQL schema + migrations | [PLUMBER] | Day 3 |
| Tool-call JSON schemas | [BRAINS] | Day 4 |
| Read-only SSE query surface | [PLUMBER] + [ILLUSIONIST] | Day 5 |
