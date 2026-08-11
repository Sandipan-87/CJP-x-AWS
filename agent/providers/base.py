"""Engram · agent/providers/base.py — provider ABCs.  [BRAINS]

design/02-low-level-design.md §1's repo layout names this file as holding
both `LLMProvider` and `EmbeddingProvider`. `LLMProvider` was deferred until
`ollama_cloud_llm.py` existed to check it against (coding-conduct rule 2) —
that module now exists, so it lands here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, NamedTuple, Sequence


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


class LLMResult(NamedTuple):
    text: str                    # content, with any vendor thinking-tag leakage already stripped
    tool_calls: list[dict]       # [{"name": ..., "arguments": {...}}, ...], arguments already-parsed dicts
    usage: dict[str, Any]        # token counts etc., shape is provider-specific; may be empty


class LLMProvider(ABC):
    """LLD §7's adapter table. `agent/providers/ollama_cloud_llm.py`'s
    `OllamaCloudLLM` is the primary rung (D13) — Groq/Together (ladder
    rungs 2/3) are config, not code, and don't exist as separate classes
    yet (nothing has needed to promote off rung 1).
    """

    @abstractmethod
    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        schema: dict | None = None,
    ) -> LLMResult:
        """`schema` is accepted per the LLD's own signature but currently
        unused by the Ollama rung specifically — Ollama has no native
        "response format" mechanism separate from tool-call `parameters`
        JSON schema, which `tools` already carries; kept as a parameter for
        ABC compatibility with a future rung that might use it natively,
        not silently dropped without a reason recorded here.

        **Never relies on a vendor "thinking" channel** for audit-grade
        rationale — that lives in the tool schema's required `reasoning`
        field, validated by the caller's own pydantic model
        (`agent.schemas.Proposal`), not by this method.
        """
