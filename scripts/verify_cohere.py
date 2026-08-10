#!/usr/bin/env python3
"""Engram · PHASE 0 · P0-B1 (Cohere leg, LLD T9b) — Cohere Embed verification.  Role: [PLUMBER]

Closes the second half of P0-B1 (the Ollama leg already passed, see
scripts/verify_ollama.py and docs/phase0-verification.md §3.2). Answers what
docs/external-constraints.md §4 still flags UNVERIFIED:

    A. auth — does the real COHERE_API_KEY work against api.cohere.com?
    B. dimension — is embed-english-v3.0 EXACTLY 1024-dim, no truncation/
       padding/projection? This is invariant #2; a mismatch is a hard fail,
       not a warning.
    C. input_type asymmetry — do `search_document` and `search_query` both
       return vectors in the SAME 1024-dim space (invariant #9's hybrid
       retrieval assumes they're comparable)?
    D. unit-norm — are returned vectors unit-norm? (Not required by cosine
       distance, but the startup assertion should record what's observed
       rather than assume — docs/external-constraints.md §4.)
    E. batch — does a >1 text call return one vector per input, in order?
    F. latency — round-trip vs the recall budget.

Deliberately defensive, same posture as verify_ollama.py: prints the raw
response shape it actually got rather than assuming Cohere's documented
shape holds on this account/tier.

    pip install -r scripts/requirements-verify.txt
    # .env: COHERE_API_KEY, EMBEDDING_MODEL, EMBEDDING_DIM
    python scripts/verify_cohere.py 2>&1 | tee docs/_raw/p0-b1-cohere.log

Exit 0 only if A, B and C pass — those three close P0-B1's Cohere leg.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import sys
import time

try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

try:
    import httpx
except ImportError:
    sys.exit("FATAL: httpx not installed. pip install -r scripts/requirements-verify.txt")

BASE = os.environ.get("COHERE_BASE_URL", "https://api.cohere.com").rstrip("/")
KEY = os.environ.get("COHERE_API_KEY")
MODEL = os.environ.get("EMBEDDING_MODEL", os.environ.get("ENGRAM_EMBED_MODEL", "embed-english-v3.0"))
EXPECT_DIM = int(os.environ.get("EMBEDDING_DIM", os.environ.get("ENGRAM_EMBED_DIMS", "1024")))
RECALL_BUDGET_S = 2.0  # recall is on the hot path (invariant #9); generous vs a single embed call

RULE = "-" * 72
results: list[tuple[str, str]] = []


def head(t: str) -> None:
    print(f"\n{RULE}\n{t}\n{RULE}")


def record(k: str, v: str) -> None:
    results.append((k, v))
    print(f"  >> {k}: {v}")


def client() -> httpx.Client:
    h = {"Content-Type": "application/json"}
    if KEY:
        h["Authorization"] = f"Bearer {KEY}"
    return httpx.Client(base_url=BASE, headers=h, timeout=30.0)


def embed(c: httpx.Client, texts: list[str], input_type: str) -> tuple[float, int, object]:
    body = {
        "model": MODEL,
        "texts": texts,
        "input_type": input_type,
        "embedding_types": ["float"],
    }
    t0 = time.perf_counter()
    try:
        r = c.post("/v2/embed", json=body)
        dt = time.perf_counter() - t0
        try:
            return dt, r.status_code, r.json()
        except Exception:
            return dt, r.status_code, r.text[:800]
    except Exception as exc:  # noqa: BLE001
        return time.perf_counter() - t0, -1, f"{type(exc).__name__}: {exc}"


def vectors_of(payload: object) -> list[list[float]]:
    """Cohere v2 nests floats under embeddings.float; be defensive either way."""
    if not isinstance(payload, dict):
        return []
    emb = payload.get("embeddings")
    if isinstance(emb, dict) and isinstance(emb.get("float"), list):
        return emb["float"]
    if isinstance(emb, list):
        return emb
    return []


def probe_auth_and_dim(c: httpx.Client) -> tuple[bool, list[float] | None]:
    head(f"PROBE A+B  auth + dimension — {MODEL} @ {BASE}")
    if not KEY:
        print("  COHERE_API_KEY is not set.")
        record("auth", "NO KEY — set COHERE_API_KEY in .env")
        return False, None
    dt, code, payload = embed(c, ["Engram Phase 0 probe: CockroachDB memory item."], "search_document")
    print(f"  HTTP {code} in {dt:.2f}s")
    if code == 401:
        record("auth", "401 — key rejected")
        return False, None
    if code != 200:
        print(f"  body: {json.dumps(payload, default=str)[:500]}")
        record("auth", f"HTTP {code}")
        return False, None
    record("auth", "OK")
    print(f"  top-level keys : {sorted(payload) if isinstance(payload, dict) else type(payload)}")
    vecs = vectors_of(payload)
    if not vecs:
        record("dimension", "NO VECTOR RETURNED — response shape did not match v2/embed")
        return True, None
    dim = len(vecs[0])
    print(f"  vectors returned: {len(vecs)}, dim of first: {dim}")
    record("dimension", f"{dim} dims" + ("  ← MATCHES invariant #2" if dim == EXPECT_DIM
                                         else f"  MISMATCH, expected {EXPECT_DIM}"))
    record("embed latency (search_document)", f"{dt:.2f}s vs {RECALL_BUDGET_S:.0f}s budget — "
                                              + ("WITHIN" if dt <= RECALL_BUDGET_S else "OVER"))
    return dim == EXPECT_DIM, vecs[0]


def probe_input_type_asymmetry(c: httpx.Client, doc_vec: list[float] | None) -> bool:
    head("PROBE C  input_type asymmetry — search_document vs search_query, same space?")
    dt, code, payload = embed(c, ["memory recall probe query"], "search_query")
    print(f"  HTTP {code} in {dt:.2f}s")
    if code != 200:
        print(f"  body: {json.dumps(payload, default=str)[:500]}")
        record("search_query call", f"FAIL HTTP {code}")
        return False
    vecs = vectors_of(payload)
    if not vecs:
        record("search_query call", "OK but no vector in response")
        return False
    q_dim = len(vecs[0])
    record("search_query call", f"OK, {q_dim} dims")
    if doc_vec is not None and q_dim == len(doc_vec):
        # Cosine similarity as a sanity check that both live in one comparable
        # space — not a correctness assertion (unrelated texts, low score is fine),
        # just proves the two calls didn't silently return different-width spaces.
        dot = sum(a * b for a, b in zip(doc_vec, vecs[0]))
        na = math.sqrt(sum(a * a for a in doc_vec))
        nb = math.sqrt(sum(b * b for b in vecs[0]))
        cos = dot / (na * nb) if na and nb else float("nan")
        record("search_document vs search_query cosine", f"{cos:.4f} (sanity check, not a threshold)")
        record("norm(search_document vector)", f"{na:.4f}" + ("  (unit-norm)" if abs(na - 1.0) < 1e-3 else "  (NOT unit-norm)"))
        record("norm(search_query vector)", f"{nb:.4f}" + ("  (unit-norm)" if abs(nb - 1.0) < 1e-3 else "  (NOT unit-norm)"))
    return q_dim == EXPECT_DIM


def probe_batch(c: httpx.Client) -> None:
    head("PROBE E  batch — multiple texts in one call")
    texts = [f"engram probe text {i}" for i in range(5)]
    dt, code, payload = embed(c, texts, "search_document")
    print(f"  HTTP {code} in {dt:.2f}s")
    if code != 200:
        record("batch call", f"FAIL HTTP {code}")
        return
    vecs = vectors_of(payload)
    record("batch call", f"sent {len(texts)}, got {len(vecs)} vectors"
                        + ("  MATCH" if len(vecs) == len(texts) else "  MISMATCH"))


def main() -> int:
    print("Engram Phase 0 · P0-B1 (Cohere leg, LLD T9b) · Cohere Embed verification")
    print(f"  base   : {BASE}")
    print(f"  model  : {MODEL}")
    print(f"  key    : {'set (…' + KEY[-4:] + ')' if KEY else 'UNSET'}")
    with client() as c:
        dim_ok, doc_vec = probe_auth_and_dim(c)
        query_ok = probe_input_type_asymmetry(c, doc_vec) if dim_ok else False
        if dim_ok:
            probe_batch(c)

    head("P0-B1 (Cohere) RESULT  — paste into docs/phase0-verification.md §3.2")
    width = max((len(k) for k, _ in results), default=10)
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    gate = dim_ok and query_ok
    print(f"\n  GATE (auth + 1024-dim search_document + 1024-dim search_query): {'PASS' if gate else 'FAIL'}")
    if not gate:
        print(
            "\n  Triage:\n"
            "    no key / 401     -> create a key at dashboard.cohere.com, put it in .env\n"
            "    dim mismatch     -> wrong model name, or account is on a different embed\n"
            "                        tier than assumed. This BLOCKS invariant #2 — stop and\n"
            "                        re-decide before seeding anything.\n"
            "    429 rate limited -> free tier is demo-only; budget paid before rehearsal."
        )
    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
