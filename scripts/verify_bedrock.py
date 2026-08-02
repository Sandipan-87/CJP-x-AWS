#!/usr/bin/env python3
"""Engram · PHASE 0 · P0-B1 — AWS Bedrock access verification.  Role: [BRAINS]

Proves, in the region we are about to build against:
  1. amazon.titan-embed-text-v2:0 is reachable and returns EXACTLY 1024 dims
  2. Claude Sonnet 5 is reachable via the Converse API

Model-ID discovery is deliberately first-class: on Bedrock, Anthropic model IDs
carry an `anthropic.` prefix and may only be callable through a cross-region
inference profile (`us.anthropic.…`). Rather than hardcode a guess, this script
enumerates what your account can actually see, then tries candidates in order
and reports the one that worked. Record that exact string in CLAUDE.md §2.

    pip install -r scripts/requirements-verify.txt
    export AWS_REGION=us-east-1          # or set BEDROCK_REGION
    python scripts/verify_bedrock.py

Exit 0 = both checks passed. Anything else = Phase 0 exit gate is NOT met.
"""

from __future__ import annotations

import json
import os
import sys
import time

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    sys.exit("FATAL: boto3 not installed. pip install -r scripts/requirements-verify.txt")

REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1"

TITAN_MODEL_ID = os.environ.get("BEDROCK_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EXPECTED_DIMS = 1024

# Tried in order. First success wins. Env override always goes first.
CLAUDE_CANDIDATES = [
    c
    for c in (
        os.environ.get("BEDROCK_CLAUDE_MODEL_ID"),
        "anthropic.claude-sonnet-5",
        "us.anthropic.claude-sonnet-5",
        "global.anthropic.claude-sonnet-5",
        "eu.anthropic.claude-sonnet-5",
        "apac.anthropic.claude-sonnet-5",
    )
    if c
]

BOTO_CONFIG = Config(
    retries={"max_attempts": 2, "mode": "standard"},
    read_timeout=90,
    connect_timeout=10,
)

RULE = "-" * 72


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def verdict(ok: bool, label: str, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    return ok


# ---------------------------------------------------------------------------
# 0 — identity + region, so the pasted transcript is self-describing
# ---------------------------------------------------------------------------
def report_context() -> None:
    head(f"CONTEXT  (region = {REGION})")
    try:
        ident = boto3.client("sts", region_name=REGION, config=BOTO_CONFIG).get_caller_identity()
        print(f"  account : {ident['Account']}")
        print(f"  arn     : {ident['Arn']}")
    except (ClientError, BotoCoreError) as exc:
        print(f"  sts:GetCallerIdentity failed: {exc}")
    print(f"  boto3   : {boto3.__version__}")
    print(f"  python  : {sys.version.split()[0]}")


# ---------------------------------------------------------------------------
# 1 — discovery: what is actually visible/enabled in this region
# ---------------------------------------------------------------------------
def discover() -> list[str]:
    """Print visible Anthropic + Titan-embedding models. Returns extra candidates."""
    head("DISCOVERY  bedrock:ListFoundationModels / ListInferenceProfiles")
    extra: list[str] = []
    bedrock = boto3.client("bedrock", region_name=REGION, config=BOTO_CONFIG)

    try:
        models = bedrock.list_foundation_models()["modelSummaries"]
    except (ClientError, BotoCoreError) as exc:
        print(f"  ListFoundationModels failed ({exc}). Continuing on hardcoded candidates.")
        models = []

    print("\n  Anthropic text models visible here:")
    for m in models:
        mid = m["modelId"]
        if not mid.startswith("anthropic."):
            continue
        inf = ",".join(m.get("inferenceTypesSupported", []))
        print(f"    {mid:<58} [{inf}]")
        if "sonnet-5" in mid and mid not in CLAUDE_CANDIDATES:
            extra.append(mid)

    print("\n  Amazon embedding models visible here:")
    for m in models:
        mid = m["modelId"]
        if mid.startswith("amazon.") and "embed" in mid.lower():
            print(f"    {mid:<58} {m.get('outputModalities', [])}")

    try:
        profiles = bedrock.list_inference_profiles()["inferenceProfileSummaries"]
        print("\n  Inference profiles mentioning sonnet-5:")
        found = False
        for p in profiles:
            pid = p.get("inferenceProfileId", "")
            if "sonnet-5" in pid:
                found = True
                print(f"    {pid:<58} {p.get('status')}")
                if pid not in CLAUDE_CANDIDATES:
                    extra.append(pid)
        if not found:
            print("    (none)")
    except (ClientError, BotoCoreError) as exc:
        print(f"\n  ListInferenceProfiles unavailable: {exc}")

    print(
        "\n  NOTE: a model appearing here is NOT proof of access. Model access is\n"
        "  granted per-model in the Bedrock console; the invoke calls below are\n"
        "  the real test."
    )
    return extra


# ---------------------------------------------------------------------------
# 2 — P0-B1a: Titan Text Embeddings V2 @ 1024 dims
# ---------------------------------------------------------------------------
def check_titan() -> bool:
    head(f"CHECK 1  Titan embeddings — {TITAN_MODEL_ID} @ {EXPECTED_DIMS} dims")
    brt = boto3.client("bedrock-runtime", region_name=REGION, config=BOTO_CONFIG)
    probe = "Index scan on orders regressed from 12ms to 4.3s after the stats refresh."
    body = {"inputText": probe, "dimensions": EXPECTED_DIMS, "normalize": True}

    t0 = time.perf_counter()
    try:
        resp = brt.invoke_model(
            modelId=TITAN_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "?")
        return verdict(False, "Titan invoke_model", f"{code}: {exc.response['Error'].get('Message', exc)}")
    except BotoCoreError as exc:
        return verdict(False, "Titan invoke_model", str(exc))
    elapsed_ms = (time.perf_counter() - t0) * 1000

    payload = json.loads(resp["body"].read())
    vec = payload.get("embedding")
    if not isinstance(vec, list):
        return verdict(False, "Titan response shape", f"no 'embedding' list; keys={list(payload)}")

    dims = len(vec)
    print(f"  latency        : {elapsed_ms:.0f} ms")
    print(f"  inputTextTokens: {payload.get('inputTextTokenCount')}")
    print(f"  dimensions     : {dims}")
    print(f"  first 5 values : {[round(v, 6) for v in vec[:5]]}")
    l2 = sum(v * v for v in vec) ** 0.5
    print(f"  L2 norm        : {l2:.6f}   (normalize=true -> expect ~1.0)")

    ok = verdict(dims == EXPECTED_DIMS, f"vector length == {EXPECTED_DIMS}", f"got {dims}")
    if ok and abs(l2 - 1.0) > 1e-3:
        print("  WARN: not unit-norm despite normalize=true — note this; cosine still works.")
    if not ok:
        print(
            "  >>> BLOCKING: CLAUDE.md invariant #2 pins VECTOR(1024). Either fix the\n"
            "  >>> `dimensions` request field or change the invariant — not both silently."
        )
    return ok


# ---------------------------------------------------------------------------
# 3 — P0-B1b: Claude Sonnet 5 via Converse
# ---------------------------------------------------------------------------
def check_claude(extra_candidates: list[str]) -> bool:
    head("CHECK 2  Claude Sonnet 5 — bedrock-runtime Converse")
    brt = boto3.client("bedrock-runtime", region_name=REGION, config=BOTO_CONFIG)

    candidates: list[str] = []
    for c in CLAUDE_CANDIDATES + extra_candidates:
        if c not in candidates:
            candidates.append(c)
    print(f"  candidate order: {candidates}\n")

    # No temperature/top_p/top_k: Sonnet 5 rejects non-default sampling params.
    # maxTokens is generous because adaptive thinking is ON BY DEFAULT on
    # Sonnet 5 and shares the output budget with the visible answer.
    for mid in candidates:
        t0 = time.perf_counter()
        try:
            resp = brt.converse(
                modelId=mid,
                messages=[{"role": "user", "content": [{"text": "Reply with exactly: OK"}]}],
                inferenceConfig={"maxTokens": 1024},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "?")
            msg = exc.response.get("Error", {}).get("Message", "")
            print(f"  {mid:<48} {code}: {msg[:90]}")
            continue
        except BotoCoreError as exc:
            print(f"  {mid:<48} transport error: {exc}")
            continue
        elapsed_ms = (time.perf_counter() - t0) * 1000

        blocks = resp["output"]["message"]["content"]
        text = "".join(b["text"] for b in blocks if "text" in b).strip()
        usage = resp.get("usage", {})
        print(f"  {mid:<48} OK  ({elapsed_ms:.0f} ms)")
        print(f"\n  RESOLVED MODEL ID -> {mid}   <-- record this in CLAUDE.md §2")
        print(f"  stopReason : {resp.get('stopReason')}")
        print(f"  usage      : in={usage.get('inputTokens')} out={usage.get('outputTokens')}")
        print(f"  block types: {[k for b in blocks for k in b]}")
        print(f"  text       : {text!r}")
        if resp.get("stopReason") == "max_tokens":
            print("  WARN: truncated at maxTokens — thinking consumed the budget. Raise it.")
        return verdict(True, "Claude Sonnet 5 reachable via Converse", mid)

    verdict(False, "Claude Sonnet 5 reachable via Converse", "every candidate failed")
    print(
        "\n  Triage, in order:\n"
        "    AccessDeniedException     -> request model access in the Bedrock console\n"
        "                                 (Model access) for THIS region, then retry.\n"
        "    ValidationException on a\n"
        "      bare anthropic.* id     -> the model is on-demand-ineligible here; use the\n"
        "                                 us./eu./apac. inference-profile id from DISCOVERY.\n"
        "    ResourceNotFoundException -> Sonnet 5 is not in this region. Switch region NOW\n"
        "                                 (roadmap P0-B1: 'switch region before anything is\n"
        "                                 built against the wrong one').\n"
        "    Anything else             -> override with BEDROCK_CLAUDE_MODEL_ID=<id> and rerun."
    )
    return False


def main() -> int:
    print("Engram Phase 0 · P0-B1 · Bedrock access verification")
    report_context()
    extra = discover()
    titan_ok = check_titan()
    claude_ok = check_claude(extra)

    head("P0-B1 RESULT")
    print(f"  region                 : {REGION}")
    print(f"  Titan V2 @ 1024 dims   : {'PASS' if titan_ok else 'FAIL'}")
    print(f"  Claude Sonnet 5 access : {'PASS' if claude_ok else 'FAIL'}")
    both = titan_ok and claude_ok
    print(f"\n  P0-B1 {'PASSES' if both else 'FAILS'} — paste this whole transcript into docs/phase0-verification.md")
    return 0 if both else 1


if __name__ == "__main__":
    sys.exit(main())
