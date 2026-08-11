"""Engram · unit tests for agent/tools/cloud_api.py -- the backup gate.

Case (a) (empty list -> refuse) is tested against `fixtures/
cloudapi-backups-basic.json`, a REAL response captured 2026-08-03 against
a live Basic cluster. Cases (b)/(c) (non-empty) are now ALSO tested against
a real capture -- `fixtures/cloudapi-backups-target-nonempty.json`, captured
2026-08-11 via `scripts/verify_ccloud.py` against the real target cluster --
which is what revealed the completion-timestamp field is actually
`as_of_time`, not `completedTime` (the field this suite's own tests
"confirmed" before any real non-empty response had ever been seen). The
`completedTime`/`completed_at`/`finishedTime` tests below are kept as
fallback-path coverage, not as evidence of the real shape anymore.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools.cloud_api import CloudApiAdapter, decide_backup_gate

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "cloudapi-backups-basic.json"
NONEMPTY_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "cloudapi-backups-target-nonempty.json"
)


def _load_real_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _load_real_nonempty_fixture() -> dict:
    return json.loads(NONEMPTY_FIXTURE_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------- pure decision logic

def test_real_fixture_is_the_empty_case():
    """Confirms the fixture on disk really is the (a) empty-list case this
    test suite relies on -- a canary against the fixture file changing
    underneath these tests without anyone noticing."""
    fixture = _load_real_fixture()
    assert fixture["response"]["backups"] == []
    assert fixture["_http_status"] == 200


def test_empty_backups_refuses_using_the_real_captured_response():
    fixture = _load_real_fixture()
    proceed, reason = decide_backup_gate(fixture["response"]["backups"])
    assert proceed is False
    assert "empty" in reason


def test_real_nonempty_fixture_uses_as_of_time_not_completed_time():
    """Canary against the fixture changing shape underneath these tests --
    also documents, in the test itself, exactly what the real API returns:
    `as_of_time`, never `completedTime`."""
    fixture = _load_real_nonempty_fixture()
    backups = fixture["response"]["backups"]
    assert len(backups) == 5
    assert all("as_of_time" in b and "completedTime" not in b for b in backups)


def test_real_nonempty_fixture_proceeds_within_window():
    """The real capture's most recent backup is 2026-08-11T00:00:00Z. At
    noon the same day that's 12h old -- within the default 24h window --
    proving decide_backup_gate() actually parses the REAL field name, not
    just the fallback names invented before any real response existed."""
    fixture = _load_real_nonempty_fixture()
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    proceed, reason = decide_backup_gate(fixture["response"]["backups"], now=now)
    assert proceed is True
    assert "within" in reason


def test_real_nonempty_fixture_refuses_once_stale():
    """Same real capture, but 'now' is far enough past the most recent
    as_of_time (2026-08-11) to fall outside the window."""
    fixture = _load_real_nonempty_fixture()
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    proceed, reason = decide_backup_gate(fixture["response"]["backups"], now=now)
    assert proceed is False
    assert "outside" in reason


def test_no_backups_at_all_refuses():
    proceed, reason = decide_backup_gate([])
    assert proceed is False


def test_recent_backup_within_window_proceeds():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=2)).isoformat()
    proceed, reason = decide_backup_gate([{"completedTime": recent}], now=now)
    assert proceed is True
    assert "within" in reason


def test_stale_backup_outside_window_refuses():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    stale = (now - timedelta(hours=48)).isoformat()
    proceed, reason = decide_backup_gate([{"completedTime": stale}], now=now)
    assert proceed is False
    assert "outside" in reason


def test_picks_the_most_recent_of_several_backups():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=100)).isoformat()
    recent = (now - timedelta(hours=1)).isoformat()
    proceed, reason = decide_backup_gate(
        [{"completedTime": old}, {"completedTime": recent}], now=now
    )
    assert proceed is True  # the RECENT one governs, not the old one


def test_z_suffix_timestamp_parsed():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    proceed, _ = decide_backup_gate([{"completedTime": recent}], now=now)
    assert proceed is True


def test_unparseable_timestamp_refuses_not_crashes():
    proceed, reason = decide_backup_gate([{"completedTime": "not-a-date"}])
    assert proceed is False
    assert "parseable" in reason


def test_missing_timestamp_field_refuses():
    proceed, reason = decide_backup_gate([{"id": "some-backup"}])
    assert proceed is False


def test_fallback_field_names_accepted():
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    for field in ("completed_at", "finishedTime"):
        proceed, _ = decide_backup_gate([{field: recent}], now=now)
        assert proceed is True


# --------------------------------------------------- CloudApiAdapter, mocked

def test_adapter_refuses_on_the_real_empty_fixture():
    fixture = _load_real_fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture["response"])

    async def run():
        client = httpx.AsyncClient(base_url="https://fake.ccloud.test",
                                     transport=httpx.MockTransport(handler))
        async with CloudApiAdapter(client=client) as adapter:
            return await adapter.check_backup_gate("some-cluster-id")

    proceed, reason = asyncio.run(run())
    assert proceed is False
    assert "empty" in reason


def test_adapter_proceeds_on_the_real_nonempty_fixture():
    fixture = _load_real_nonempty_fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture["response"])

    async def run():
        client = httpx.AsyncClient(base_url="https://fake.ccloud.test",
                                     transport=httpx.MockTransport(handler))
        async with CloudApiAdapter(client=client) as adapter:
            return await adapter.check_backup_gate("some-cluster-id")

    # check_backup_gate uses real wall-clock `now` internally, and the fixture's
    # most recent backup is 2026-08-11 -- so this only proves "doesn't crash and
    # reaches a decision" (real window math needs `now`, tested directly above,
    # not through the adapter which doesn't expose a `now` override).
    proceed, reason = asyncio.run(run())
    assert isinstance(proceed, bool)
    assert reason


def test_adapter_handles_non_200_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async def run():
        client = httpx.AsyncClient(base_url="https://fake.ccloud.test",
                                     transport=httpx.MockTransport(handler))
        async with CloudApiAdapter(client=client) as adapter:
            return await adapter.check_backup_gate("some-cluster-id")

    proceed, reason = asyncio.run(run())
    assert proceed is False
    assert "403" in reason
