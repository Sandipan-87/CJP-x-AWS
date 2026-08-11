"""Engram · agent/memory/embeddings.py — write-path embedding + fingerprint cache.  [PLUMBER]

design/02-low-level-design.md §1 repo layout + D9 ("embedding cache: never
embed the same content twice"). Sits between `agent/providers/cohere_embed.
py` (the provider) and `agent/memory/db.py`'s new `embedding_cache` methods:
hash the content, check the cache, call the provider only on a miss, write
the cache row. Provider-agnostic on purpose — takes an `EmbeddingProvider`,
not a `CohereEmbeddings` specifically, even though there is currently only
one implementation (invariant #2: no embeddings ladder).

A cache hit under a DIFFERENT `model_id` is treated as a MISS, never as a
hit: 1024-dim spaces from different models are mutually incomparable
(HLD §3 D9/D12) — the width matching is not enough, the model must match too.

CAUGHT BEFORE A SECOND CALL SITE EXISTED, stated rather than silently fixed:
`embedding_cache`'s frozen schema (migration 001) keys on `content_sha256`
alone — no `input_type` column. But Cohere's `search_document` and
`search_query` embeddings for the SAME text are deliberately DIFFERENT
vectors (the asymmetry invariant #9/§4 already warns about). §5.2's
`recall(node)` spec calls for "cache hit on fingerprint" reusing the exact
text `observe(node)` already embedded with `search_document` — if the cache
key were pure content hash, a later `search_query` lookup for that same
text would silently return the WRONG (document-side) vector: exactly the
"collapsing input_type degrades recall silently" failure invariant #9 exists
to prevent, just relocated into the cache instead of a missing argument.
**Fix, contained entirely in this module, no migration needed:** the hash
folds in `input_type`, so a `search_document` and a `search_query` embedding
of the same text get different cache keys and can never collide. Cheap
because nothing has been seeded yet (invariant #1) — changing the key
composition now costs nothing; changing it after real rows exist would not.
"""

from __future__ import annotations

import hashlib
import os
from typing import Sequence

from agent.errors import EmbeddingDimensionError
from agent.memory.db import Database
from agent.providers.base import EmbeddingProvider

EXPECTED_DIM = 1024
DEFAULT_PROVIDER_BATCH = 96  # matches CohereEmbeddings.MAX_BATCH; no ladder exists to vary this


def _content_hash(text: str, input_type: str) -> str:
    """`input_type` is folded into the key on purpose — see the module
    docstring's CAUGHT note. Never hash `text` alone."""
    return hashlib.sha256(f"{input_type}:{text}".encode("utf-8")).hexdigest()


async def embed_and_cache(
    db: Database,
    provider: EmbeddingProvider,
    texts: Sequence[str],
    input_type: str,
    *,
    model_id: str | None = None,
    provider_batch: int = DEFAULT_PROVIDER_BATCH,
) -> list[list[float]]:
    """One 1024-dim vector per input text, same order. Cache hits never
    reach the provider at all; misses are batched (`provider_batch`) so a
    caller doesn't need to know the provider's own batch ceiling.
    """
    model_id = model_id or os.environ.get("ENGRAM_EMBED_MODEL", "embed-english-v3.0")
    if not texts:
        return []

    hashes = [_content_hash(t, input_type) for t in texts]
    cached = await db.get_cached_embeddings(hashes)

    results: list[list[float] | None] = [None] * len(texts)
    miss_indices: list[int] = []
    for i, h in enumerate(hashes):
        row = cached.get(h)
        if row is not None and row["model_id"] == model_id:
            results[i] = row["embedding"]
        else:
            miss_indices.append(i)

    for chunk_start in range(0, len(miss_indices), provider_batch):
        chunk_indices = miss_indices[chunk_start: chunk_start + provider_batch]
        chunk_texts = [texts[i] for i in chunk_indices]
        vectors = await provider.embed(chunk_texts, input_type)
        for i, vec in zip(chunk_indices, vectors):
            if len(vec) != EXPECTED_DIM:
                # Defense-in-depth: CohereEmbeddings already raises
                # EmbeddingDimensionError internally on a bad width. This is
                # the second check, right before anything gets WRITTEN —
                # LLD §16: never degrade, never write, park immediately.
                raise EmbeddingDimensionError(len(vec), EXPECTED_DIM)
            results[i] = vec
            await db.insert_embedding_cache(hashes[i], vec, model_id)

    assert all(r is not None for r in results)  # every index was either a hit or filled above
    return results  # type: ignore[return-value]
