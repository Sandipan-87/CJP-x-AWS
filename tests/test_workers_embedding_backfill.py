"""Engram · unit tests for workers/embedding_backfill/handler.py -- cache-hit/miss routing and
per-row failure isolation. `get_embedding_backfill_connection`/the common.db helpers/`embed_batch`
are all mocked here; the underlying DB privilege boundary is proven live by
scripts/bootstrap_lifecycle_roles.py, not duplicated here (same split as
tests/test_workers_sweep_enumerator.py).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workers"))

from embedding_backfill.handler import _content_hash, handler  # noqa: E402

ROW_A = {"item_id": "item-a", "content": "Applied create_index on orders."}
ROW_B = {"item_id": "item-b", "content": "Applied analyze_table on customers."}
VEC = [0.1] * 1024


def _patched(rows, cached_by_hash=None, embed_return=None, embed_side_effect=None):
    cached_by_hash = cached_by_hash or {}

    def _get_cached(conn, content_hash, model_id):
        return cached_by_hash.get(content_hash)

    patches = [
        patch("embedding_backfill.handler.get_embedding_backfill_connection", return_value="fake-conn"),
        patch("embedding_backfill.handler.list_memory_items_missing_embedding", return_value=rows),
        patch("embedding_backfill.handler.get_cached_embedding", side_effect=_get_cached),
        patch("embedding_backfill.handler.insert_embedding_cache"),
        patch("embedding_backfill.handler.update_memory_item_embedding"),
        patch.dict("os.environ", {"COHERE_API_KEY": "test-key"}, clear=False),
    ]
    if embed_side_effect is not None:
        patches.append(patch("embedding_backfill.handler.embed_batch", side_effect=embed_side_effect))
    else:
        patches.append(patch("embedding_backfill.handler.embed_batch", return_value=embed_return or []))
    return patches


def test_no_rows_missing_embedding():
    patches = _patched([])
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = handler({}, None)
    assert result == {"statusCode": 200, "filled": 0, "cache_hits": 0, "failed": 0, "candidates": 0}


def test_cache_hit_never_calls_embed_batch():
    h = _content_hash(ROW_A["content"], "search_document")
    patches = _patched([ROW_A], cached_by_hash={h: VEC})
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as mock_embed:
        result = handler({}, None)
    assert result["filled"] == 1
    assert result["cache_hits"] == 1
    assert result["failed"] == 0
    mock_embed.assert_not_called()


def test_cache_miss_calls_embed_batch_and_writes_cache():
    patches = _patched([ROW_B], cached_by_hash={}, embed_return=[VEC])
    with patches[0], patches[1], patches[2] as _p, patches[3] as mock_insert_cache, \
         patches[4] as mock_update, patches[5], patches[6] as mock_embed:
        result = handler({}, None)
    assert result["filled"] == 1
    assert result["cache_hits"] == 0
    mock_embed.assert_called_once()
    call_args = mock_embed.call_args
    assert call_args.args[1] == [ROW_B["content"]]
    assert call_args.args[2] == "search_document"
    mock_insert_cache.assert_called_once()
    mock_update.assert_called_once()


def test_embed_batch_failure_marks_chunk_failed_not_fatal():
    patches = _patched([ROW_A, ROW_B], cached_by_hash={}, embed_side_effect=Exception("cohere down"))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
        result = handler({}, None)
    assert result["failed"] == 2
    assert result["filled"] == 0
    assert result["candidates"] == 2


def test_content_hash_folds_in_input_type_same_as_agent_embeddings():
    """Same key composition as agent/memory/embeddings.py's `_content_hash` (see that module's
    own D9 docstring) -- a search_document and search_query embedding of the same text must
    never collide in the shared embedding_cache table."""
    doc_hash = _content_hash("same text", "search_document")
    query_hash = _content_hash("same text", "search_query")
    assert doc_hash != query_hash
