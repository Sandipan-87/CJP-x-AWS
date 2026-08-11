"""Engram · agent/providers/ollama_cloud_llm.py — primary reasoning rung (D13).  [BRAINS]

design/02-low-level-design.md §7 adapter table. Wire shape is NOT a fresh
guess here — it's exactly what `scripts/verify_ollama.py` already measured
and gated PASS (Session 7, `docs/external-constraints.md` §3.0): native
`POST https://ollama.com/api/chat` (not the OpenAI-compatible
`/v1/chat/completions` shape), model tag `minimax-m3:cloud` invocable
despite being absent from `/api/tags`'s listing, `message.thinking`
returned as its own field with no `<mm:think>` leak into `content` observed.

`<mm:think>` stripping is kept anyway, as **defense-in-depth, not the
primary-path safeguard the original design assumed** — the corrected record
is in `docs/external-constraints.md` §3.1 (Session 7): the leak this
guards against was never actually observed on a real, keyed call. Audit
rationale still lives in the tool schema's required `reasoning` field,
never the `thinking` channel — that principle is unchanged by the
correction, only its original justification was.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
from typing import Any

import httpx

from agent.errors import LlmRateLimitError, LlmTimeoutError
from agent.providers.base import LLMProvider, LLMResult

DEFAULT_BASE_URL = "https://ollama.com"
DEFAULT_MODEL = "minimax-m3:cloud"
DEFAULT_TIMEOUT_S = 90.0          # LLD §7 / §2 config contract: ENGRAM_LLM_TIMEOUT_S
DEFAULT_MAX_RETRIES = 3           # LLD §7 / §2 config contract: ENGRAM_LLM_MAX_RETRIES
DEFAULT_TEMPERATURE = 0.1         # LLD §7: "temp 0.1"
RETRY_BACKOFF_BASE_S = 1.0

_MM_THINK_RE = re.compile(r"<mm:think>.*?</mm:think>", re.DOTALL | re.IGNORECASE)


def _strip_think_tags(text: str) -> str:
    return _MM_THINK_RE.sub("", text)


def _parse_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Defensive, same posture as `scripts/verify_ollama.py`: some builds
    return `arguments` as a JSON string rather than a parsed object."""
    calls = message.get("tool_calls") or []
    parsed: list[dict[str, Any]] = []
    for call in calls:
        fn = call.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass  # leave as the raw string; caller's schema validation will reject it
        parsed.append({"name": fn.get("name"), "arguments": args})
    return parsed


class OllamaCloudLLM(LLMProvider):
    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """`client` is a testability seam — pass a pre-built `httpx.AsyncClient`
        (e.g. wired to `httpx.MockTransport`) to exercise retry/error paths
        without a real network call, same pattern as `CohereEmbeddings`.
        """
        self._model = model or os.environ.get("ENGRAM_LLM_MODEL", DEFAULT_MODEL)
        self._max_retries = max_retries or int(os.environ.get("ENGRAM_LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES))
        if client is not None:
            self._client = client
            return
        api_key = api_key or os.environ.get("OLLAMA_API_KEY")
        if not api_key:
            raise RuntimeError("OLLAMA_API_KEY not set and no api_key provided")
        timeout_s = timeout_s or float(os.environ.get("ENGRAM_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self._client = httpx.AsyncClient(
            base_url=base_url or os.environ.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OllamaCloudLLM":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def complete(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        schema: dict | None = None,  # accepted per the ABC; unused by this rung, see base.py
    ) -> LLMResult:
        full_messages = [{"role": "system", "content": system}, *messages]
        body: dict[str, Any] = {
            "model": self._model,
            "messages": full_messages,
            "stream": False,
            "options": {"temperature": DEFAULT_TEMPERATURE},
        }
        if tools:
            body["tools"] = tools

        last_error: str = "unknown"
        for attempt in range(1, self._max_retries + 1):
            try:
                r = await self._client.post("/api/chat", json=body)
            except httpx.TimeoutException as exc:
                last_error = f"timeout: {exc}"
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise LlmTimeoutError(f"{last_error} (after {attempt} attempts)") from exc
            except httpx.TransportError as exc:
                last_error = f"transport error: {type(exc).__name__}: {exc}"
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise LlmTimeoutError(f"{last_error} (after {attempt} attempts)") from exc

            if r.status_code == 401:
                raise RuntimeError(f"auth failed (401) — check OLLAMA_API_KEY: {r.text[:200]}")

            if r.status_code == 429:
                last_error = f"HTTP 429: {r.text[:200]}"
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise LlmRateLimitError(f"{last_error} (after {attempt} attempts)")

            if r.status_code >= 500:
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                raise LlmTimeoutError(f"{last_error} (after {attempt} attempts)")

            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

            payload = r.json()
            message = payload.get("message", {}) if isinstance(payload, dict) else {}
            content = _strip_think_tags(str(message.get("content") or ""))
            tool_calls = _parse_tool_calls(message)
            usage = {
                k: payload[k]
                for k in ("eval_count", "prompt_eval_count", "total_duration")
                if isinstance(payload, dict) and k in payload
            }
            return LLMResult(text=content, tool_calls=tool_calls, usage=usage)

        # Unreachable: every branch above either returns or raises.
        raise LlmTimeoutError(f"retry loop exited without result: {last_error}")

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(RETRY_BACKOFF_BASE_S * attempt + random.uniform(0, 0.5))
