#!/usr/bin/env python3
"""Engram · infra/app.py — CDK app entrypoint. `cd infra && cdk synth` / `cdk deploy`."""

from __future__ import annotations

import os

import aws_cdk as cdk

from engram_infra.agent_stack import EngramAgentStack
from engram_infra.approvals_stack import EngramApiStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

EngramApiStack(
    app,
    "EngramApprovalsStack",  # unchanged construct id -- keeps updating the same deployed stack
    dashboard_origin=os.environ.get("ENGRAM_DASHBOARD_ORIGIN", "http://localhost:3000"),
    env=env,
)

EngramAgentStack(
    app,
    "EngramAgentStack",
    env=env,
)

app.synth()
