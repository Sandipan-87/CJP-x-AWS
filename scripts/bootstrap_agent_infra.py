#!/usr/bin/env python3
"""Engram · one-time AWS-side provisioning for agent/main.py's ECS deployment.  [PLUMBER]

Creates exactly two things `infra/engram_infra/agent_stack.py` IMPORTS but never
creates itself -- the same "CDK imports, a script provisions" split this project
already uses for every Secrets Manager secret (`infra/README.md`'s own
`approvals_stack.py` comment: "This stack never creates or writes the DSN value
itself"), extended here to a second AWS resource type (ECR) for the same reason:
CDK never needs create/write access to either, only read/pull access at deploy
time.

  1. The ECR repository `engram-agent` -- `.github/workflows/build-agent-image.yml`
     pushes into it; `cdk deploy` only ever references it by name.
  2. The Secrets Manager secret `engram/agent-secrets` (one JSON blob, one ARN --
     matches this project's own "fewer secrets, fewer IAM statements" preference
     over one-secret-per-value) holding the values agent/main.py's ECS task
     definition injects as container env vars via `ecs.Secret.from_secrets_
     manager(secret, field=...)`: ENGRAM_MEMORY_DSN, ENGRAM_TARGET_PROBE_DSN,
     ENGRAM_TARGET_OPERATOR_DSN, ENGRAM_TARGET_DSN (fallback, same as every
     other consumer of these three DSNs in this repo), COHERE_API_KEY,
     OLLAMA_API_KEY, CCLOUD_TOKEN.

**Expected to fail under `engram-phase0`** (the only credential normally present
in `.env`) -- that identity is deliberately S3-only, the same least-privilege-
working-as-designed shape every prior AWS-side provisioning gap in this project
has hit (S3 bucket creation, every Secrets Manager write). Run this under
`engram-deploy`'s credentials instead (temporarily exported, never written to
`.env` -- matches how the approver/webhook secrets were created in Sessions 32/33).

    python scripts/bootstrap_agent_infra.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

import boto3
from botocore.exceptions import ClientError

ECR_REPOSITORY_NAME = "engram-agent"
SECRET_NAME = "engram/agent-secrets"
SECRET_ENV_KEYS = (
    "ENGRAM_MEMORY_DSN",
    "ENGRAM_TARGET_PROBE_DSN",
    "ENGRAM_TARGET_OPERATOR_DSN",
    "ENGRAM_TARGET_DSN",
    "COHERE_API_KEY",
    "OLLAMA_API_KEY",
    "CCLOUD_TOKEN",
)

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


def ensure_ecr_repository(region: str) -> bool:
    ecr = boto3.client("ecr", region_name=region)
    try:
        ecr.create_repository(
            repositoryName=ECR_REPOSITORY_NAME,
            imageScanningConfiguration={"scanOnPush": True},
            imageTagMutability="MUTABLE",
        )
        record("ECR repository created", True, ECR_REPOSITORY_NAME)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "RepositoryAlreadyExistsException":
            record("ECR repository already exists", True, ECR_REPOSITORY_NAME)
            return True
        record("ECR repository creation failed", False, f"{type(exc).__name__}: {exc}")
        return False


def ensure_agent_secret(region: str) -> bool:
    missing = [k for k in SECRET_ENV_KEYS if not os.environ.get(k)]
    if missing:
        record(
            "all required env vars present in .env", False,
            f"missing: {missing} -- fill these in .env before running this script",
        )
        return False

    payload = json.dumps({k: os.environ[k] for k in SECRET_ENV_KEYS})
    secretsmanager = boto3.client("secretsmanager", region_name=region)
    try:
        secretsmanager.create_secret(Name=SECRET_NAME, SecretString=payload)
        record("Secrets Manager secret created", True, SECRET_NAME)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceExistsException":
            secretsmanager.put_secret_value(SecretId=SECRET_NAME, SecretString=payload)
            record("Secrets Manager secret already existed -- value updated", True, SECRET_NAME)
            return True
        record("Secrets Manager secret provisioning failed", False, f"{type(exc).__name__}: {exc}")
        return False


def main() -> int:
    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        identity = boto3.client("sts", region_name=region).get_caller_identity()
        print(f"Running as: {identity['Arn']}")
    except Exception as exc:  # noqa: BLE001
        record("AWS credentials resolvable", False, f"{type(exc).__name__}: {exc}")
        return 1

    ecr_ok = ensure_ecr_repository(region)
    secret_ok = ensure_agent_secret(region)

    print(f"\n{RULE}\nRESULT\n{RULE}")
    for name, detail in results:
        print(f"  {name}: {detail}")
    all_ok = ecr_ok and secret_ok
    print(f"\n{'ALL OK' if all_ok else 'SOME FAILED (see above -- expected under engram-phase0, see module docstring)'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
