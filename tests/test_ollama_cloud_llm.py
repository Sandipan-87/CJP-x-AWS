"""Engram · unit tests for agent/providers/ollama_cloud_llm.py -- mocked, no network/real key.

Same httpx.MockTransport pattern as tests/test_cohere_embed.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.errors import LlmRateLimitError, LlmTimeoutError
from agent.providers.ollama_cloud_llm import OllamaCloudLLM, _strip_think_tags


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://fake.ollama.test",
                              transport=httpx.MockTransport(handler))


def _chat_response(content: str = "", tool_calls: list | None = None, **extra_top_level) -> httpx.Response:
    message: dict = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"message": message, **extra_top_level})


def test_strip_think_tags_removes_them():
    assert _strip_think_tags("before<mm:think>hidden</mm:think>after") == "beforeafter"


def test_strip_think_tags_noop_when_absent():
    assert _strip_think_tags("plain content") == "plain content"


def test_plain_chat_returns_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(content="OK")

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            return await llm.complete("system", [{"role": "user", "content": "hi"}], [])

    result = asyncio.run(run())
    assert result.text == "OK"
    assert result.tool_calls == []


def test_think_tags_stripped_from_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(content="<mm:think>secret</mm:think>the answer")

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            return await llm.complete("system", [], [])

    assert asyncio.run(run()).text == "the answer"


def test_tool_call_with_dict_arguments_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(tool_calls=[
            {"function": {"name": "propose", "arguments": {"reasoning": "x"}}}
        ])

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            return await llm.complete("system", [], [{"type": "function"}])

    result = asyncio.run(run())
    assert result.tool_calls == [{"name": "propose", "arguments": {"reasoning": "x"}}]


def test_tool_call_with_string_arguments_parsed_defensively():
    """Some builds return arguments as a JSON string, not a parsed dict --
    scripts/verify_ollama.py already had to handle this."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(tool_calls=[
            {"function": {"name": "propose", "arguments": '{"reasoning": "x"}'}}
        ])

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            return await llm.complete("system", [], [{"type": "function"}])

    result = asyncio.run(run())
    assert result.tool_calls[0]["arguments"] == {"reasoning": "x"}


def test_429_retries_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, text="rate limited")
        return _chat_response(content="OK")

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            return await llm.complete("system", [], [])

    assert asyncio.run(run()).text == "OK"
    assert len(calls) == 3


def test_429_exhausts_retries_and_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            await llm.complete("system", [], [])

    with pytest.raises(LlmRateLimitError):
        asyncio.run(run())


def test_500_raises_timeout_error_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            await llm.complete("system", [], [])

    with pytest.raises(LlmTimeoutError):
        asyncio.run(run())


def test_usage_excludes_total_duration_but_keeps_token_counts():
    """Real bug, found live via the dashboard's llm_token_usage chart (2026-08-13):
    `total_duration` is Ollama's own call latency in NANOSECONDS (billions for a real multi-
    second call) -- `reason(node)` sums every numeric field in `usage`, so including it here
    silently turned "token usage" into "call duration in nanoseconds" in the real CloudWatch
    metric. `usage` must carry only the two real token-count fields.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(content="OK", eval_count=42, prompt_eval_count=17, total_duration=8_500_000_000)

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            return await llm.complete("system", [], [])

    result = asyncio.run(run())
    assert result.usage == {"eval_count": 42, "prompt_eval_count": 17}
    assert "total_duration" not in result.usage


def test_401_is_never_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    async def run():
        async with OllamaCloudLLM(client=_client_with(handler)) as llm:
            await llm.complete("system", [], [])

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert len(calls) == 1
