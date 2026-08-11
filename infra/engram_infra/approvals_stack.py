"""Engram · infra/engram_infra/approvals_stack.py — API Gateway + Lambda for POST /approvals/{id}.

design/02-low-level-design.md §11.2 / HLD §5.6: "Mutations (approve/reject) -> API Gateway ->
Lambda -> memory cluster." This is the ONLY write path into the memory cluster that the dashboard
(or anything client-facing) ever touches -- `engram_reader` (the dashboard's own SSE credential)
is SELECT-only by construction; this Lambda holds the separate, narrowly-scoped
`engram_approver` credential (SELECT+UPDATE on `approvals` only, `db/migrations/
006_approver_role.sql`) instead.

IaC choice (AWS CDK, Python) matches HLD §6's locked stack table: "IaC | AWS CDK (Python) | one
language across repo."
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from build import build_approvals_package

DEFAULT_SECRET_NAME = "engram/approver-dsn"


class ApprovalsStack(cdk.Stack):
    """One Lambda, one REST API resource (`POST /approvals/{approval_id}`), API-key + usage-plan
    auth, CORS scoped to the dashboard's own origin -- not `*` (LLD §11.2: "CORS for dashboard
    origin", singular, on purpose).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        dashboard_origin: str,
        approver_secret_name: str = DEFAULT_SECRET_NAME,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Imports an ALREADY-EXISTING secret -- this stack never creates or writes the DSN value
        # itself (scripts/bootstrap_approver_role.py does that, the same script that sets the SQL
        # role's password). Keeping secret creation and infra provisioning as separate concerns
        # means CDK never needs write access to Secrets Manager, only read, and the CDK template/
        # state never has to see the actual password.
        approver_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "ApproverDsnSecret", approver_secret_name
        )

        package_path = build_approvals_package()

        fn = lambda_.Function(
            self,
            "ApprovalsFunction",
            function_name="engram-approvals",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="approvals.handler.handler",
            code=lambda_.Code.from_asset(package_path),
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "ENGRAM_APPROVER_SECRET_NAME": approver_secret_name,
                "ENGRAM_DASHBOARD_ORIGIN": dashboard_origin,
            },
        )
        # Least-privilege IAM, same discipline as every SQL role in this project: read access to
        # exactly this one secret ARN, nothing broader (invariant-style rule carried into IAM,
        # not just SQL grants -- CLAUDE.md's own "s3:PutObject/GetObject scoped to one bucket ARN,
        # never s3:*" pattern, applied here to Secrets Manager instead of S3).
        approver_secret.grant_read(fn)

        api = apigateway.RestApi(
            self,
            "ApprovalsApi",
            rest_api_name="engram-approvals",
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=[dashboard_origin],
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Content-Type", "X-Api-Key"],
            ),
        )
        approval_resource = api.root.add_resource("approvals").add_resource("{approval_id}")
        approval_resource.add_method(
            "POST",
            apigateway.LambdaIntegration(fn, proxy=True),
            api_key_required=True,
        )

        plan = api.add_usage_plan(
            "ApprovalsUsagePlan",
            name="engram-approvals-usage-plan",
            throttle=apigateway.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        key = api.add_api_key("ApprovalsApiKey")
        plan.add_api_key(key)
        plan.add_api_stage(stage=api.deployment_stage)

        cdk.CfnOutput(self, "ApiUrl", value=f"{api.url}approvals/{{approval_id}}")
        cdk.CfnOutput(
            self,
            "ApiKeyId",
            value=key.key_id,
            description="Retrieve the actual key value with: "
            "aws apigateway get-api-key --api-key <this-id> --include-value",
        )
