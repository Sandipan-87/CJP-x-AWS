# Engram workers — Lambda functions behind the dashboard's API Gateway + the sweep enumerator

[PLUMBER]. `design/02-low-level-design.md` §11.2 (`approvals`/`metrics`/`webhooks`) and §5.1 step 1
(`sweep_enumerator`). The lifecycle-worker Lambdas (`consolidator`/`decayer`/`embedding_backfill`)
named in the LLD's directory tree are NOT built, out of scope so far.

**DEPLOYED LIVE** as of 2026-08-12 (`infra/`, stack `EngramApprovalsStack`). All three API-Gateway
routes verified against the real deployed infrastructure, not just locally. `sweep_enumerator`
(added 2026-08-13) is built, unit-tested, and wired into `EngramAgentStack`'s CDK, but its
EventBridge rule is still `enabled=False` and it has not been deployed — see
`infra/engram_infra/agent_stack.py`'s module docstring for exactly why.

## Layout

```
workers/
  common/          # shared, no agent/ import (see common/db.py's own docstring for why)
    db.py          # engram_approver + engram_webhook + engram_sweep_enumerator connection factories (pg8000)
    config.py      # "env var, else Secrets Manager" resolution, shared by db.py and webhooks
    incident.py    # the tasks+observations+entities one-txn insert, reimplemented for Lambda
  approvals/handler.py         # POST /approvals/{approval_id}  -- API key
  metrics/handler.py           # GET  /metrics?window=1h        -- API key
  webhooks/handler.py          # POST /webhooks/alerts          -- HMAC signature, NOT an API key
  sweep_enumerator/handler.py  # EventBridge (5min, still disabled) -> reads watched_queries -> SQS
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
- `metrics` needs no DB role at all — it only talks to CloudWatch.

## Local testing

`scripts/bootstrap_approver_role.py` / `scripts/bootstrap_webhook_role.py` (repo root) provision
the roles, write `ENGRAM_APPROVER_DSN`/`ENGRAM_WEBHOOK_DSN`/`ENGRAM_WEBHOOK_HMAC_SECRET` into the
repo-root `.env`, and live-verify the privilege boundary. With those set, each handler can be
invoked directly (no AWS needed) — see `scripts/smoke_test_approvals_lambda.py` for the pattern,
or run a handler function directly with a hand-built API-Gateway-proxy-shaped `event` dict, e.g.:

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
