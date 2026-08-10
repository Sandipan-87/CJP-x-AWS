"""Engram · T3 (design/02-low-level-design.md §14) — CohereEmbeddings adapter mocks.

No network, no real key needed — every call goes through `httpx.MockTransport`.
Covers exactly what T3 names: "a non-1024-width embedding response is
rejected, not written."
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.errors import EmbeddingDimensionError, EmbeddingProviderError
from agent.providers.cohere_embed import MAX_BATCH, MAX_RETRIES, CohereEmbeddings


def _client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="https://fake.cohere.test",
                              transport=httpx.MockTransport(handler))


def _embed_response(dims: list[int]) -> httpx.Response:
    return httpx.Response(200, json={"embeddings": {"float": [[0.1] * d for d in dims]}})


def test_correct_width_is_returned():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _embed_response([1024, 1024])

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            return await ce.embed(["a", "b"], "search_document")

    result = asyncio.run(run())
    assert len(result) == 2
    assert all(len(v) == 1024 for v in result)
    assert len(calls) == 1


def test_wrong_width_raises_dimension_error_not_written():
    def handler(request: httpx.Request) -> httpx.Response:
        return _embed_response([1024, 512])  # second vector is the wrong width

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            await ce.embed(["a", "b"], "search_document")

    with pytest.raises(EmbeddingDimensionError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.got == 512
    assert exc_info.value.expected == 1024


def test_429_retries_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, text="rate limited")
        return _embed_response([1024])

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            return await ce.embed(["a"], "search_document")

    result = asyncio.run(run())
    assert len(result) == 1
    assert len(calls) == 3  # 2 failures + 1 success, within MAX_RETRIES


def test_429_exhausts_retries_and_raises():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, text="rate limited")

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            await ce.embed(["a"], "search_document")

    with pytest.raises(EmbeddingProviderError):
        asyncio.run(run())
    assert len(calls) == MAX_RETRIES


def test_401_is_never_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            await ce.embed(["a"], "search_document")

    with pytest.raises(EmbeddingProviderError):
        asyncio.run(run())
    assert len(calls) == 1  # auth failure is not transient -- no retry


def test_invalid_input_type_rejected_before_any_http_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _embed_response([1024])

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            await ce.embed(["a"], "not_a_real_input_type")

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert len(calls) == 0


def test_batch_over_max_rejected_before_any_http_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _embed_response([1024] * (MAX_BATCH + 1))

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            await ce.embed(["x"] * (MAX_BATCH + 1), "search_document")

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert len(calls) == 0


def test_empty_texts_returns_empty_without_any_http_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _embed_response([])

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            return await ce.embed([], "search_document")

    assert asyncio.run(run()) == []
    assert len(calls) == 0


def test_vector_count_mismatch_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return _embed_response([1024])  # asked for 2 texts, got 1 vector back

    async def run():
        async with CohereEmbeddings(client=_client_with(handler)) as ce:
            await ce.embed(["a", "b"], "search_document")

    with pytest.raises(EmbeddingProviderError):
        asyncio.run(run())
