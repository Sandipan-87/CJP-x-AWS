# Phase 0 — Verification Record

> **This file is evidence, not prose.** Paste raw terminal transcripts. Do not
> summarise them. The Phase 0 exit gate is judged on what is pasted below.
>
> **EXIT GATE:** `P0-P1` and `P0-B1` both pass, and the Apache-2.0 license badge
> is visible in the GitHub About sidebar.
>
> Fallbacks, decided on Day 1 and not Day 8:
> - **P0-P1 fails** → brute-force cosine over a bounded candidate set; the index
>   path gets documented and tested separately against a Standard trial.
> - **P0-B1 fails** → switch region *before* anything is built against the wrong one.

| Field | Value |
|---|---|
| Date run | _YYYY-MM-DD_ |
| Operator | _name_ |
| AWS region | _e.g. us-east-1_ |
| Memory cluster (`engram-memory`) ID | |
| Target cluster (`engram-target-sandbox`) ID | |
| CockroachDB version (`SELECT version()`) | |

## Status board

| ID | Role | Check | Status | Evidence |
|---|---|---|---|---|
| P0-P1 | [PLUMBER] | `feature.vector_index.enabled` + `VECTOR(1024)` + C-SPANN index + `EXPLAIN` proves the index is used | ☐ | §1 |
| P0-P2 | [PLUMBER] | Second Basic cluster `engram-target-sandbox` exists | ☐ | §2 |
| P0-B1 | [BRAINS] | Bedrock: Titan V2 → exactly 1024 dims **and** Claude Sonnet 5 reachable | ☐ | §3 |
| P0-B2 | [BRAINS] | MCP `list_clusters` + `explain_query`; 10 KiB / 20 s / `LIMIT 25` measured | ☐ | §4 |
| P0-P3 | [PLUMBER] | `ccloud cluster backup list -o json` parses on **Basic** | ☐ | §5 |
| P0-I1 | [ILLUSIONIST] | Public repo, Apache-2.0 in the **first** commit, badge renders | ☐ | §6 |

---

## 1 · P0-P1 — Vector index on a free Basic cluster  `[PLUMBER]`

```bash
cockroach sql --url "$ENGRAM_MEMORY_DSN" \
  -f db/phase0_vector_probe.sql --echo-sql 2>&1 | tee docs/_raw/p0-p1.log
```

Three things must appear in the transcript. Anything else is context.

1. `SHOW CLUSTER SETTING feature.vector_index.enabled` → `true`
2. `SHOW CREATE TABLE vec_probe` → the `VECTOR INDEX (scope_id, embedding vector_cosine_ops)` line
3. **STEP 5 `EXPLAIN` names a vector index scan on `vec_probe_scope_cos`**, and
   **STEP 6 (no `scope_id` predicate) does not** — the contrast is the proof
   that `CLAUDE.md` invariant #3 is real and that we can recognise its violation.

### 1.1 Setting applied

```text
(paste)
```

### 1.2 `SHOW CREATE TABLE vec_probe`

```text
(paste)
```

### 1.3 STEP 5 — scope-constrained ANN query · **index MUST be used**

```text
(paste EXPLAIN (VERBOSE), the 3 result rows, and EXPLAIN ANALYZE)
```

Index used? **☐ yes / ☐ no** — index name observed: `______________`
Rows read per `EXPLAIN ANALYZE` (expect ≪ 400): `______`

### 1.4 STEP 6 — negative control · **index MUST NOT be used**

```text
(paste)
```

### 1.5 STEP 7 — `IN`-list form and beam-size sweep

```text
(paste; if `SET vector_search_beam_size` errors, say so here — not a blocker)
```

| beam size | latency | rows read |
|---|---|---|
| 8 | | |
| 64 | | |

→ First data point for the README's beam-size trade-off table.

### 1.6 Anything surprising

_Notes. If the seed `INSERT` or the `::VECTOR(1024)` cast behaved unexpectedly,
that matters for P2-P1's embedding write path — write it down._

---

## 2 · P0-P2 — Target cluster  `[PLUMBER]`

Two clusters, two roles, never conflated: memory = the product, target = the subject.

```bash
ccloud cluster list -o json
```

```text
(paste)
```

Cluster ID recorded in `CLAUDE.md` §4? **☐**

---

## 3 · P0-B1 — Bedrock access  `[BRAINS]`

```bash
pip install -r scripts/requirements-verify.txt
export AWS_REGION=us-east-1
python scripts/verify_bedrock.py 2>&1 | tee docs/_raw/p0-b1.log
```

```text
(paste the entire transcript — DISCOVERY section included; it is the record of
what was visible in this region on this date)
```

| Field | Value |
|---|---|
| Region | |
| Titan model ID | `amazon.titan-embed-text-v2:0` |
| Titan vector length | **must be 1024** → `____` |
| Titan L2 norm (`normalize=true`) | |
| Titan latency | |
| **Resolved Claude model ID** | `____________________` ← copy into `CLAUDE.md` §2 |
| Claude latency / `stopReason` | |

If the resolved Claude ID needed a `us.` / `eu.` / `apac.` inference-profile
prefix, note it — that is a real deployment fact, not a script quirk, and the
agent core must use the same string.

---

## 4 · P0-B2 — Managed MCP server limits  `[BRAINS]`

Run this **while `vec_probe` is still seeded** (between STEP 3 and STEP 8 of the
SQL file) so probes C and E have a real table to hit.

```bash
export CRDB_MCP_TOKEN=...  CRDB_MCP_CLUSTER_ID=...  CRDB_MCP_DATABASE=defaultdb
python scripts/verify_mcp.py 2>&1 | tee docs/_raw/p0-b2.log
```

```text
(paste)
```

### Measured vs documented

| Constraint | Documented (`CLAUDE.md` §4) | Measured | Agrees? |
|---|---|---|---|
| Max response size | 10 KiB | | |
| Query timeout | 20 s | | |
| `SELECT` default limit | 25 rows | | |
| `SHOW` cap | 100 rows | | |
| SQL length limit | 16,384 chars | | |
| Deny-listed schemas | `crdb_internal`, `pg_catalog`, `information_schema` refused | | |
| Write tools exposed | none (`mcp:read` only) | | |

Tools actually offered: `______________________________________________`

**Any disagreement above is a `CLAUDE.md` §4 edit plus a changelog entry.** The
20 s number in particular is load-bearing: P2-B3 sets the client timeout to 15 s
so it fires *inside* the server ceiling and degrades as a typed result instead of
a hang.

---

## 5 · P0-P3 — `ccloud` backup list on Basic  `[PLUMBER]`

```bash
ccloud cluster backup list --cluster "$ENGRAM_TARGET_CLUSTER_ID" -o json \
  | tee fixtures/ccloud-backup-list.json
```

```text
(paste)
```

Parseable JSON on a **Basic** cluster? **☐ yes / ☐ no**
Committed as `fixtures/ccloud-backup-list.json`? **☐**

If Basic exposes no backup surface at all, the pre-flight gate (P3-P3) needs a
different signal. Decide it here, not on Day 9 — the refusal beat is on the
"never cut" list.

---

## 6 · P0-I1 — Repo and license  `[ILLUSIONIST]`

| Field | Value |
|---|---|
| Repo URL | |
| First commit SHA | |
| First commit date (must be after 2026-06-30) | |
| `LICENSE` present in that first commit | ☐ |
| GitHub About sidebar shows "Apache-2.0" | ☐ |

```bash
git log --reverse --format='%H %ad %s' --date=iso | head -1
git show --stat --oneline "$(git rev-list --max-parents=0 HEAD)" | head -20
```

```text
(paste)
```

Sidebar screenshot: `docs/img/license-badge.png`

The badge is on the "never cut" list. If GitHub does not detect it, the cause is
almost always a modified or truncated license body — restore the verbatim
Apache-2.0 text rather than hand-editing it.

---

## 7 · Gate decision

| | |
|---|---|
| P0-P1 | ☐ PASS ☐ FAIL |
| P0-B1 | ☐ PASS ☐ FAIL |
| License badge visible | ☐ |
| **PHASE 0 EXIT GATE** | **☐ PASS ☐ FAIL** |
| Fallback triggered? | |
| Decided by / at | |

On PASS: update `CLAUDE.md` §6 `CURRENT POSITION` to Phase 1 / P1-P1, add the
session changelog entry, and record the region + both cluster IDs in §2/§4.
On FAIL: take the fallback named at the top of this file **today**.
