# Engram infra — dashboard API Gateway + the agent's SQS/EventBridge/ECS

[PLUMBER]. Two independent CDK stacks in this app now (`infra/app.py`):

- **`EngramApprovalsStack`** (Python class `EngramApiStack`) — `design/02-low-level-design.md`
  §11.2 / HLD §5.6, the dashboard-facing API Gateway (approvals + metrics + webhooks). **DEPLOYED
  LIVE** — see its own section below.
- **`EngramAgentStack`** (Python class `EngramAgentStack`,
  `infra/engram_infra/agent_stack.py`) — the SQS queue, EventBridge sweep rule, and ECS Fargate
  service that actually run `agent/main.py`. **Built + `cdk synth` clean, NOT yet deployed** — see
  its own section below for exactly what's real vs. still needed before `cdk deploy`.

The lifecycle-worker Lambdas (`consolidator`/`decayer`/`embedding_backfill`) named in the LLD's
directory tree are still NOT built, out of scope so far for either stack.

## `EngramAgentStack` — SQS + EventBridge + ECS Fargate for `agent/main.py`

Full design rationale lives in `infra/engram_infra/agent_stack.py`'s own module docstring
(networking choices, why no ALB, why the sweep rule is disabled) — this section covers only the
deploy sequence and what's still needed.

**Real constraint this stack is built around**: no Docker in this dev environment (same one
`infra/build.py` already worked around for the Lambda workers), and `agent/`'s dependencies
(`psycopg[binary]`, transitively `numpy`/`psycopg2-binary`/`greenlet`) can't use that workaround's
pure-Python trick. So this stack does not build an image itself — it imports one from ECR by tag.

**Deploy order (NOT yet executed — asking before any of this touches real AWS, per this
project's own standing rule for consequential/billable actions):**

1. `python scripts/bootstrap_agent_infra.py` (under `engram-deploy` credentials) — creates the
   `engram-agent` ECR repository and the `engram/agent-secrets` Secrets Manager secret (one JSON
   blob: `ENGRAM_MEMORY_DSN`, `ENGRAM_TARGET_PROBE_DSN`, `ENGRAM_TARGET_OPERATOR_DSN`,
   `ENGRAM_TARGET_DSN`, `COHERE_API_KEY`, `OLLAMA_API_KEY`, `CCLOUD_TOKEN`, pulled from local
   `.env`). Expected to fail under `engram-phase0` (S3-only by design), same as every prior
   Secrets Manager gap in this project.
2. Add two NEW GitHub Actions repo secrets — `ENGRAM_ECR_PUSH_AWS_ACCESS_KEY_ID` /
   `ENGRAM_ECR_PUSH_AWS_SECRET_ACCESS_KEY` — for a NEW, narrowly-scoped IAM identity (NOT
   `engram-deploy`: a CI credential pushing one image doesn't need CDK's broader deploy surface).
   Minimum policy: `ecr:GetAuthorizationToken` (`Resource: "*"`, the same kind of no-ARN-scoping
   limitation CloudWatch's metrics actions already have) plus
   `ecr:BatchCheckLayerAvailability`/`PutImage`/`InitiateLayerUpload`/`UploadLayerPart`/
   `CompleteLayerUpload`/`BatchGetImage` scoped to the `engram-agent` repository ARN from step 1.
3. Run `.github/workflows/build-agent-image.yml` (`workflow_dispatch`) — builds & pushes the
   image, tagged `latest` and by git sha, into the repo from step 1.
4. `cdk deploy EngramAgentStack` (from this directory, `engram-deploy` credentials) — creates a
   dedicated `nat_gateways=0` VPC, the FIFO `engram-commands` queue + DLQ, an ECS cluster/
   service/task definition pulling the image from step 3, and a DISABLED 5-minute EventBridge
   sweep rule (see the stack's own docstring for why disabled: no sweep enumerator exists yet).

**`cdk synth EngramAgentStack` needs no AWS credentials** (confirmed — a fresh, dedicated VPC is
created rather than looking up the account's default one, keeping this stack's synth-time
property identical to `EngramApprovalsStack`'s). **A real ordering bug was caught by `cdk synth`
itself on the first attempt**, not assumed correct: granting the Secrets Manager read to
`task_definition.execution_role` before calling `add_container()` failed with a `jsii`
null-deserialization error, because `FargateTaskDefinition` only lazily creates an execution role
once something (the ECR image + log driver) actually needs one. Fixed by moving that grant after
`add_container()`.

**What's real vs. still needed, stated plainly:** the CDK stack, the Dockerfile, and the GitHub
Actions build workflow are all written and `cdk synth`-verified; nothing has been deployed. The
5-minute EventBridge sweep rule, even once deployed, stays `enabled=False` until a real sweep
enumerator exists (LLD §5.1 step 1's still-unimplemented MCP/CloudWatch/ccloud collection legs) —
manually publishing a real message to the queue, or invoking `agent/main.py`'s `process_message()`
directly (as `scripts/smoke_test_main.py` already does, live), remains the only proven way to
exercise the agent today.

---

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
