"""Engram · agent/tools/cloud_api.py — CockroachDB Cloud REST API: the backup gate.  [PLUMBER]

design/02-low-level-design.md §5.5 step 1. `GET /api/v1/clusters/{id}/
backups`, **not** `ccloud cluster backup list` (that subcommand does not
exist). This is one of the "never cut" demo beats (CLAUDE.md §9): refuse to
act without a verified recent backup, safe default.

WHAT'S ACTUALLY MEASURED vs ASSUMED, stated plainly:

  - `fixtures/cloudapi-backups-basic.json` (captured 2026-08-03 against the
    real memory cluster on a fresh Basic plan) is REAL evidence:
    `200 {"backups": [], "pagination": null}`. Case (a) — empty list, refuse
    — is exactly what this file's own tests exercise against that real
    capture, not an invented fixture.
  - **CORRECTED 2026-08-11 (Session 29, `scripts/verify_ccloud.py` gate
    PASS): case (b)/(c) is now measured, not assumed, and the original
    guess was WRONG.** A real `CCLOUD_TOKEN` against the real target
    cluster returned a genuinely non-empty response —
    `fixtures/cloudapi-backups-target-nonempty.json` — and the completion
    timestamp field is **`as_of_time`**, not `completedTime`/
    `completed_at`/`finishedTime` (the prior best-guess names, kept below
    as fallbacks now that they're known-wrong for this API version rather
    than removed outright, in case a different endpoint/version ever uses
    one of them). Each entry is `{"id": <uuid>, "as_of_time": <ISO8601>}`
    — no other fields observed.
  - The LIVE network call itself (`CloudApiAdapter.check_backup_gate`) is
    now VERIFIED: `scripts/verify_ccloud.py` confirmed auth (200, not
    401), correct scope (200 on target, a real 403 on memory — the
    opposite of the wrong-scope mistake this file's history already
    records), and the real response shape above. `tests/test_cloud_api.py`
    still also runs the mocked-transport path against both the empty and
    the new non-empty fixture.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_BASE_URL = "https://cockroachlabs.cloud"


def decide_backup_gate(
    backups: list[dict], *, window_hours: float = DEFAULT_WINDOW_HOURS, now: datetime | None = None
) -> tuple[bool, str]:
    """LLD §5.5 step 1's (a)/(b)/(c). Returns `(proceed, reason)` — never
    raises; the caller (`agent/nodes/act_measure.py`) decides what a
    `proceed=False` means (`BackupGateBlocked`).
    """
    if not backups:
        return False, "no backups exist yet (empty list) — safe default is refuse"

    now = now or datetime.now(timezone.utc)
    most_recent: datetime | None = None
    for entry in backups:
        raw = (
            entry.get("as_of_time")  # confirmed real field, scripts/verify_ccloud.py 2026-08-11
            or entry.get("completedTime")
            or entry.get("completed_at")
            or entry.get("finishedTime")
        )
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if most_recent is None or ts > most_recent:
            most_recent = ts

    if most_recent is None:
        return False, "backups exist but none had a parseable completion timestamp"

    age_hours = (now - most_recent).total_seconds() / 3600
    if age_hours > window_hours:
        return False, f"most recent backup is {age_hours:.1f}h old, outside the {window_hours}h window"
    return True, f"most recent backup is {age_hours:.1f}h old, within the {window_hours}h window"


class CloudApiAdapter:
    """Thin `httpx` client for the backups endpoint. See module docstring —
    only the mocked-transport path has run against real captured data;
    the live network path is unverified (no `CCLOUD_TOKEN` provisioned).
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if client is not None:
            self._client = client
            return
        token = token or os.environ.get("CCLOUD_TOKEN")
        if not token:
            raise RuntimeError("CCLOUD_TOKEN not set and no token provided")
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("CCLOUD_API_BASE_URL", DEFAULT_BASE_URL),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "CloudApiAdapter":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def check_backup_gate(
        self, cluster_id: str, *, window_hours: float = DEFAULT_WINDOW_HOURS
    ) -> tuple[bool, str]:
        try:
            r = await self._client.get(f"/api/v1/clusters/{cluster_id}/backups")
        except httpx.TransportError as exc:
            return False, f"transport error checking backup gate: {type(exc).__name__}: {exc}"
        if r.status_code != 200:
            return False, f"backup gate check failed: HTTP {r.status_code}: {r.text[:200]}"
        payload = r.json()
        return decide_backup_gate(payload.get("backups") or [], window_hours=window_hours)
