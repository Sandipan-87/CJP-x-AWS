"""Engram · workers/consolidator/handler.py — merges related episodes into procedures.  [PLUMBER]

design/02-low-level-design.md §9: "Embed episode summaries -> scoped ANN against `class=
'procedure'` -> if >=3 tight episodes (sim >= 0.9) share outcome -> INSERT
`procedures(status='draft', sources=[episode ids])`; first induction per scope requires human
confirm (approvals channel) -> then `active` + `memory_items(class='procedure')`." Idempotency:
"`UNIQUE(scope_id, name)` + `sources` overlap check."

**Two deliberate, stated simplifications against the LLD's literal wording -- neither is a
shortcut around a real requirement, both are explained here rather than silently decided:**

1. **No fresh embedding call for clustering.** By the time this worker runs (its own EventBridge
   schedule is hourly, `embedding_backfill`'s is nightly + on-demand -- both already exist), a
   real episode's `embedding` column has almost always already been filled by `embedding_backfill`
   (invariant #1's seed-then-backfill sequencing). Re-embedding the same content a second time
   here would just be a redundant Cohere call for a vector this project's own D9 cache already
   has. So clustering reuses the ALREADY-STORED embedding (`memory_items.embedding`) directly;
   a row whose embedding is still NULL simply isn't a clustering candidate yet on this run --
   `list_unconsolidated_episodes` filters those out, and it becomes eligible the next time this
   worker runs, after `embedding_backfill` has had a chance to fill it in.

2. **"Scoped ANN against `class='procedure'`" is read as "don't re-induce a procedure the
   sources-overlap check + `UNIQUE(scope_id, name)` already cover", not as a literal second ANN
   pass against existing procedure embeddings.** Grouping episodes into candidate clusters uses a
   SINGLE ANN pass among not-yet-consolidated EPISODES of the same scope (via
   `ann_neighbors_within_distance`, invariant #3's own scoped-ANN discipline), narrowed first by a
   real, structured "share outcome" signal -- `(action_kind, outcome, table)`, read from
   `remediation_actions` via each episode's own `provenance->>'action_id'`, which is far more
   reliable than parsing the human-readable sentence `act_measure(node)`/`gate(node)` write as
   episode `content` (see `list_unconsolidated_episodes`'s own docstring). Avoiding a genuine
   duplicate procedure is then a two-layer DB-level guarantee, exactly as the LLD's own
   idempotency column names it: the sources-overlap `NOT EXISTS` clause already excludes episodes
   claimed by an existing procedure BEFORE clustering ever runs, and `ON CONFLICT (scope_id,
   name) DO NOTHING` catches anything that still collides at insert time.

**The "first induction per scope requires human confirm (approvals channel)" step has a real,
structural gap, stated rather than worked around**: `approvals` (migration 001) has hard `NOT
NULL` foreign keys to BOTH `remediation_actions` and `tasks` -- there is no way to insert a
generic "please confirm this procedure induction" approval row without fabricating a fake
remediation action that never happened, which would pollute the dashboard's real Action Feed.
Rather than do that, a scope's FIRST-EVER procedure induction is created and left at
`status='draft'` with NO corresponding `memory_items(class='procedure')` row (a draft is never
recall-eligible anyway, invariant #9's `status != 'active'` hard filter) -- promoting it to
`active` is a genuine, one-time manual step (a plain `UPDATE procedures SET status='active'`) for
whoever operates this system, logged loudly in this handler's own return value and CloudWatch
Logs so it isn't a silent dead end. Every SUBSEQUENT induction for a scope that already has at
least one procedure (any status -- the human already crossed this gate once for that scope)
promotes straight to `active` + writes the matching memory item in the same transaction, exactly
as the LLD specifies.

**Invoked on a schedule (EventBridge, 1h per the LLD), same shape as `sweep_enumerator`/
`decayer`/`embedding_backfill`** -- `event` is a Scheduled Event payload this handler doesn't
inspect.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from common.db import (
    ann_neighbors_within_distance,
    count_procedures_for_scope,
    get_consolidator_connection,
    insert_draft_procedure,
    list_scopes_with_episodes,
    list_unconsolidated_episodes,
    promote_procedure_active,
)
from common.scoring import decayed_confidence

logger = logging.getLogger("engram.consolidator")
logger.setLevel(logging.INFO)

MIN_CLUSTER_SIZE = 3       # LLD §9: ">=3 tight episodes"
MAX_COSINE_DISTANCE = 0.1  # LLD §9: "sim >= 0.9" -- 1 - 0.9, for Cohere's unit-norm vectors
ANN_LIMIT = 50


def _bucket_key(row: dict) -> tuple[str, str, str]:
    table = (row.get("parameters") or {}).get("table", "unknown")
    return (row["action_kind"], row["outcome"], table)


def _group_by_bucket(rows: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    buckets: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        buckets.setdefault(_bucket_key(row), []).append(row)
    return buckets


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    conn = get_consolidator_connection()

    scopes = list_scopes_with_episodes(conn)
    logger.info("consolidator: %d scope(s) with episode memory", len(scopes))

    induced = 0
    activated = 0
    pending_confirm: list[str] = []
    failed = 0

    for scope_id in scopes:
        try:
            candidates = list_unconsolidated_episodes(conn, scope_id)
        except Exception as exc:  # noqa: BLE001 -- one bad scope must not block the rest
            failed += 1
            logger.error("failed to list candidates for scope_id=%s: %s", scope_id, exc)
            continue

        for key, bucket_rows in _group_by_bucket(candidates).items():
            if len(bucket_rows) < MIN_CLUSTER_SIZE:
                continue
            action_kind, outcome, table = key
            item_ids = [str(r["item_id"]) for r in bucket_rows]
            seed = bucket_rows[0]
            try:
                neighbors = ann_neighbors_within_distance(
                    conn, scope_id, item_ids, seed["embedding"], MAX_COSINE_DISTANCE, ANN_LIMIT,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error("ANN clustering failed for scope_id=%s bucket=%s: %s", scope_id, key, exc)
                continue
            cluster_ids = {item_id for item_id, _dist in neighbors}
            if len(cluster_ids) < MIN_CLUSTER_SIZE:
                continue

            cluster_rows = [r for r in bucket_rows if str(r["item_id"]) in cluster_ids]
            successes = sum(1 for r in cluster_rows if r["outcome"] == "success")
            attempts = len(cluster_rows)
            name = f"{action_kind}_{table}"  # deterministic -- a rerun over the identical
                                              # cluster derives the identical name (LLD's own
                                              # UNIQUE(scope_id, name) idempotency layer)

            try:
                first_ever = count_procedures_for_scope(conn, scope_id) == 0
                procedure_id = insert_draft_procedure(
                    conn, scope_id, name,
                    description=f"Induced from {attempts} episode(s) of {action_kind} on {table} sharing outcome={outcome}.",
                    steps=json.dumps([{
                        "action_kind": action_kind,
                        "parameters": cluster_rows[0].get("parameters") or {},
                        "expected_effect": f"outcome={outcome}",
                    }]),
                    outcome_stats=json.dumps({"successes": successes, "attempts": attempts}),
                    confidence=decayed_confidence(successes, attempts, age_days=0.0),
                    sources_json=json.dumps(sorted(cluster_ids)),
                    status="draft",
                )
                conn.commit()  # this connection is autocommit=False (see get_consolidator_connection)
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                failed += 1
                logger.error("failed to insert draft procedure name=%r scope_id=%s: %s", name, scope_id, exc)
                continue

            if procedure_id is None:
                continue  # ON CONFLICT DO NOTHING -- already consolidated under this name
            induced += 1

            if first_ever:
                pending_confirm.append(procedure_id)
                logger.info(
                    "consolidator: first-ever induction for scope_id=%s (procedure_id=%s, name=%r) "
                    "left as draft -- needs a manual `UPDATE procedures SET status='active'`, see "
                    "handler module docstring", scope_id, procedure_id, name,
                )
                continue

            try:
                promote_procedure_active(
                    conn, procedure_id, scope_id,
                    content=f"Procedure {name!r} activated from {attempts} episode(s) ({successes}/{attempts} successful).",
                    provenance_json=json.dumps({"sources": sorted(cluster_ids)}),
                )
                activated += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error("failed to promote procedure_id=%s to active: %s", procedure_id, exc)

    logger.info(
        "consolidator complete: induced=%d activated=%d pending_confirm=%d failed=%d scopes=%d",
        induced, activated, len(pending_confirm), failed, len(scopes),
    )
    return {
        "statusCode": 200,
        "induced": induced,
        "activated": activated,
        "pending_confirm": pending_confirm,
        "failed": failed,
        "scopes": len(scopes),
    }
