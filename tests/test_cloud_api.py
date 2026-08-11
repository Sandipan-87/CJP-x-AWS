"""Engram · unit tests for agent/tools/cloud_api.py -- the backup gate.

Case (a) (empty list -> refuse) is tested against `fixtures/
cloudapi-backups-basic.json`, a REAL response captured 2026-08-03 against
a live Basic cluster -- not an invented fixture. Cases (b)/(c) (non-empty)
use a plausible-but-unverified shape, stated as such in cloud_api.py's own
module docstring.
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


def _load_real_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


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
