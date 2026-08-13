"""Engram · workers/common/embed.py — a synchronous Cohere embed client for Lambda.  [PLUMBER]

design/02-low-level-design.md §9: `embedding_backfill` and `consolidator` both need to call
Cohere `embed-english-v3.0`. Deliberately NOT `agent/providers/cohere_embed.py` -- that module is
`async` (built for the long-lived ECS agent's event loop) and lives under `agent/`, which
`workers/` never imports (this project's own directory-tree convention, restated in every
`workers/common/*.py` module so far). Same wire shape, same limits, reimplemented with a
synchronous `httpx.Client` since a Lambda handler here is a plain sync function.
"""

from __future__ import annotations

from typing import Sequence

import httpx

EXPECTED_DIM = 1024
MAX_BATCH = 96  # matches agent/providers/cohere_embed.py's own ceiling, same Cohere API limit
VALID_INPUT_TYPES = {"search_document", "search_query"}


class EmbeddingDimensionError(RuntimeError):
    def __init__(self, got: int, expected: int = EXPECTED_DIM) -> None:
        super().__init__(f"embedding width {got} != expected {expected} -- never write this, park instead")


def embed_batch(api_key: str, texts: Sequence[str], input_type: str, *, model: str = "embed-english-v3.0") -> list[list[float]]:
    """One 1024-dim vector per input text, same order. Raises `EmbeddingDimensionError` on a
    wrong-width response rather than ever returning or writing it (LLD §16: never degrade, never
    write, park immediately) -- the caller (a Lambda invocation) parks by simply letting the
    exception propagate; EventBridge's own retry policy covers the rerun.
    """
    if input_type not in VALID_INPUT_TYPES:
        raise ValueError(f"input_type must be one of {sorted(VALID_INPUT_TYPES)}, got {input_type!r}")
    if not texts:
        return []
    if len(texts) > MAX_BATCH:
        raise ValueError(f"batch of {len(texts)} exceeds MAX_BATCH={MAX_BATCH}; caller must chunk")

    with httpx.Client(
        base_url="https://api.cohere.com",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30.0,
    ) as client:
        r = client.post(
            "/v2/embed",
            json={
                "model": model,
                "texts": list(texts),
                "input_type": input_type,
                "embedding_types": ["float"],
            },
        )
    r.raise_for_status()
    payload = r.json()
    emb = payload.get("embeddings")
    vectors = emb["float"] if isinstance(emb, dict) else emb
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        got = len(vectors) if isinstance(vectors, list) else repr(vectors)
        raise RuntimeError(f"expected {len(texts)} vectors, got {got}")
    for vec in vectors:
        if len(vec) != EXPECTED_DIM:
            raise EmbeddingDimensionError(len(vec))
    return vectors
