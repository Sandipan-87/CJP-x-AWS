"""Engram · workers/sweep_enumerator/handler.py — the sweep's own enumeration step.  [PLUMBER]

design/02-low-level-design.md §5.1 step 1 ("Collect: MCP ... probe SQL ... CloudWatch ...
ccloud"). CLAUDE.md's own Next-action list named this as "the actual blocker on ever flipping the
sweep rule's `enabled=False`": `infra/engram_infra/agent_stack.py`'s 5-minute EventBridge rule has
always fired a single hardcoded EXAMPLE message (or nothing at all) because nothing in this
codebase ever decided WHICH scope/cluster/table/query is worth periodically re-checking.

**A deliberately smaller substitute for the LLD's own answer, not a shortcut around it.** The
LLD's step 1 wants live traffic discovery via MCP (`show_running_queries`, `list_tables`) — this
project has never built an MCP client at all (a separate, larger, already-tracked gap; `agent/
main.py`'s own startup self-tests skip the MCP check for the same reason). Rather than block the
sweep rule on that unbuilt piece indefinitely, this Lambda reads an explicit, ops-maintained
registry instead (`db/migrations/008_watched_queries.sql`'s `watched_queries` table) — the same
"watched query list" pattern real DB reliability teams already use when they don't have (or don't
trust) fully automatic traffic discovery. Nothing about the actual MEASUREMENT is faked: this
Lambda only decides WHICH query gets checked on a given tick; `agent/main.py`'s own real
`SqlProbe.explain_analyze()` still does the real work downstream, unchanged, exactly as it does
for a manually- or webhook-triggered message today.

**Invoked on a schedule (EventBridge), not an HTTP request** — `event` is a Scheduled Event
payload this handler doesn't need to inspect at all, unlike every other Lambda in `workers/`.
Returns a plain summary dict (not an API Gateway response shape) purely for CloudWatch Logs
visibility; nothing consumes the return value.

**FIFO message group is the watched_query_id, not a recomputed query fingerprint.** `agent/main
.py`'s own `_thread_id_for_fingerprint`/`fingerprint` live in `agent/`, which `workers/` never
imports (same split as everywhere else in this directory — `pg8000` vs `psycopg3`, no Docker-based
Lambda bundling). A FIFO MessageGroupId only needs to be a STABLE identifier per distinct
candidate so SQS won't ever process two ticks of the SAME watched query out of order/concurrently
— the registry row's own primary key already provides that, with no need to duplicate the
fingerprint algorithm here. (`agent/main.py`'s `process_message()` independently recomputes its
own fingerprint from `query_text` when it receives the message, same as it does for every other
trigger — this Lambda's MessageGroupId choice has no bearing on that.)

**One bad row must never block the rest, same "never fail the sweep on a single source" rule LLD
§5.1 step 6 states for `observe(node)`'s own collection legs** — each row is enqueued in its own
try/except; a single malformed row or a single `SendMessage` failure is logged and skipped, not
fatal to the whole invocation.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from common.db import get_sweep_connection, list_enabled_watched_queries

logger = logging.getLogger("engram.sweep_enumerator")
logger.setLevel(logging.INFO)


def _build_message(row: dict) -> dict:
    """Matches `agent/main.py`'s documented message schema exactly (its own module docstring,
    decision #6) — `trigger` is always `"eventbridge"` here, since this Lambda only ever runs off
    the scheduled sweep rule.
    """
    return {
        "scope_id": row["scope_id"],
        "target_cluster_id": row["target_cluster_id"],
        "table_name": row["table_name"],
        "query_text": row["query_text"],
        "trigger": "eventbridge",
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    queue_url = os.environ["ENGRAM_QUEUE_URL"]

    conn = get_sweep_connection()
    rows = list_enabled_watched_queries(conn)
    logger.info("watched_queries: %d enabled row(s)", len(rows))

    import boto3  # imported lazily -- keeps unit tests from needing real AWS credentials to import this module

    sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    enqueued = 0
    failed = 0
    for row in rows:
        try:
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(_build_message(row)),
                MessageGroupId=str(row["watched_query_id"]),
                MessageDeduplicationId=str(uuid.uuid4()),
            )
            enqueued += 1
        except Exception as exc:  # noqa: BLE001 -- one bad row must not block the rest, see module docstring
            failed += 1
            logger.error("failed to enqueue watched_query_id=%s: %s", row.get("watched_query_id"), exc)

    logger.info("sweep enumeration complete: enqueued=%d failed=%d", enqueued, failed)
    return {"statusCode": 200, "enqueued": enqueued, "failed": failed, "candidates": len(rows)}
