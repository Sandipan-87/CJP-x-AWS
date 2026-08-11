# Engram infra — API Gateway + Lambda for the approvals mutation path

[PLUMBER]. `design/02-low-level-design.md` §11.2 / HLD §5.6. Only `EngramApprovalsStack` exists
here — the other named Lambda functions (`consolidator`/`decayer`/`embedding_backfill`/
`metrics_proxy`/`alert_ingest`) and any other CDK stacks are NOT built, out of scope for this
chunk.

## What this stack is

`POST /approvals/{approval_id}` → API Gateway (API key + usage plan, CORS scoped to the
dashboard's own origin) → Lambda (`workers/approvals/handler.py`) → CockroachDB, using the
dedicated `engram_approver` role (`db/migrations/006_approver_role.sql`, SELECT+UPDATE on
`approvals` only — the same least-privilege discipline every other role in this project follows).

## Prerequisites

1. `db/migrations/006_approver_role.sql` applied, `scripts/bootstrap_approver_role.py` run — this
   also (re)writes the `engram/approver-dsn` AWS Secrets Manager secret the Lambda reads at
   runtime. **As of this writing, the `engram-phase0` IAM user cannot do this step** —
   `secretsmanager:PutSecretValue`/`CreateSecret` both returned `AccessDenied`, the same
   least-privilege pattern as the S3 bucket and `CCLOUD_TOKEN` gaps before it. Either grant that
   permission or create/populate the secret manually via the console before deploying.
2. `pip install -r infra/requirements.txt` (this repo's own venv — `aws-cdk-lib`, `constructs`).
3. AWS CDK CLI: `npx aws-cdk <command>` needs no separate install; a global `cdk` isn't required.
4. **No Docker available in this dev environment.** `infra/build.py` works around this by hand-
   assembling the Lambda's deployment package (`pip install --target` into `infra/.build/`,
   gitignored) instead of using `aws_lambda_python_alpha.PythonFunction`'s Docker-based bundling
   — safe only because `workers/requirements.txt` is pure-Python (`pg8000`); this approach would
   NOT work for a dependency with a native extension (e.g. `psycopg[binary]`).

## Commands (run from this directory)

```
cdk synth      # generates the CloudFormation template locally -- no AWS credentials needed
               # beyond a placeholder account/region; does not touch real infrastructure.
cdk diff       # needs real AWS credentials with read access to the target account.
cdk deploy     # creates REAL, BILLABLE AWS resources (Lambda, API Gateway, IAM role) --
               # NOT run as part of building this stack; see CLAUDE.md for the explicit
               # go/no-go check before this is ever run.
```

## Known gap, stated not hidden

The current `engram-phase0` IAM user is deliberately least-privilege (S3-only, per earlier
sessions) and almost certainly lacks `iam:CreateRole`, `lambda:CreateFunction`,
`apigateway:POST`, and `cloudformation:*` — everything `cdk deploy` needs. `cdk synth` doesn't
need any of that (it's pure local template generation), so it's safe to run and was run as part
of building this stack. Actually deploying needs either a broader IAM grant or a different,
deploy-capable identity — a manual step, same shape as every other AWS-console gap this project
has hit (S3 bucket creation, IAM policy attachment, `CCLOUD_TOKEN`).
