"""Engram · workers/embedding_backfill/handler.py — fills `memory_items.embedding IS NULL` rows.  [PLUMBER]

design/02-low-level-design.md §9: "rows with `embedding IS NULL` -> Cohere `embed-english-v3.0`,
`input_type='search_document'` (via `embedding_cache` first) -> UPDATE; also fills
`embedding_cache` with `model_id='embed-english-v3.0'`." Idempotency: "`WHERE embedding IS NULL
LIMIT 500` cursor."

This is the other half of invariant #1's seed-then-backfill sequencing (`agent/memory/db.py`'s
`insert_memory_item` already writes `embedding=NULL` for `episode`/`procedure` rows on purpose --
see that module's own docstring) and the other half of D9's write-path cache (`agent/memory/
embeddings.py` is the ECS agent's own async version of the identical cache-then-embed logic; this
Lambda is the batch catch-up path for whatever the agent didn't embed synchronously, plus
anything `consolidator` writes before this next runs).

**Content-hash convention matches `agent/memory/embeddings.py` exactly** (`sha256(f"{input_type}:
{text}")`, `input_type` folded into the key) -- a `search_document` and `search_query` embedding
of the same text must never collide in the shared `embedding_cache` table, regardless of which
process wrote which.

**Invoked on a schedule (EventBridge, nightly + on-demand per the LLD), same shape as
`sweep_enumerator`** -- `event` is a Scheduled Event payload this handler doesn't inspect.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from common.config import resolve_secret
from common.db import (
    get_cached_embedding,
    get_embedding_backfill_connection,
    insert_embedding_cache,
    list_memory_items_missing_embedding,
    update_memory_item_embedding,
)
from common.embed import MAX_BATCH, embed_batch

logger = logging.getLogger("engram.embedding_backfill")
logger.setLevel(logging.INFO)

MODEL_ID = "embed-english-v3.0"
INPUT_TYPE = "search_document"  # LLD §9's own stated value for this worker
DEFAULT_ROW_LIMIT = 500


def _content_hash(text: str, input_type: str) -> str:
    """Same key composition as `agent/memory/embeddings.py`'s `_content_hash` -- see module
    docstring."""
    return hashlib.sha256(f"{input_type}:{text}".encode("utf-8")).hexdigest()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    api_key = resolve_secret("COHERE_API_KEY", "COHERE_API_KEY_SECRET_NAME")
    conn = get_embedding_backfill_connection()

    rows = list_memory_items_missing_embedding(conn, limit=DEFAULT_ROW_LIMIT)
    logger.info("embedding_backfill: %d row(s) missing embedding", len(rows))

    filled = 0
    failed = 0
    cache_hits = 0
    misses: list[dict] = []
    for row in rows:
        h = _content_hash(row["content"], INPUT_TYPE)
        cached = get_cached_embedding(conn, h, MODEL_ID)
        if cached is not None:
            cache_hits += 1
            try:
                update_memory_item_embedding(conn, row["item_id"], cached)
                filled += 1
            except Exception as exc:  # noqa: BLE001 -- one bad row must not block the rest
                failed += 1
                logger.error("failed to write cached embedding for item_id=%s: %s", row["item_id"], exc)
        else:
            misses.append({**row, "hash": h})

    # Misses are batched to Cohere's own ceiling so a large backlog doesn't send one request per
    # row -- mirrors agent/memory/embeddings.py's embed_and_cache chunking.
    for chunk_start in range(0, len(misses), MAX_BATCH):
        chunk = misses[chunk_start: chunk_start + MAX_BATCH]
        try:
            vectors = embed_batch(api_key, [m["content"] for m in chunk], INPUT_TYPE, model=MODEL_ID)
        except Exception as exc:  # noqa: BLE001 -- LLD §16: degrade, don't crash the whole run;
            # the still-NULL rows are picked up again by the next invocation's cursor.
            failed += len(chunk)
            logger.error("embed_batch failed for %d row(s): %s", len(chunk), exc)
            continue
        for m, vec in zip(chunk, vectors):
            try:
                insert_embedding_cache(conn, m["hash"], vec, MODEL_ID)
                update_memory_item_embedding(conn, m["item_id"], vec)
                filled += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error("failed to write embedding for item_id=%s: %s", m["item_id"], exc)

    logger.info(
        "embedding_backfill complete: filled=%d cache_hits=%d failed=%d candidates=%d",
        filled, cache_hits, failed, len(rows),
    )
    return {
        "statusCode": 200,
        "filled": filled,
        "cache_hits": cache_hits,
        "failed": failed,
        "candidates": len(rows),
    }
