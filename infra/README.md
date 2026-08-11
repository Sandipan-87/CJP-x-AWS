# Engram infra — API Gateway + Lambda for the approvals mutation path

[PLUMBER]. `design/02-low-level-design.md` §11.2 / HLD §5.6. Only `EngramApprovalsStack` exists
here — the other named Lambda functions (`consolidator`/`decayer`/`embedding_backfill`/
`metrics_proxy`/`alert_ingest`) and any other CDK stacks are NOT built, out of scope for this
chunk.

**DEPLOYED LIVE, 2026-08-12.** Stack `EngramApprovalsStack` in account `532749777349`,
`us-east-1`. Real endpoint + API key retrieved and wired into `dashboard/.env.local`; a real
browser click on the dashboard's Approve button, through this real API Gateway + Lambda, produced
a real `200` and a real `approvals` row update (verified directly: `status='approved'`,
`channel='dashboard'`). Deployed under a dedicated `engram-deploy` IAM user (kept separate from
`engram-phase0`, which stays S3-only), using a custom least-privilege policy scoped to CDK's own
bootstrap resource naming convention — not `AdministratorAccess`.

## What this stack is

`POST /approvals/{approval_id}` → API Gateway (API key + usage plan, CORS scoped to the
dashboard's own origin) → Lambda (`workers/approvals/handler.py`) → CockroachDB, using the
dedicated `engram_approver` role (`db/migrations/006_approver_role.sql`, SELECT+UPDATE on
`approvals` only — the same least-privilege discipline every other role in this project follows).

## Prerequisites (all closed)

1. `db/migrations/006_approver_role.sql` applied, `scripts/bootstrap_approver_role.py` run.
2. The `engram/approver-dsn` Secrets Manager secret — created directly (not via
   `bootstrap_approver_role.py`'s own attempt, which still correctly fails under
   `engram-phase0`) using the `engram-deploy` credentials, which do have
   `secretsmanager:CreateSecret`/`PutSecretValue` scoped to this one secret ARN.
3. `pip install -r infra/requirements.txt` (`aws-cdk-lib`, `constructs`).
4. `cdk bootstrap aws://532749777349/us-east-1` — done, using `engram-deploy`.
5. **No Docker available in this dev environment.** `infra/build.py` works around this by
   hand-assembling the Lambda's deployment package (`pip install --target` into
   `infra/.build/`, gitignored) instead of using `aws_lambda_python_alpha.PythonFunction`'s
   Docker-based bundling — safe only because `workers/requirements.txt` is pure-Python
   (`pg8000`); this approach would NOT work for a dependency with a native extension (e.g.
   `psycopg[binary]`).

## Commands (run from this directory)

```
cdk synth      # generates the CloudFormation template locally -- no AWS credentials needed
               # beyond a placeholder account/region; does not touch real infrastructure.
cdk diff       # needs real AWS credentials with read access to the target account.
cdk deploy     # creates/updates REAL, BILLABLE AWS resources. Already run once (2026-08-12);
               # re-running picks up code changes under workers/approvals or workers/common.
```

All three need `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION` set to the
`engram-deploy` identity's credentials (from `.env`'s `AWS_DEPLOY_ACCESS_KEY_ID`/
`AWS_DEPLOY_SECRET_ACCESS_KEY`), not `engram-phase0`'s.

## IAM setup that made this work

A dedicated IAM user, `engram-deploy`, with a custom managed policy (`EngramCdkDeploy`) scoped
to CDK's default bootstrap qualifier (`hnb659fds`) and this stack's specific resources — not
`AdministratorAccess`. Bootstrap and deploy both succeeded on the first real attempt with this
policy: CloudFormation/S3/ECR/IAM/SSM scoped to CDK's own bootstrap resource ARNs,
`lambda:*`/`apigateway:*` unscoped (their resource ARNs don't exist before first deploy), and
Secrets Manager scoped to the one `engram/approver-dsn` secret ARN.

**One real, informative surprise while verifying the live endpoint:** the very first request
against the freshly created API key returned `403 Forbidden` at the API Gateway layer (never
reached the Lambda) — a known AWS propagation delay for newly created API keys/usage plans
(up to ~30s). A retry moments later succeeded; not a bug in this stack.

## Retrieving the API key value again later

```
aws apigateway get-api-key --api-key <ApiKeyId output> --include-value --region us-east-1
```
(`ApiKeyId` is in the stack's CloudFormation outputs.)
