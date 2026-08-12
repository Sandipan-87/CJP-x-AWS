"""Engram · infra/engram_infra/approvals_stack.py — the dashboard-facing API Gateway.

design/02-low-level-design.md §11.2 / HLD §5.6. One REST API, three routes, added
incrementally in three sessions -- the class name (`EngramApiStack`) and this file's name have
diverged on purpose: renaming the Python class doesn't touch the deployed CloudFormation stack's
identity (that's `infra/app.py`'s `EngramApprovalsStack` construct id, unchanged since the first
deploy), so `cdk deploy` updates the existing stack in place rather than replacing it.

  - `POST /approvals/{approval_id}` (API key) -- LLD §11.2's mutation path. Only write path into
    the memory cluster anything client-facing ever touches; `engram_reader` (the dashboard's SSE
    credential) is SELECT-only by construction.
  - `GET /metrics?window=1h` (API key) -- CloudWatch GetMetricData for LLD §12's metrics.
  - `POST /webhooks/alerts` (HMAC signature, NOT an API key -- LLD's own auth column names a
    different scheme for this one route specifically, for external alert sources that speak
    HMAC, not API-Gateway API keys).

IaC choice (AWS CDK, Python) matches HLD §6's locked stack table: "IaC | AWS CDK (Python) | one
language across repo."
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from build import build_approvals_package, build_metrics_package, build_webhooks_package

DEFAULT_APPROVER_SECRET_NAME = "engram/approver-dsn"
DEFAULT_WEBHOOK_DSN_SECRET_NAME = "engram/webhook-dsn"
DEFAULT_WEBHOOK_HMAC_SECRET_NAME = "engram/webhook-hmac-secret"


class EngramApiStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        dashboard_origin: str,
        approver_secret_name: str = DEFAULT_APPROVER_SECRET_NAME,
        webhook_dsn_secret_name: str = DEFAULT_WEBHOOK_DSN_SECRET_NAME,
        webhook_hmac_secret_name: str = DEFAULT_WEBHOOK_HMAC_SECRET_NAME,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        api = apigateway.RestApi(
            self,
            "ApprovalsApi",
            rest_api_name="engram-approvals",
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=[dashboard_origin],
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type", "X-Api-Key", "X-Engram-Signature"],
            ),
        )
        plan = api.add_usage_plan(
            "ApprovalsUsagePlan",
            name="engram-approvals-usage-plan",
            throttle=apigateway.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        key = api.add_api_key("ApprovalsApiKey")
        plan.add_api_key(key)
        plan.add_api_stage(stage=api.deployment_stage)

        self._add_approvals(api, dashboard_origin, approver_secret_name)
        self._add_metrics(api, dashboard_origin)
        self._add_webhooks(api, dashboard_origin, webhook_dsn_secret_name, webhook_hmac_secret_name)

        cdk.CfnOutput(self, "ApiKeyId", value=key.key_id,
                       description="aws apigateway get-api-key --api-key <this-id> --include-value")

    # ---------------------------------------------------------------- approvals

    def _add_approvals(self, api: apigateway.RestApi, dashboard_origin: str, secret_name: str) -> None:
        # Imports an ALREADY-EXISTING secret -- this stack never creates or writes the DSN value
        # itself (scripts/bootstrap_approver_role.py does). Secret creation and infra provisioning
        # stay separate concerns so CDK never needs write access to Secrets Manager.
        secret = secretsmanager.Secret.from_secret_name_v2(self, "ApproverDsnSecret", secret_name)
        fn = lambda_.Function(
            self, "ApprovalsFunction",
            function_name="engram-approvals",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="approvals.handler.handler",
            code=lambda_.Code.from_asset(build_approvals_package()),
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "ENGRAM_APPROVER_SECRET_NAME": secret_name,
                "ENGRAM_DASHBOARD_ORIGIN": dashboard_origin,
            },
        )
        # Least-privilege IAM, same discipline as every SQL role in this project: read access to
        # exactly this one secret ARN, never broader.
        secret.grant_read(fn)

        resource = api.root.add_resource("approvals").add_resource("{approval_id}")
        resource.add_method("POST", apigateway.LambdaIntegration(fn, proxy=True), api_key_required=True)

        cdk.CfnOutput(self, "ApiUrl", value=f"{api.url}approvals/{{approval_id}}")

    # ----------------------------------------------------------------- metrics

    def _add_metrics(self, api: apigateway.RestApi, dashboard_origin: str) -> None:
        fn = lambda_.Function(
            self, "MetricsFunction",
            function_name="engram-metrics",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="metrics.handler.handler",
            code=lambda_.Code.from_asset(build_metrics_package()),
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"ENGRAM_DASHBOARD_ORIGIN": dashboard_origin},
        )
        # cloudwatch:GetMetricData/ListMetrics do NOT support resource-level ARN scoping in IAM
        # (a documented AWS limitation, not a choice made here) -- Resource: "*" is the actual
        # least-privilege boundary these two actions allow; stated, not glossed over.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"],
                resources=["*"],
            )
        )

        resource = api.root.add_resource("metrics")
        resource.add_method("GET", apigateway.LambdaIntegration(fn, proxy=True), api_key_required=True)

        cdk.CfnOutput(self, "MetricsApiUrl", value=f"{api.url}metrics")

    # ---------------------------------------------------------------- webhooks

    def _add_webhooks(
        self, api: apigateway.RestApi, dashboard_origin: str, dsn_secret_name: str, hmac_secret_name: str
    ) -> None:
        dsn_secret = secretsmanager.Secret.from_secret_name_v2(self, "WebhookDsnSecret", dsn_secret_name)
        hmac_secret = secretsmanager.Secret.from_secret_name_v2(self, "WebhookHmacSecret", hmac_secret_name)

        fn = lambda_.Function(
            self, "WebhooksFunction",
            function_name="engram-webhooks",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="webhooks.handler.handler",
            code=lambda_.Code.from_asset(build_webhooks_package()),
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={
                "ENGRAM_WEBHOOK_SECRET_NAME": dsn_secret_name,
                "ENGRAM_WEBHOOK_HMAC_SECRET_NAME": hmac_secret_name,
                "ENGRAM_DASHBOARD_ORIGIN": dashboard_origin,
            },
        )
        dsn_secret.grant_read(fn)
        hmac_secret.grant_read(fn)

        # NOT api_key_required: LLD §11.2 names a DIFFERENT auth scheme for this one route
        # ("HMAC signature", not "API key") -- an external alert source authenticates by
        # computing the right HMAC, not by holding an API-Gateway key. The Lambda itself
        # verifies the signature (workers/webhooks/handler.py); API Gateway does no auth here.
        resource = api.root.add_resource("webhooks").add_resource("alerts")
        resource.add_method("POST", apigateway.LambdaIntegration(fn, proxy=True), api_key_required=False)

        cdk.CfnOutput(self, "WebhooksApiUrl", value=f"{api.url}webhooks/alerts")
