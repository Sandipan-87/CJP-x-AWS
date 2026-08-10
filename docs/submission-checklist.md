# Submission checklist — full text

> `CLAUDE.md` §9 keeps the one-line checklist so the always-loaded file stays inside its context budget; the wording we actually have to produce lives here. **Nothing was dropped; it was moved.**
>
> **Never cut, whatever slips:** kill-and-resume · the backup-gate refusal · the two-incident contrast · the licence · the guest-accessible demo URL.

---

## 0. Deadline and judging criteria

**2026-08-18 17:00 ET; we submit by 12:00 ET.** CockroachDB × AWS. Five **equally weighted** criteria — Agentic Memory Design · Technological Implementation · Real-World Impact · Product Readiness · Creativity & Originality. Equal weighting is why the dashboard and the README count as much as the agent loop.

## 1. Repository

Public repo, **Apache-2.0 `LICENSE` visible in the GitHub About sidebar**, first commit dated after 2026-06-30 (the submission window opened; the commit graph is the evidence that the project is new).

✅ **RESOLVED 2026-08-11** — `LICENSE` added and pushed as a normal commit; first commit `4304008` is already dated after 2026-06-30. See `docs/blocked-register.md` §4 for why no root-commit amend was needed.

## 2. Written statement — which AWS services, and how

**Say it plainly: no Bedrock.** Reasoning and embeddings are third-party cloud APIs (Ollama Cloud, Cohere) because Bedrock `InvokeModel` is blocked account-wide (`docs/external-constraints.md` §6). AWS provides the runtime, the orchestration and the durable artifacts:

| Service | What it actually does here |
|---|---|
| **ECS Fargate** | Hosts the long-lived agent. **Load-bearing:** `aws ecs stop-task` *is* the resilience demo. |
| **Lambda** | Lifecycle workers — consolidation, decay, embedding backfill. Separate from the agent on purpose: memory maintenance must survive agent death. |
| **EventBridge** | 5-minute sweep, hourly consolidation, nightly decay. |
| **SQS** (`engram-commands`) | Durable trigger, FIFO by fingerprint. |
| **API Gateway** | Approval callbacks and the metrics surface. |
| **S3** (`engram-agent-artifacts`) | Task logs, execution traces, EXPLAIN bundles, plan diffs via `boto3`. Serves invariant #11. |
| **Secrets Manager** | Provider keys and both cluster DSNs (Phase 3 / P3-P4). |
| **CloudWatch** | The five demo metrics. |
| **IAM** | Four identities, least privilege — `s3:PutObject`/`s3:GetObject` scoped to one bucket ARN, never `s3:*`. |

**Never imply Bedrock does the reasoning.**

## 3. README

Quickstart · architecture diagram · the four-identity and blast-radius tables · measured numbers (recall latency, beam-size trade-off, MTTR before/after) · a falsifiability paragraph stating what would prove the memory layer is not working.

## 4. Demo

- **Functional demo URL, testable by a stranger with no credentials**, alive through judging.
- Video **under 3 minutes**, public, with the memory layer on screen for most of the runtime.

## 5. Written statement — CockroachDB tools

Which CockroachDB tools were used and **what the agent actually did with them** (strategy §14.5): the managed MCP server as a control plane behind a nine-tool read allowlist, psycopg3 on the hot path, `VECTOR`/C-SPANN for recall, Row-Level TTL for forgetting, `AS OF SYSTEM TIME` for belief-state replay, and the Cloud REST API for the backup gate.

## 6. Optional, but do it

Architecture diagram and tool feedback to CockroachDB Labs (strategy §21).
