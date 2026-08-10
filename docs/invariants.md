# Schema invariants — full text and rationale

> `CLAUDE.md` §3 keeps the normative one-line statement of each invariant so the always-loaded file stays inside its context budget; the reasoning, the evidence and the failure mode each one prevents live here. **Nothing was dropped; it was moved.**
>
> **Violating any of these breaks the submission.** Numbering is stable — §3 and this file are the same list, and a rule is never renumbered.

---

## 1 — Enable the feature, then seed, *then* index

`SET CLUSTER SETTING feature.vector_index.enabled = true` must run before any vector DDL.

**Seed rows BEFORE creating the vector index.** Large batch inserts into a vector-indexed table are explicitly warned against by CockroachDB, and `IMPORT INTO` is **unsupported** on a table that carries a vector index. The ordering is therefore: create table → seed → create `VECTOR INDEX`. The LLD marks `mem_vec_idx` as a post-seed step for exactly this reason.

## 2 — `VECTOR(1024)` and the C-SPANN index shape

```sql
VECTOR INDEX (scope_id, embedding vector_cosine_ops)
  WITH (min_partition_size=16, max_partition_size=128)
```

**Verified 2026-08-03** on a free Basic v26.2.1 cluster (`docs/phase0-verification.md`, P0-P1): the plan showed a `vector search` operator with `prefix spans` on `scope_id`, reading 11 of 400 rows in 6 ms / 5.576 RU.

Two corollaries learned the hard way:

- **C-SPANN does not serve plain `scope_id` predicates.** A non-ANN scoped lookup against a vector-indexed table full-scans. `memory_items` therefore needs its own btree index on `(scope_id, status)` **as well as** the vector index.
- **This pins the dimension only, never the vector space.** See §2.1 of `CLAUDE.md` and `docs/external-constraints.md` §4.1 — the space is Cohere `embed-english-v3.0`, and 1024-dim vectors from two different models are mutually incomparable.

## 3 — Every ANN query equality-constrains `scope_id`

Every ANN query must constrain `scope_id` with `=` or `IN`, and order by `embedding <=> $1 LIMIT k`. Without the equality predicate **the index is silently unused** — the query still returns rows, just from a full scan. This is the single most common way to ship a "vector search" that isn't one. The negative control in P0-P1 confirms it: dropping the `scope_id` predicate turned the same query into a `FULL SCAN`.

## 4 — `remediation_actions.idempotency_key` is `UNIQUE`

**That constraint — not application logic — is what makes double-apply impossible.** It is the exactly-once guarantee, enforced by the database, and it is what makes the kill-and-resume demo produce **one** action row where a naive agent produces two.

Never work around a uniqueness violation. Read it as *"this action was already intended"*, then reconcile against reality: look up the existing row, check whether the side effect actually landed, and continue from there. Replacing this with application-level bookkeeping is more code *and* weaker (`docs/coding-conduct.md`, Engram corollaries).

## 5 — Lease acquisition takes a row lock and bumps a monotonic fence token

`agent_leases` acquisition takes a **row lock on `task_id`** and bumps a monotonic `fence_token`; stale holders are rejected at write time.

The LLD implements it as `UPDATE … WHERE task_id=$1 AND expires_at < now()` followed by `INSERT … ON CONFLICT DO NOTHING`. The `UPDATE` takes the lock; only an *expired* lease can be taken over; a live holder's token is never reset; and the read-modify-write window is gone.

**The invariant is the row lock plus monotonicity, not that particular statement pair.** A different formulation that preserves both is acceptable; one that checks expiry outside a lock is not.

## 6 — Decision + intent-to-act + side-effect record commit in ONE transaction

The project's thesis expressed as a `BEGIN`/`COMMIT`: the reasoning that produced a decision, the record that the agent intended to act, and the record of the side effect are one atomic unit. If they can be observed apart, the audit trail can lie about what the agent did.

**Writing them separately? Stop.** This is the invariant most likely to be "simplified" away under time pressure, and it must not be.

## 7 — Row-Level TTL, with explicit `ON DELETE` on every child

`working_memory` 7 days · `observations` 30 days · `tasks` 90 days · LangGraph checkpoint tables TTL-enabled. **Forgetting is declared in DDL, not implemented in cron** — that is part of the memory-design argument, not an operational shortcut.

**Every FK referencing a TTL'd parent needs an explicit `ON DELETE` action** or the TTL job errors silently and rows stop expiring. Per LLD §6.2 (**fixed state, applied 2026-08-10**): `ON DELETE CASCADE` on `observations`, `decisions`, `tool_calls`, `remediation_actions`, `working_memory`, `agent_leases` **and `approvals`**; `ON DELETE SET NULL` on `procedures.created_by` and `tasks.parent_task_id`.

**`approvals` was added to that list on 2026-08-10** — it was missing here while carrying FKs into *two* TTL'd parents (`tasks` **and** `remediation_actions`, both 90-day), which is exactly the failure this invariant exists to prevent. It is the reason the rule is stated as "every FK to a TTL'd parent" and not as a fixed table list: **enumerate from the DDL, never from memory.** LLD test **T12** back-dates a task and asserts the TTL job actually reclaims it and cascades.

**Declaration order is part of the same fix.** `remediation_actions` must be created **before** `approvals` (`approvals.action_id` is a hard FK into it; `remediation_actions.approval_id` deliberately carries no FK, to avoid the cycle). That order is inside the Day-3 frozen contract.

## 8 — `AS OF SYSTEM TIME` is reserved for belief-state replay

It exists in this design to answer *"what did the agent believe at the moment it decided?"* — the audit feature. **It is not a performance trick** and must not be sprinkled onto read paths to dodge contention.

## 9 — Retrieval is hybrid, never pure cosine

```
0.45·similarity + 0.30·confidence + 0.15·recency + 0.10·entity_affinity
```

Hard-filter `confidence < 0.15` and `status <> 'active'` before ranking. Pure cosine ranking would surface a memory that *looks* similar over one that is *known to work*; the weights are the claim that this system ranks by usefulness, not by embedding distance alone.

## 10 — Confidence is a time-decayed Wilson lower bound

Confidence is a **Wilson lower bound** on `successes/attempts`, then time-decayed. **A 1/1 procedure must not outrank a 47/50 one** — a naive success ratio scores both 1.0 and would make the memory layer confidently wrong on first contact.

## 11 — Large artifacts go to S3, rows hold URI + hash

EXPLAIN bundles, plan diffs and execution traces go to **S3 `engram-agent-artifacts`**; the database row stores the `s3://` URI plus a content hash, never the blob. This protects the 10 GiB free-tier storage budget and is the AWS service on the agent's own path (`docs/external-constraints.md` §5).
