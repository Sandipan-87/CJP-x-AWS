"""Engram · agent/providers/cohere_embed.py — the only embedder, no ladder.  [BRAINS]

design/02-low-level-design.md §7's adapter table + §4.1 (HLD D9/D12).
`embed-english-v3.0` is natively exactly 1024-dim — no truncation, padding
or projection. Changing the model changes the vector space, not the width,
and owes a full re-embed; there is no fallback embedder for that reason
(unlike the reasoning ladder, this is a one-rung provider on purpose).

Uses a thin `httpx` client rather than the `cohere` SDK — same choice LLD §7
offers either way, and this repo already leans on `httpx` for every other
provider (`scripts/verify_cohere.py`, `verify_ollama.py`).
"""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Sequence

import httpx

from agent.errors import EmbeddingDimensionError, EmbeddingProviderError
from agent.providers.base import EmbeddingProvider

EXPECTED_DIM = 1024
MAX_BATCH = 96                  # LLD §7 adapter table's stated ceiling
MAX_RETRIES = 3                 # LLD §7: "retry 3"
RETRY_BACKOFF_BASE_S = 1.0
VALID_INPUT_TYPES = {"search_document", "search_query"}


def _vectors_of(payload: dict[str, Any]) -> list[list[float]]:
    """Defensive, same posture as scripts/verify_cohere.py: print/trust what
    the response actually contains rather than assuming Cohere's documented
    v2 shape (`embeddings.float`) holds on every account/tier."""
    emb = payload.get("embeddings")
    if isinstance(emb, dict) and isinstance(emb.get("float"), list):
        return emb["float"]
    if isinstance(emb, list):
        return emb
    return []


class CohereEmbeddings(EmbeddingProvider):
    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """`client` is a testability seam — pass a pre-built `httpx.AsyncClient`
        (e.g. one wired to `httpx.MockTransport`) to exercise retry/error
        paths without a real network call (`tests/test_cohere_embed.py`).
        Real callers never need it.
        """
        if client is not None:
            self._client = client
            self._model = model or os.environ.get("ENGRAM_EMBED_MODEL", "embed-english-v3.0")
            return
        api_key = api_key or os.environ.get("COHERE_API_KEY")
        if not api_key:
            raise RuntimeError("COHERE_API_KEY not set and no api_key provided")
        self._model = model or os.environ.get("ENGRAM_EMBED_MODEL", "embed-english-v3.0")
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("COHERE_BASE_URL", "https://api.cohere.com"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "CohereEmbeddings":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        if input_type not in VALID_INPUT_TYPES:
            raise ValueError(f"input_type must be one of {sorted(VALID_INPUT_TYPES)}, got {input_type!r}")
        if not texts:
            return []
        if len(texts) > MAX_BATCH:
            # Fail fast rather than silently sub-batching — the caller decided
            # the batch size; chunking it here would hide that decision.
            raise ValueError(f"batch of {len(texts)} exceeds MAX_BATCH={MAX_BATCH}; caller must chunk")

        last_error: str = "unknown"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = await self._client.post(
                    "/v2/embed",
                    json={
                        "model": self._model,
                        "texts": list(texts),
                        "input_type": input_type,
                        "embedding_types": ["float"],
                    },
                )
            except httpx.TransportError as exc:
                last_error = f"transport error: {type(exc).__name__}: {exc}"
                if attempt < MAX_RETRIES:
                    await self._backoff(attempt)
                    continue
                raise EmbeddingProviderError(f"{last_error} (after {attempt} attempts)") from exc

            if r.status_code == 401:
                # Not transient — never retry an auth failure.
                raise EmbeddingProviderError(f"auth failed (401) — check COHERE_API_KEY: {r.text[:200]}")

            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                if attempt < MAX_RETRIES:
                    await self._backoff(attempt)
                    continue
                raise EmbeddingProviderError(f"{last_error} (after {attempt} attempts)")

            if r.status_code != 200:
                raise EmbeddingProviderError(f"HTTP {r.status_code}: {r.text[:200]}")

            vectors = _vectors_of(r.json())
            if len(vectors) != len(texts):
                raise EmbeddingProviderError(
                    f"expected {len(texts)} vectors, got {len(vectors)} — response shape mismatch"
                )
            for vec in vectors:
                if len(vec) != EXPECTED_DIM:
                    # LLD §16: never degrade, never write — park immediately.
                    raise EmbeddingDimensionError(len(vec), EXPECTED_DIM)
            return vectors

        # Unreachable: every branch above either returns or raises.
        raise EmbeddingProviderError(f"retry loop exited without result: {last_error}")

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(RETRY_BACKOFF_BASE_S * attempt + random.uniform(0, 0.5))
