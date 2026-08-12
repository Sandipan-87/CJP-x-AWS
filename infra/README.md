# Engram infra — the dashboard-facing API Gateway (approvals + metrics + webhooks)

[PLUMBER]. `design/02-low-level-design.md` §11.2 / HLD §5.6. One stack, `EngramApprovalsStack`
(Python class `EngramApiStack` — renamed once scope grew past just approvals; the CloudFormation
stack id is unchanged, so `cdk deploy` updates the same deployed stack rather than replacing it).
The lifecycle-worker Lambdas (`consolidator`/`decayer`/`embedding_backfill`) named in the LLD's
directory tree are still NOT built, out of scope so far.

**DEPLOYED LIVE.** All three routes verified against the real deployed infrastructure:

| Route | Auth | Verified |
|---|---|---|
| `POST /approvals/{approval_id}` | API key | Real browser click → real `200` → real `approvals` row (`status='approved'`, `channel='dashboard'`), 2026-08-12. |
| `GET /metrics?window=1h` | API key | Real `200` against real CloudWatch — correctly empty for every `engram`-namespace metric (nothing publishes to it yet, stated not hidden), 2026-08-12. |
| `POST /webhooks/alerts` | HMAC-SHA256 signature | Real `200`, real incident `task`+`observation`+`entity` row, real dedupe on a repeat call, real `401` on a bad signature — all verified directly by querying the database afterward, 2026-08-12. |

Deployed under a dedicated `engram-deploy` IAM user (kept separate from `engram-phase0`, which
stays S3-only), using a custom least-privilege policy (`EngramCdkDeploy`) — not
`AdministratorAccess`.

## What each route does

- **`POST /approvals/{approval_id}`** → `workers/approvals/handler.py` → CockroachDB via
  `engram_approver` (SELECT+UPDATE on `approvals` only, `db/migrations/006_approver_role.sql`).
- **`GET /metrics?window=1h`** → `workers/metrics/handler.py` → CloudWatch `ListMetrics`+
  `GetMetricData` directly (no DB role needed at all).
- **`POST /webhooks/alerts`** → `workers/webhooks/handler.py` → CockroachDB via `engram_webhook`
  (SELECT+INSERT `tasks`, INSERT `observations`, SELECT+INSERT+UPDATE `entities`,
  `db/migrations/007_webhook_role.sql`) — the same one-txn "tasks+observations+entities" insert
  `agent/nodes/observe.py`'s internal sweep path uses, reimplemented independently
  (`workers/common/incident.py`).

## Prerequisites (all closed)

1. Migrations `006_approver_role.sql` + `007_webhook_role.sql` applied;
   `scripts/bootstrap_approver_role.py` + `scripts/bootstrap_webhook_role.py` run.
2. Three Secrets Manager secrets — `engram/approver-dsn`, `engram/webhook-dsn`,
   `engram/webhook-hmac-secret` — created directly with `engram-deploy`'s credentials (not via
   the bootstrap scripts' own attempt, which still correctly fails under `engram-phase0`).
   **The `EngramCdkDeploy` policy needed widening twice** to get here: once for the first secret
   (scoped to `engram/approver-dsn-*`), and again for the second two, at which point the
   resource was generalized to `engram/*` so a third round-trip won't be needed for future
   secrets under this naming convention. A `LambdaLogsRead` statement was added at the same time
   (`logs:DescribeLogStreams`/`GetLogEvents` on `/aws/lambda/engram-*`) for future debugging.
3. `pip install -r infra/requirements.txt` (`aws-cdk-lib`, `constructs`).
4. `cdk bootstrap aws://532749777349/us-east-1` — done, using `engram-deploy`.
5. **No Docker available in this dev environment.** `infra/build.py` works around this by
   hand-assembling each Lambda's deployment package (`pip install --target` into
   `infra/.build/<name>/`, gitignored) instead of using `aws_lambda_python_alpha.PythonFunction`'s
   Docker-based bundling — safe only because `workers/requirements.txt` is pure-Python
   (`pg8000`); this approach would NOT work for a dependency with a native extension (e.g.
   `psycopg[binary]`).

## Commands (run from this directory)

```
cdk synth      # generates the CloudFormation template locally -- no AWS credentials needed
               # beyond a placeholder account/region; does not touch real infrastructure.
cdk diff       # needs real AWS credentials with read access to the target account.
cdk deploy     # creates/updates REAL, BILLABLE AWS resources. Re-running picks up code changes
               # under any workers/<name> or workers/common directory.
```

All three need `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION` set to the
`engram-deploy` identity's credentials (from `.env`'s `AWS_DEPLOY_ACCESS_KEY_ID`/
`AWS_DEPLOY_SECRET_ACCESS_KEY`), not `engram-phase0`'s.

## IAM notes

- `cloudwatch:GetMetricData`/`cloudwatch:ListMetrics` do NOT support resource-level ARN scoping
  in IAM (a documented AWS limitation) — the metrics Lambda's own role grants these as
  `Resource: "*"`, the actual least-privilege boundary these two actions allow. Stated in
  `infra/engram_infra/approvals_stack.py`'s own comment, not glossed over.
- `POST /webhooks/alerts` deliberately has `api_key_required=False` — LLD §11.2 names a
  different auth scheme for this one route ("HMAC signature"), so API Gateway does no auth at
  all here; `workers/webhooks/handler.py` verifies the signature itself.

**Two real, informative surprises while verifying the live endpoints, neither a bug in this
stack:** (1) the very first request against a freshly created API key returned `403 Forbidden`
at the API Gateway layer — a known AWS propagation delay for new API keys/usage plans (up to
~30s); a retry moments later succeeded. (2) the webhooks route returned a real `502` the first
time it was hit, before its two Secrets Manager secrets existed — exactly the expected failure
mode for a Lambda that can't fetch a secret it needs, not a code bug; resolved once the secrets
were created and the IAM policy widened to allow it.

## Retrieving the API key value again later

```
aws apigateway get-api-key --api-key <ApiKeyId output> --include-value --region us-east-1
```
(`ApiKeyId` is in the stack's CloudFormation outputs, alongside `ApiUrl`/`MetricsApiUrl`/
`WebhooksApiUrl`.)
