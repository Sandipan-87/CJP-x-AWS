# Engram workers — Lambda functions behind the dashboard's API Gateway + background lifecycle jobs

[PLUMBER]. `design/02-low-level-design.md` §11.2 (`approvals`/`metrics`/`webhooks`), §5.1 step 1
(`sweep_enumerator`), and §9 (`consolidator`/`decayer`/`embedding_backfill`, the memory janitors).
All six Lambdas named in the LLD's directory tree now exist.

**DEPLOYED LIVE** as of 2026-08-12 (`infra/`, stack `EngramApprovalsStack`). All three API-Gateway
routes verified against the real deployed infrastructure, not just locally. `sweep_enumerator`
(2026-08-13) and the three lifecycle-worker Lambdas (`consolidator`/`decayer`/`embedding_backfill`,
same session) are all built, unit-tested, and wired into `EngramAgentStack`'s CDK, but none of the
four have been deployed and all four EventBridge rules stay `enabled=False` — see
`infra/engram_infra/agent_stack.py`'s module docstring for exactly why. The lifecycle workers'
three DB roles ARE already provisioned and live-verified against the real memory cluster
(`db/migrations/009_lifecycle_roles.sql` + `scripts/bootstrap_lifecycle_roles.py`), unlike the
Lambda deployment itself.

## Layout

```
workers/
  common/          # shared, no agent/ import (see common/db.py's own docstring for why)
    db.py          # one connection factory per role (pg8000) -- approver/webhook/sweep_enumerator/
                    #   embedding_backfill/decayer/consolidator
    config.py      # "env var, else Secrets Manager" resolution, shared by db.py and webhooks
    incident.py    # the tasks+observations+entities one-txn insert, reimplemented for Lambda
    embed.py       # synchronous Cohere embed client (embedding_backfill/consolidator's own,
                    #   NOT agent/providers/cohere_embed.py -- that one's async, for the ECS agent)
    scoring.py     # decayed_confidence (wilson_lb * 90-day exp decay) -- duplicated, not
                    #   imported, from agent/memory/scoring.py; a canary test keeps them in lockstep
  approvals/handler.py           # POST /approvals/{approval_id}  -- API key
  metrics/handler.py             # GET  /metrics?window=1h        -- API key
  webhooks/handler.py            # POST /webhooks/alerts          -- HMAC signature, NOT an API key
  sweep_enumerator/handler.py    # EventBridge (5min, still disabled) -> reads watched_queries -> SQS
  embedding_backfill/handler.py  # EventBridge (nightly, still disabled) -> fills memory_items.embedding IS NULL
  decayer/handler.py             # EventBridge (nightly, still disabled) -> Wilson-decay + retire procedures
  consolidator/handler.py        # EventBridge (1h, still disabled) -> clusters episodes into procedures
```

## Why a separate DB role (and DSN) per Lambda

Same least-privilege discipline as every SQL role elsewhere in this project:
- `engram_approver` (migration 006): SELECT+UPDATE on `approvals` only.
- `engram_webhook` (migration 007): SELECT+INSERT on `tasks`, INSERT on `observations`,
  SELECT+INSERT+UPDATE on `entities` — **the SELECT on `entities` was a real, measured
  requirement, not assumed**: `INSERT ... ON CONFLICT DO UPDATE` needs SELECT to detect the
  conflict in the first place, on top of INSERT+UPDATE for the two branches. Caught live via
  `scripts/bootstrap_webhook_role.py`'s own verification step, not from reading the SQL alone.
- `engram_sweep_enumerator` (migration 008): SELECT on `watched_queries` only — read-only by
  design, since populating/editing the registry is an operator action, not something the
  automated sweep path should be able to do to itself.
- `engram_embedding_backfill` (migration 009): SELECT+UPDATE on `memory_items`, SELECT+INSERT on
  `embedding_cache`.
- `engram_decayer` (migration 009): SELECT+UPDATE on `procedures`, SELECT+UPDATE on `memory_items`
  — no INSERT anywhere, since decaying/retiring never creates a row.
- `engram_consolidator` (migration 009): SELECT on `remediation_actions`, SELECT+INSERT on
  `memory_items`/`procedures` — deliberately NO grant on `embedding_cache` (`consolidator/
  handler.py`'s own docstring: clustering reuses already-stored embeddings, never calls Cohere).
  **A second real, measured requirement, not assumed**: also needs SELECT on `tasks` and
  `entities`, even though it never reads either — CockroachDB checks SELECT privilege on a
  nullable FK's REFERENCED table (`procedures.created_by` → `tasks`, `memory_items.entity_id` →
  `entities`) even when that column is omitted from the INSERT and so is implicitly `NULL`.
  Caught live via `scripts/bootstrap_lifecycle_roles.py`'s own verification step (a real
  `InsufficientPrivilege` error on `tasks`/`entities` before the grants existed), not from reading
  the SQL alone — see migration 009's own comment.
- `metrics` needs no DB role at all — it only talks to CloudWatch.

## Local testing

`scripts/bootstrap_approver_role.py` / `scripts/bootstrap_webhook_role.py` /
`scripts/bootstrap_sweep_enumerator_role.py` / `scripts/bootstrap_lifecycle_roles.py` (repo root)
provision the roles, write each `ENGRAM_*_DSN` into the repo-root `.env`, and live-verify the
privilege boundary. With those set, each handler can be invoked directly (no AWS needed) — see
`scripts/smoke_test_approvals_lambda.py` for the pattern, or run a handler function directly with
a hand-built API-Gateway-proxy-shaped `event` dict, e.g.:

```python
import sys; sys.path.insert(0, "workers")
from webhooks.handler import handler
handler({"httpMethod": "POST", "headers": {...}, "body": "..."}, None)
```

`scripts/local_approvals_api_shim.py` does the same thing behind an actual local HTTP server, if
you want to test the dashboard's Next.js proxy route against real handler code without deploying.

## Why `pg8000`, not `psycopg3`

See `workers/common/db.py`'s own module docstring — no Docker in this dev environment, and
`psycopg[binary]`'s C extension needs Docker-based bundling for CDK's Python Lambda packaging.
`pg8000` is pure Python; `infra/build.py` hand-assembles each Lambda's package with a plain
`pip install --target`, no cross-compilation needed.
