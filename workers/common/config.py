"""Engram · workers/common/config.py — "env var, else Secrets Manager" resolution.  [PLUMBER]

Same pattern needed by every Lambda in `workers/`: prefer a plain environment variable for local
testing (`scripts/bootstrap_*_role.py` write these to the repo-root `.env`), fall back to AWS
Secrets Manager for the real deployed Lambda (HLD's `secret/engram/*` convention). Pulled out
once both `workers/common/db.py` (DSNs) and `workers/webhooks/handler.py` (the HMAC secret)
needed the identical two-step lookup, rather than duplicating it a third time.
"""

from __future__ import annotations

import os

_cache: dict[str, str] = {}


def resolve_secret(value_env_var: str, secret_name_env_var: str) -> str:
    """`value_env_var` (e.g. `ENGRAM_WEBHOOK_HMAC_SECRET`) is checked first; if unset, fetches
    from Secrets Manager using the secret name in `secret_name_env_var` (e.g.
    `ENGRAM_WEBHOOK_HMAC_SECRET_NAME`). Cached per-process (module-level global) so a warm Lambda
    invocation never re-fetches.
    """
    if value_env_var in _cache:
        return _cache[value_env_var]
    value = os.environ.get(value_env_var)
    if not value:
        secret_name = os.environ.get(secret_name_env_var)
        if not secret_name:
            raise RuntimeError(
                f"neither {value_env_var} nor {secret_name_env_var} is set -- see workers/README.md"
            )
        import boto3  # imported lazily -- not needed at all for local env-var-based testing

        client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        value = client.get_secret_value(SecretId=secret_name)["SecretString"]
    _cache[value_env_var] = value
    return value
