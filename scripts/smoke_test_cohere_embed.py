#!/usr/bin/env python3
"""Engram · smoke test for agent/providers/cohere_embed.py against the real API.

Exercises the CLASS this time, not raw httpx like scripts/verify_cohere.py
did (P0-B1's Cohere leg) — same provider, same account, but through the
actual code path `agent/memory/embeddings.py` (not yet written) will call.

    python scripts/smoke_test_cohere_embed.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from agent.providers.cohere_embed import CohereEmbeddings

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


async def main() -> int:
    async with CohereEmbeddings() as ce:
        print(f"\n{RULE}\nsearch_document — batch of 3\n{RULE}")
        docs = await ce.embed(
            ["CockroachDB memory item one", "memory item two", "memory item three"],
            "search_document",
        )
        record("3 texts -> 3 vectors", len(docs) == 3, f"{len(docs)} vector(s)")
        record("all vectors exactly 1024-dim", all(len(v) == 1024 for v in docs),
               f"{[len(v) for v in docs]}")

        print(f"\n{RULE}\nsearch_query — single text\n{RULE}")
        query = await ce.embed(["recall probe"], "search_query")
        record("1 text -> 1 vector, 1024-dim", len(query) == 1 and len(query[0]) == 1024)

        print(f"\n{RULE}\nempty input — no call, no error\n{RULE}")
        empty = await ce.embed([], "search_document")
        record("empty list returns []", empty == [])

        print(f"\n{RULE}\ninvalid input_type rejected client-side\n{RULE}")
        rejected = False
        try:
            await ce.embed(["x"], "not_a_real_type")
        except ValueError:
            rejected = True
        record("invalid input_type raises ValueError before any request", rejected)

    print(f"\n{RULE}\nRESULT\n{RULE}")
    width = max((len(k) for k, _ in results), default=10)
    failures = [k for k, v in results if v.startswith("FAIL")]
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    print(f"\n  {len(results) - len(failures)}/{len(results)} checks passed")
    return 0 if not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
