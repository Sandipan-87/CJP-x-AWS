"""Engram · agent/providers/base.py — provider ABCs.  [BRAINS]

design/02-low-level-design.md §1's repo layout names this file as holding
both `LLMProvider` and `EmbeddingProvider`. Only `EmbeddingProvider` exists
so far — `LLMProvider` lands with `ollama_cloud_llm.py`, not yet written;
adding it now with no implementation to check it against would be
speculative (coding-conduct rule 2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence


class EmbeddingProvider(ABC):
    """`agent/providers/cohere_embed.py`'s `CohereEmbeddings` is the only
    implementation — LLD §7: "the only embedder — no ladder."
    """

    @abstractmethod
    async def embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        """One vector per input text, same order, each exactly 1024-dim.

        `input_type` is REQUIRED, not optional — `search_document` on the
        write path, `search_query` on recall. Collapsing it to a single
        value degrades recall silently rather than raising
        (docs/external-constraints.md §4) — every call site must pass it
        explicitly, there is no default.
        """
