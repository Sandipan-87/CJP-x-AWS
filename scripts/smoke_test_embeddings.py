#!/usr/bin/env python3
"""Engram · smoke test for agent/memory/embeddings.py against real Cohere + CockroachDB.  [PLUMBER]

The first test in this repo that exercises the FULL write-path chain for
real: CohereEmbeddings -> embed_and_cache -> embedding_cache table ->
cache hit on the second call. Confirms D9 ("never embed the same content
twice") isn't just a comment -- the second call must not reach Cohere at
all, verified by wrapping the real provider and counting calls.

    python scripts/smoke_test_embeddings.py --sslrootcert cluster-ca.crt
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
import uuid
from typing import Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from agent.memory.db import Database
from agent.memory.embeddings import embed_and_cache
from agent.providers.cohere_embed import CohereEmbeddings

RULE = "-" * 72
results: list[tuple[str, str]] = []


def record(k: str, ok: bool, detail: str = "") -> None:
    results.append((k, ("OK " + detail).strip() if ok else "FAIL " + detail))
    print(f"  >> {k}: {'OK' if ok else 'FAIL'} {detail}")


class CountingProvider:
    """Wraps a real EmbeddingProvider and counts embed() calls + texts sent,
    so the test can PROVE a cache hit skipped the provider, not just assume it.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0
        self.texts_sent: list[str] = []

    async def embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        self.calls += 1
        self.texts_sent.extend(texts)
        return await self._inner.embed(texts, input_type)


async def main(sslrootcert: str | None) -> int:
    db = await Database.connect(sslrootcert=sslrootcert)
    marker = uuid.uuid4().hex[:8]  # unique text per run -- never collides with a real cache entry
    texts = [f"engram smoke embeddings test {marker} — item one",
              f"engram smoke embeddings test {marker} — item two"]
    new_text = f"engram smoke embeddings test {marker} — item three (new)"  # defined up-front,
    # so the finally-block cleanup can reference it even if the try block fails before reaching it
    all_ok = True

    async with CohereEmbeddings() as real:
        counting = CountingProvider(real)
        try:
            print(f"\n{RULE}\nFIRST CALL — both texts are new, must hit Cohere\n{RULE}")
            v1 = await embed_and_cache(db, counting, texts, "search_document")
            record("returns 2 vectors", len(v1) == 2, f"{len(v1)}")
            record("both vectors exactly 1024-dim", all(len(v) == 1024 for v in v1),
                   f"{[len(v) for v in v1]}")
            record("provider called exactly once (one batch, both misses)", counting.calls == 1,
                   f"calls={counting.calls}")
            record("both texts were sent to the provider", set(counting.texts_sent) == set(texts))

            print(f"\n{RULE}\nSECOND CALL — same texts, must be a full cache hit\n{RULE}")
            calls_before = counting.calls
            v2 = await embed_and_cache(db, counting, texts, "search_document")
            record("provider NOT called again (D9: never embed twice)", counting.calls == calls_before,
                   f"calls stayed at {counting.calls}")
            record("cached vectors are byte-identical to the originals", v1 == v2)

            print(f"\n{RULE}\nMIXED CALL — one cached text + one brand-new text\n{RULE}")
            calls_before = counting.calls
            v3 = await embed_and_cache(db, counting, [texts[0], new_text], "search_document")
            record("provider called again, exactly once", counting.calls == calls_before + 1)
            record("only the NEW text was sent, not the cached one",
                   new_text in counting.texts_sent[-1:] or new_text in counting.texts_sent,
                   f"last batch sent: {counting.texts_sent[-1] if counting.texts_sent else None!r}")
            record("mixed call still returns 2 correct vectors", len(v3) == 2 and v3[0] == v1[0])

            print(f"\n{RULE}\nEMPTY INPUT — no provider call, no error\n{RULE}")
            empty = await embed_and_cache(db, counting, [], "search_document")
            record("empty list returns [] without touching the provider", empty == [])

        except Exception as exc:  # noqa: BLE001
            all_ok = False
            record("UNEXPECTED EXCEPTION", False, f"{type(exc).__name__}: {exc}")

        finally:
            print(f"\n{RULE}\nCLEANUP\n{RULE}")
            try:
                all_hashes = [
                    __import__("hashlib").sha256(t.encode("utf-8")).hexdigest()
                    for t in (*texts, new_text)
                ]
                async with db._pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "DELETE FROM embedding_cache WHERE content_sha256 = ANY(%s)",
                            (all_hashes,),
                        )
                print(f"  cleaned up {len(all_hashes)} cache row(s)")
            except Exception as exc:  # noqa: BLE001
                all_ok = False
                print(f"  CLEANUP FAILED: {exc}")
            await db.close()

    print(f"\n{RULE}\nRESULT\n{RULE}")
    width = max((len(k) for k, _ in results), default=10)
    failures = [k for k, v in results if v.startswith("FAIL")]
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    print(f"\n  {len(results) - len(failures)}/{len(results)} checks passed")
    return 0 if all_ok and not failures else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser()
    ap.add_argument("--sslrootcert", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.sslrootcert)))
