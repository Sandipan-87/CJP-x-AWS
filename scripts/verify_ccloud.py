#!/usr/bin/env python3
"""Engram · CCLOUD_TOKEN verification — closes docs/blocked-register.md §8.  Role: [PLUMBER]

The backup gate (`agent/tools/cloud_api.py`, LLD §5.5 step 1) has only ever run against a
mocked transport + a real fixture captured 2026-08-03 (`fixtures/cloudapi-backups-basic.json`).
The live network call has been unverified because no `CCLOUD_TOKEN` existed. This script closes
that the same way `verify_ollama.py`/`verify_cohere.py`/`verify_s3.py` closed theirs: hit the
REAL endpoint with the REAL key and print exactly what comes back, rather than assume the
documented shape holds on this account/tier.

Checks:
    A. auth        — does CCLOUD_TOKEN authenticate against the Cloud REST API at all?
    B. scope       — 200 on the TARGET cluster specifically. LLD §2 names a KNOWN historical
                      mistake worth re-checking here: design/02-low-level-design.md line ~190
                      records an earlier key that 403'd on target and 200'd on memory (scoped to
                      the wrong cluster). This script probes BOTH cluster IDs so that mistake, if
                      repeated, is caught immediately rather than discovered later mid-demo.
    C. shape       — is the response `{"backups": [...]}`  as `cloud_api.py` assumes, or
                      something else? Printed raw, not assumed.
    D. decision    — runs the real response through the same `decide_backup_gate()` the agent
                      itself uses, so this script proves the actual decision path, not just
                      connectivity.

    pip install -r scripts/requirements-verify.txt
    # .env: CCLOUD_TOKEN, ENGRAM_TARGET_CLUSTER_ID, ENGRAM_MEMORY_CLUSTER_ID
    python scripts/verify_ccloud.py

Exit 0 only if A+B+C pass against the TARGET cluster — that's what actually unblocks the live
leg of `agent/tools/cloud_api.py`.
"""

from __future__ import annotations

import asyncio
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

import httpx

from agent.tools.cloud_api import DEFAULT_BASE_URL, decide_backup_gate

RULE = "-" * 72
results: list[tuple[str, bool, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {k}{': ' + detail if detail else ''}")


async def _probe_cluster(client: httpx.AsyncClient, cluster_id: str, label: str) -> tuple[int, dict | None]:
    try:
        r = await client.get(f"/api/v1/clusters/{cluster_id}/backups")
    except httpx.TransportError as exc:
        print(f"  {label} ({cluster_id}): TRANSPORT ERROR {type(exc).__name__}: {exc}")
        return -1, None
    print(f"  {label} ({cluster_id}): HTTP {r.status_code}")
    try:
        body = r.json()
        print(f"    body: {json.dumps(body)[:300]}")
    except ValueError:
        body = None
        print(f"    body (non-JSON): {r.text[:200]}")
    return r.status_code, body


async def main() -> int:
    token = os.environ.get("CCLOUD_TOKEN")
    target_cluster_id = os.environ.get("ENGRAM_TARGET_CLUSTER_ID")
    memory_cluster_id = os.environ.get("ENGRAM_MEMORY_CLUSTER_ID")

    if not token:
        print("CCLOUD_TOKEN not set in .env — nothing to verify. See CLAUDE.md §8 row 8 / "
              "docs/blocked-register.md §8 for the provisioning steps.")
        return 1
    if not target_cluster_id:
        print("ENGRAM_TARGET_CLUSTER_ID not set — cannot probe the target cluster.")
        return 1

    async with httpx.AsyncClient(
        base_url=os.environ.get("CCLOUD_API_BASE_URL", DEFAULT_BASE_URL),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as client:
        print(f"{RULE}\nA/B — auth + scope: probing TARGET cluster (the one that must work)\n{RULE}")
        target_status, target_body = await _probe_cluster(client, target_cluster_id, "TARGET")
        record("A. token authenticates (not 401)", target_status not in (401, -1),
               f"HTTP {target_status}")
        record("B. scoped correctly to the TARGET cluster (200, not 403)", target_status == 200,
               f"HTTP {target_status}")

        if memory_cluster_id:
            print(f"\n{RULE}\nInformational — probing MEMORY cluster too "
                  f"(catches the historical wrong-scope mistake LLD line ~190 records)\n{RULE}")
            mem_status, _ = await _probe_cluster(client, memory_cluster_id, "MEMORY")
            if mem_status == 200 and target_status != 200:
                print("  NOTE: 200 on memory, not-200 on target — this is the EXACT wrong-scope "
                      "mistake already seen once (2026-08-03). Re-scope the key to the target "
                      "cluster specifically.")

        if target_status == 200 and isinstance(target_body, dict):
            has_backups_key = "backups" in target_body
            record("C. response shape is {'backups': [...]}", has_backups_key,
                   f"keys={list(target_body.keys())}")
            if has_backups_key:
                backups = target_body["backups"] or []
                proceed, reason = decide_backup_gate(backups)
                print(f"\n{RULE}\nD — real decide_backup_gate() outcome\n{RULE}")
                print(f"  backups: {len(backups)} entr{'y' if len(backups) == 1 else 'ies'}")
                print(f"  proceed={proceed}, reason={reason!r}")
                if not backups:
                    print("  This is the refusal demo beat (CLAUDE.md §9: 'never claim the "
                          "allow-path was tested unless it was') — empty list -> safe default refuse.")
        else:
            record("C. response shape is {'backups': [...]}", False,
                   f"cannot check shape, HTTP {target_status}")

    print(f"\n{RULE}\nRESULT\n{RULE}")
    failures = [k for k, ok, _ in results if not ok]
    for k, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {k}")
    print(f"\n  {len(results) - len(failures)}/{len(results)} checks passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
