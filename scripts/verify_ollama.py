#!/usr/bin/env python3
"""Engram · PHASE 0 · P0-B1 replacement — Ollama Cloud verification.  Role: [BRAINS]

Replaces the Bedrock Claude gate (ADR-001). Answers the questions the design
docs flag as unverified, and one they do not:

    A. auth + is `minimax-m3:cloud` actually available on this account?
    B. plain chat round-trip latency vs the 8 s demo budget (HLD §9.3)
    C. strict-JSON tool calling — does it fill a REQUIRED `reasoning` field?
    D. thinking-mode reality — is `message.thinking` returned? do <mm:think>
       tags leak into content? (ollama #16632, vLLM #45687)
    E. multi-turn tool-result handling — does it stall? (ollama #16389)
    F. EMBEDDINGS — can Ollama serve a 1024-dim vector? This is not in the
       design docs, and it matters: Bedrock Titan is blocked account-wide, so
       embeddings currently have NO provider (CLAUDE.md §8 #2).

Deliberately defensive: the exact Ollama Cloud response shapes are not verified
by us, so every probe prints the raw keys it actually received rather than
assuming. A probe that cannot find what it expects says so instead of crashing.

    pip install -r scripts/requirements-verify.txt
    # .env: OLLAMA_API_KEY, ENGRAM_LLM_MODEL
    python scripts/verify_ollama.py 2>&1 | tee docs/_raw/p0-b1-ollama.log

Exit 0 only if A, B and C pass — those three are the P0-B1 gate.
"""

from __future__ import annotations

import json
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

BASE = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com").rstrip("/")
KEY = os.environ.get("OLLAMA_API_KEY")
MODEL = os.environ.get("ENGRAM_LLM_MODEL", "minimax-m3:cloud")
TIMEOUT = float(os.environ.get("ENGRAM_LLM_TIMEOUT_S", "90"))
DEMO_BUDGET_S = 5.0  # HLD §9.3: M3 total < 5 s inside the 8 s beat

# All 1024-dim, so any of them satisfies invariant #2's dimension pin.
EMBED_CANDIDATES = [
    c for c in (os.environ.get("ENGRAM_EMBED_MODEL"), "mxbai-embed-large",
                "bge-large", "bge-large-en-v1.5", "qwen3-embedding:0.6b") if c
]

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
    return httpx.Client(base_url=BASE, headers=h, timeout=TIMEOUT)


def post(c: httpx.Client, path: str, body: dict) -> tuple[float, int, object]:
    t0 = time.perf_counter()
    try:
        r = c.post(path, json=body)
        dt = time.perf_counter() - t0
        try:
            return dt, r.status_code, r.json()
        except Exception:
            return dt, r.status_code, r.text[:800]
    except Exception as exc:  # noqa: BLE001
        return time.perf_counter() - t0, -1, f"{type(exc).__name__}: {exc}"


def msg_of(payload: object) -> dict:
    return payload.get("message", {}) if isinstance(payload, dict) else {}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "propose_index",
            "description": "Propose exactly one CockroachDB index to fix a slow query.",
            "parameters": {
                "type": "object",
                "properties": {
                    # The design's whole thinking-mode workaround rests on this
                    # field existing and being filled (LLD §3, Proposal).
                    "reasoning": {"type": "string", "description": "Audit-grade rationale."},
                    "table": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["reasoning", "table", "columns", "risk"],
            },
        },
    }
]

INCIDENT = (
    "Query `SELECT id FROM orders WHERE customer_id = $1` on CockroachDB does a "
    "full scan of 2,000,000 rows and takes 4.3 s. EXPLAIN recommends "
    "`CREATE INDEX ON orders (customer_id)`. Call propose_index exactly once."
)


def probe_a(c: httpx.Client) -> bool:
    head(f"PROBE A  auth + model availability — {MODEL} @ {BASE}")
    if not KEY:
        print("  OLLAMA_API_KEY is not set.")
        record("auth", "NO KEY — set OLLAMA_API_KEY in .env")
        return False
    try:
        t0 = time.perf_counter()
        r = c.get("/api/tags")
        dt = time.perf_counter() - t0
    except Exception as exc:  # noqa: BLE001
        record("auth", f"transport error: {type(exc).__name__}: {exc}")
        return False
    print(f"  GET /api/tags -> {r.status_code} in {dt:.2f}s")
    if r.status_code == 401:
        record("auth", "401 — key rejected")
        return False
    if r.status_code != 200:
        print(f"  body: {r.text[:300]}")
        record("auth", f"HTTP {r.status_code} — /api/tags may not be exposed on Cloud")
        # Not fatal: Cloud may not implement /api/tags. Chat is the real test.
        record("model listed", "UNKNOWN (endpoint unavailable) — relying on PROBE B")
        return True
    try:
        names = [m.get("name") or m.get("model") for m in (r.json().get("models") or [])]
    except Exception:
        names = []
    print(f"  {len(names)} models visible")
    for n in sorted(x for x in names if x):
        print(f"    {n}")
    record("auth", "OK")
    record("model listed", "YES" if MODEL in names else f"NOT LISTED (looked for {MODEL!r})")
    return True


def probe_b(c: httpx.Client) -> bool:
    head("PROBE B  plain chat round-trip + latency vs the 8 s beat")
    dt, code, payload = post(c, "/api/chat", {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "stream": False,
        "options": {"temperature": 0.1},
    })
    print(f"  HTTP {code} in {dt:.2f}s")
    if code != 200:
        print(f"  body: {json.dumps(payload, default=str)[:500]}")
        record("chat round-trip", f"FAIL HTTP {code}")
        return False
    m = msg_of(payload)
    print(f"  top-level keys : {sorted(payload) if isinstance(payload, dict) else type(payload)}")
    print(f"  message keys   : {sorted(m)}")
    print(f"  content        : {str(m.get('content'))[:200]!r}")
    for k in ("eval_count", "prompt_eval_count", "total_duration"):
        if isinstance(payload, dict) and k in payload:
            print(f"  {k:16} {payload[k]}")
    record("chat round-trip", "OK")
    record("chat latency", f"{dt:.2f}s vs {DEMO_BUDGET_S:.0f}s budget — "
                          + ("WITHIN" if dt <= DEMO_BUDGET_S else "OVER, see HLD §14"))
    return True


def probe_c(c: httpx.Client) -> bool:
    head("PROBE C  strict-JSON tool call — is REQUIRED `reasoning` filled?")
    dt, code, payload = post(c, "/api/chat", {
        "model": MODEL,
        "messages": [{"role": "user", "content": INCIDENT}],
        "tools": TOOLS,
        "stream": False,
        "options": {"temperature": 0.1},
    })
    print(f"  HTTP {code} in {dt:.2f}s")
    if code != 200:
        print(f"  body: {json.dumps(payload, default=str)[:500]}")
        record("tool call", f"FAIL HTTP {code}")
        return False
    m = msg_of(payload)
    calls = m.get("tool_calls") or []
    print(f"  message keys : {sorted(m)}")
    print(f"  tool_calls   : {len(calls)}")
    if not calls:
        print(f"  content: {str(m.get('content'))[:400]!r}")
        record("tool call", "NO tool_calls — model answered in prose instead")
        record("reasoning field", "n/a")
        return False
    args = (calls[0].get("function") or {}).get("arguments")
    if isinstance(args, str):  # some builds return a JSON string
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            record("tool call", "arguments were a non-JSON string")
            return False
    print(f"  arguments    : {json.dumps(args, default=str)[:500]}")
    missing = [k for k in ("reasoning", "table", "columns", "risk") if k not in (args or {})]
    record("tool call", "OK" if not missing else f"schema INCOMPLETE, missing {missing}")
    reasoning = (args or {}).get("reasoning") or ""
    record("reasoning field", f"{len(reasoning)} chars" if reasoning
                              else "EMPTY — the design's audit trail depends on this")
    record("tool-call latency", f"{dt:.2f}s")
    return not missing


def probe_d(c: httpx.Client) -> None:
    head("PROBE D  thinking mode — ollama #16632 / vLLM #45687")
    dt, code, payload = post(c, "/api/chat", {
        "model": MODEL,
        "messages": [{"role": "user", "content": "What is 27 * 453? Answer with the number only."}],
        "stream": False,
        "think": True,
        "options": {"temperature": 0.1},
    })
    print(f"  HTTP {code} in {dt:.2f}s  (think=true)")
    if code != 200:
        record("think=true accepted", f"NO — HTTP {code}")
        return
    m = msg_of(payload)
    content = str(m.get("content") or "")
    has_field = "thinking" in m and bool(m.get("thinking"))
    leaks = "<mm:think" in content or "</mm:think>" in content
    print(f"  message keys : {sorted(m)}")
    print(f"  content      : {content[:240]!r}")
    record("message.thinking returned", "YES (design assumption is stale)" if has_field
                                        else "no — matches ollama #16632")
    record("<mm:think> leaks into content", "YES — adapter MUST strip" if leaks else "not observed")


def probe_e(c: httpx.Client) -> None:
    head("PROBE E  multi-turn tool-result handling — ollama #16389 stalling")
    convo = [
        {"role": "user", "content": INCIDENT},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "propose_index",
                                      "arguments": {"reasoning": "seq scan on a selective predicate",
                                                    "table": "orders", "columns": ["customer_id"],
                                                    "risk": "low"}}}]},
        {"role": "tool", "content": json.dumps(
            {"applied": True, "before_ms": 4300, "after_ms": 12})},
    ]
    dt, code, payload = post(c, "/api/chat", {
        "model": MODEL, "messages": convo, "tools": TOOLS,
        "stream": False, "options": {"temperature": 0.1},
    })
    print(f"  HTTP {code} in {dt:.2f}s")
    if code != 200:
        print(f"  body: {json.dumps(payload, default=str)[:400]}")
        record("tool-result turn", f"FAIL HTTP {code}")
        return
    m = msg_of(payload)
    content = str(m.get("content") or "")
    print(f"  content: {content[:300]!r}")
    record("tool-result turn", "OK — model continued" if content.strip()
                               else "EMPTY RESPONSE — looks like the #16389 stall")
    record("tool-result latency", f"{dt:.2f}s")


def probe_f(c: httpx.Client) -> None:
    head("PROBE F  embeddings — can Ollama supply a 1024-dim vector?")
    print("  Context: Bedrock Titan is blocked account-wide, so embeddings have")
    print("  no provider (CLAUDE.md §8 #2). A 1024-dim hit here unblocks Phase 2.")
    print("  REMINDER: the vector space is provider-specific — decide BEFORE seeding.\n")
    found = None
    for model in EMBED_CANDIDATES:
        for path, body in (("/api/embed", {"model": model, "input": "engram probe"}),
                           ("/api/embeddings", {"model": model, "prompt": "engram probe"})):
            dt, code, payload = post(c, path, body)
            if code != 200:
                detail = json.dumps(payload, default=str)[:110] if not isinstance(payload, str) else payload[:110]
                print(f"  {model:26} {path:18} HTTP {code}  {detail}")
                continue
            vec = None
            if isinstance(payload, dict):
                if isinstance(payload.get("embeddings"), list) and payload["embeddings"]:
                    vec = payload["embeddings"][0]
                elif isinstance(payload.get("embedding"), list):
                    vec = payload["embedding"]
            dims = len(vec) if isinstance(vec, list) else None
            print(f"  {model:26} {path:18} HTTP 200  dims={dims}  ({dt:.2f}s)")
            if dims:
                record(f"embed · {model}", f"{dims} dims"
                       + ("  ← MATCHES invariant #2" if dims == 1024 else "  (NOT 1024)"))
                if dims == 1024 and found is None:
                    found = model
            break
    record("1024-dim embedder on Ollama",
           f"YES — {found}" if found else "none found; blocker #2 stands")


def main() -> int:
    print("Engram Phase 0 · P0-B1 (replacement) · Ollama Cloud verification")
    print(f"  base   : {BASE}")
    print(f"  model  : {MODEL}")
    print(f"  key    : {'set (…' + KEY[-4:] + ')' if KEY else 'UNSET'}")
    with client() as c:
        a = probe_a(c)
        b = probe_b(c) if a else False
        cc = probe_c(c) if b else False
        if b:
            probe_d(c)
            probe_e(c)
        if a:
            probe_f(c)

    head("P0-B1 (Ollama) RESULT  — paste into docs/phase0-verification.md §3")
    width = max((len(k) for k, _ in results), default=10)
    for k, v in results:
        print(f"  {k.ljust(width)} : {v}")
    gate = a and b and cc
    print(f"\n  GATE (A auth + B chat + C strict JSON): {'PASS' if gate else 'FAIL'}")
    if not gate:
        print(
            "\n  Triage:\n"
            "    no key / 401        -> create a key at https://ollama.com, put it in .env\n"
            "    model not listed    -> confirm the exact id on ollama.com/library; the\n"
            "                           `:cloud` suffix and Cloud-tier availability are\n"
            "                           BOTH unverified by us (CLAUDE.md §4).\n"
            "    no tool_calls       -> the model answered in prose. Reason node needs\n"
            "                           strict JSON; if this persists the fallback ladder\n"
            "                           (HLD §9.1) has to move now, not on Day 6.\n"
            "    429 / rate limited  -> free tier is demo-only; Pro is budgeted from Day 1."
        )
    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
