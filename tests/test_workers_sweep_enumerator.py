"""Engram · unit tests for workers/sweep_enumerator/handler.py -- enumeration + per-row SQS
send logic. `get_sweep_connection`/`list_enabled_watched_queries` and `boto3.client` are all
mocked here; the underlying DB privilege boundary (read-only, cannot write to watched_queries,
cannot touch tasks) is proven live by scripts/bootstrap_sweep_enumerator_role.py, not duplicated
here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workers"))

from sweep_enumerator.handler import handler  # noqa: E402

ROW = {
    "watched_query_id": "wq-1",
    "scope_id": "scope-1",
    "target_cluster_id": "cluster-1",
    "table_name": "orders",
    "query_text": "SELECT * FROM orders WHERE customer_id = 1",
}
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/engram-commands.fifo"


def _patched(rows):
    return (
        patch("sweep_enumerator.handler.get_sweep_connection", return_value="fake-conn"),
        patch("sweep_enumerator.handler.list_enabled_watched_queries", return_value=rows),
        patch.dict("os.environ", {"ENGRAM_QUEUE_URL": QUEUE_URL}, clear=False),
    )


def test_no_watched_queries_enqueues_nothing():
    p1, p2, p3 = _patched([])
    mock_sqs = MagicMock()
    with p1, p2, p3, patch("boto3.client", return_value=mock_sqs):
        result = handler({}, None)
    assert result == {"statusCode": 200, "enqueued": 0, "failed": 0, "candidates": 0}
    mock_sqs.send_message.assert_not_called()


def test_one_watched_query_enqueues_one_message_matching_main_py_schema():
    p1, p2, p3 = _patched([ROW])
    mock_sqs = MagicMock()
    with p1, p2, p3, patch("boto3.client", return_value=mock_sqs):
        result = handler({}, None)

    assert result["enqueued"] == 1
    assert result["failed"] == 0
    assert result["candidates"] == 1
    mock_sqs.send_message.assert_called_once()
    call_kwargs = mock_sqs.send_message.call_args.kwargs
    assert call_kwargs["QueueUrl"] == QUEUE_URL
    body = json.loads(call_kwargs["MessageBody"])
    assert body == {
        "scope_id": "scope-1",
        "target_cluster_id": "cluster-1",
        "table_name": "orders",
        "query_text": "SELECT * FROM orders WHERE customer_id = 1",
        "trigger": "eventbridge",
    }
    # FIFO group is the registry row's own primary key -- see handler.py's module docstring
    # for why this doesn't need to duplicate agent/main.py's fingerprint algorithm.
    assert call_kwargs["MessageGroupId"] == "wq-1"
    assert "MessageDeduplicationId" in call_kwargs


def test_multiple_rows_get_distinct_message_group_ids():
    rows = [ROW, {**ROW, "watched_query_id": "wq-2", "scope_id": "scope-2"}]
    p1, p2, p3 = _patched(rows)
    mock_sqs = MagicMock()
    with p1, p2, p3, patch("boto3.client", return_value=mock_sqs):
        result = handler({}, None)

    assert result["enqueued"] == 2
    group_ids = {c.kwargs["MessageGroupId"] for c in mock_sqs.send_message.call_args_list}
    assert group_ids == {"wq-1", "wq-2"}


def test_one_bad_row_does_not_block_the_rest():
    """Same "never fail the sweep on a single source" rule LLD §5.1 step 6 states for
    observe(node)'s own collection legs, applied here to a single SendMessage failure.
    """
    rows = [ROW, {**ROW, "watched_query_id": "wq-2"}]
    mock_sqs = MagicMock()
    mock_sqs.send_message.side_effect = [Exception("boom"), None]
    p1, p2, p3 = _patched(rows)
    with p1, p2, p3, patch("boto3.client", return_value=mock_sqs):
        result = handler({}, None)

    assert result["enqueued"] == 1
    assert result["failed"] == 1
    assert result["candidates"] == 2
