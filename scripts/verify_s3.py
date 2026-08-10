#!/usr/bin/env python3
"""Engram · PHASE 0 · S3 artifact store verification (LLD T9c).  Role: [PLUMBER]

Closes the AWS-anchor half of Phase 0 evidence (docs/external-constraints.md
§5, invariant #11: rows hold the s3:// URI + content hash, never the blob).
Answers what is still UNVERIFIED there:

    A. auth — does the real AWS key reach S3 as this identity?
    B. put — can we write an object to engram-agent-artifacts?
    C. get + hash — does the object read back byte-identical (sha256 match)?
    D. IAM scope — is the identity's access actually scoped to this bucket,
       or does it have broader s3:* (which invariant #11 forbids)?
    E. cleanup — delete the probe object so the bucket isn't left with test
       litter (this script's own writes are the only thing it deletes).

Deliberately defensive, same posture as verify_ollama.py / verify_cohere.py:
prints what boto3 actually returned rather than assuming AWS's documented
shape. Never touches any object this script did not itself create.

    pip install -r scripts/requirements-verify.txt
    # .env: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, AWS_S3_BUCKET_NAME
    python scripts/verify_s3.py 2>&1 | tee docs/_raw/p0-s3.log

Exit 0 only if A, B and C pass.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import time
import uuid

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    sys.exit("FATAL: boto3 not installed. pip install -r scripts/requirements-verify.txt")

REGION = os.environ.get("AWS_REGION", "us-east-1")
BUCKET = os.environ.get("AWS_S3_BUCKET_NAME", os.environ.get("ENGRAM_S3_BUCKET", "engram-agent-artifacts"))
PROBE_KEY = f"phase0-probes/verify_s3-{uuid.uuid4().hex[:12]}.txt"
PROBE_BODY = f"Engram Phase 0 S3 probe — {uuid.uuid4().hex}".encode("utf-8")
PROBE_HASH = hashlib.sha256(PROBE_BODY).hexdigest()

RULE = "-" * 72
results: list[tuple[str, str]] = []


def head(t: str) -> None:
    print(f"\n{RULE}\n{t}\n{RULE}")


def record(k: str, v: str) -> None:
    results.append((k, v))
    print(f"  >> {k}: {v}")


def probe_auth() -> tuple[boto3.Session, bool]:
    head(f"PROBE A  auth — which identity, which region")
    session = boto3.Session(region_name=REGION)
    try:
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        print(f"  Account : {ident.get('Account')}")
        print(f"  ARN     : {ident.get('Arn')}")
        record("auth", "OK")
        record("IAM identity", ident.get("Arn", "unknown"))
        return session, True
    except NoCredentialsError:
        record("auth", "NO CREDENTIALS — set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env")
        return session, False
    except ClientError as exc:
        record("auth", f"FAIL — {exc.response.get('Error', {}).get('Code', type(exc).__name__)}")
        return session, False


def probe_put(s3) -> bool:
    head(f"PROBE B  put — s3://{BUCKET}/{PROBE_KEY}")
    t0 = time.perf_counter()
    try:
        s3.put_object(Bucket=BUCKET, Key=PROBE_KEY, Body=PROBE_BODY, ContentType="text/plain")
        dt = time.perf_counter() - t0
        print(f"  PutObject OK in {dt:.2f}s, {len(PROBE_BODY)} bytes")
        record("put", "OK")
        record("put latency", f"{dt:.2f}s")
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        print(f"  {exc}")
        record("put", f"FAIL — {code}")
        if code == "NoSuchBucket":
            print(f"  Bucket '{BUCKET}' does not exist in region {REGION} — create it before re-running.")
        elif code in ("AccessDenied", "AccessDeniedException"):
            print("  IAM policy does not grant s3:PutObject on this bucket/prefix.")
        return False


def probe_get_and_hash(s3) -> bool:
    head("PROBE C  get + hash — byte-identical round-trip?")
    t0 = time.perf_counter()
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=PROBE_KEY)
        body = obj["Body"].read()
        dt = time.perf_counter() - t0
    except ClientError as exc:
        record("get", f"FAIL — {exc.response.get('Error', {}).get('Code', type(exc).__name__)}")
        return False
    got_hash = hashlib.sha256(body).hexdigest()
    print(f"  GetObject OK in {dt:.2f}s, {len(body)} bytes")
    print(f"  expected sha256 : {PROBE_HASH}")
    print(f"  got sha256      : {got_hash}")
    match = got_hash == PROBE_HASH
    record("get + hash round-trip", "OK — hashes match" if match else "MISMATCH — content corrupted in transit")
    record("get latency", f"{dt:.2f}s")
    return match


def probe_iam_scope(s3) -> None:
    head("PROBE D  IAM scope — is access actually scoped to this bucket?")
    # Best-effort, informational only: try an operation against a bucket we do
    # NOT own. A bucket-scoped policy should deny this; a broad s3:* policy
    # would (dangerously) allow it. Uses a name that almost certainly isn't ours.
    decoy = f"engram-should-not-exist-{uuid.uuid4().hex[:8]}"
    try:
        s3.head_bucket(Bucket=decoy)
        record("IAM scope check", f"UNEXPECTED — head_bucket on unrelated bucket '{decoy}' did not raise")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        if code in ("403", "AccessDenied", "Forbidden"):
            record("IAM scope check", "OK — denied on an unrelated bucket, as expected for a scoped policy")
        elif code in ("404", "NoSuchBucket"):
            record("IAM scope check", "inconclusive — decoy bucket name simply doesn't exist (expected, harmless)")
        else:
            record("IAM scope check", f"inconclusive — {code}")


def probe_cleanup(s3, put_ok: bool) -> None:
    head("PROBE E  cleanup — delete the probe object")
    if not put_ok:
        print("  Skipped — put never succeeded, nothing to delete.")
        return
    try:
        s3.delete_object(Bucket=BUCKET, Key=PROBE_KEY)
        record("cleanup", f"OK — deleted s3://{BUCKET}/{PROBE_KEY}")
    except ClientError as exc:
        record("cleanup", f"FAIL — {exc.response.get('Error', {}).get('Code', type(exc).__name__)} "
                          f"(manual delete needed: s3://{BUCKET}/{PROBE_KEY})")


def main() -> int:
    print("Engram Phase 0 · S3 artifact store verification (LLD T9c)")
    print(f"  region : {REGION}")
    print(f"  bucket : {BUCKET}")
    print(f"  key id : {'set (…' + os.environ.get('AWS_ACCESS_KEY_ID', '')[-4:] + ')' if os.environ.get('AWS_ACCESS_KEY_ID') else 'UNSET'}")

    session, auth_ok = probe_auth()
    put_ok = get_ok = False
    if auth_ok:
        s3 = session.client("s3")
        put_ok = probe_put(s3)
        get_ok = probe_get_and_hash(s3) if put_ok else False
        probe_iam_scope(s3)
        probe_cleanup(s3, put_ok)

    head("S3 PROBE RESULT  — paste into docs/phase0-verification.md")
    width = max((len(k) for k, _ in results), default=10)
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    gate = auth_ok and put_ok and get_ok
    print(f"\n  GATE (auth + put + get/hash match): {'PASS' if gate else 'FAIL'}")
    if not gate:
        print(
            "\n  Triage:\n"
            "    NO CREDENTIALS      -> set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env\n"
            "    NoSuchBucket        -> create 'engram-agent-artifacts' in this account/region\n"
            "    AccessDenied on put -> IAM policy needs s3:PutObject scoped to this bucket ARN\n"
            "    AccessDenied on get -> IAM policy needs s3:GetObject scoped to this bucket ARN"
        )
    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
