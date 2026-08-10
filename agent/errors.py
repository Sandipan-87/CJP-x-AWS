"""Engram · typed exception taxonomy (design/02-low-level-design.md §16).  [BRAINS + PLUMBER]

Grown incrementally as each module needs one. `StaleLeaseError` is raised by
`agent/memory/db.py` (§6.1); `LeaseAcquireTimeoutError` by `agent/memory/
leases.py` (§6.4's retry/backoff policy layer); `EmbeddingProviderError` and
`EmbeddingDimensionError` by `agent/providers/cohere_embed.py` (§7). The rest
of the taxonomy (`LlmRateLimitError`, `McpTimeoutError`, ...) lands with the
modules that actually raise them.
"""

from __future__ import annotations


class EngramError(Exception):
    """Base for every typed exception in the taxonomy. Never raised directly."""


class StaleLeaseError(EngramError):
    """A write was attempted by a holder/fence_token that no longer owns the lease.

    LLD §6.4: "every mutating DAO call accepts holder_id+fence_token and the
    SQL adds AND fence_token = $expected ... A stale holder's write affects 0
    rows -> typed StaleLeaseError -> park." Recovery per §16: park (another
    holder owns the task) -- never retry blindly, the task is no longer ours.
    """

    def __init__(self, task_id: str, holder_id: str, fence_token: int | None = None) -> None:
        self.task_id = task_id
        self.holder_id = holder_id
        self.fence_token = fence_token
        super().__init__(
            f"stale lease: holder={holder_id!r} fence_token={fence_token!r} "
            f"no longer owns task_id={task_id!r}"
        )


class LeaseAcquireTimeoutError(EngramError):
    """Never won the lease after retrying — a live holder held it the whole
    time. Distinct from `StaleLeaseError` (losing a lease already held):
    this is failing to ever get one, not losing one — LLD §6.4's runbook
    comment ("back off with jitter and retry") caps out somewhere, and this
    is what it raises when it does.
    """

    def __init__(self, task_id: str, holder_id: str, attempts: int) -> None:
        self.task_id = task_id
        self.holder_id = holder_id
        self.attempts = attempts
        super().__init__(
            f"could not acquire lease for task_id={task_id!r} as holder={holder_id!r} "
            f"after {attempts} attempt(s) — a live holder held it throughout"
        )


class EmbeddingProviderError(EngramError):
    """Transient embedding failure (throttle, timeout, transport, auth) —
    raised only after retries are exhausted, never on the first failure.

    Recovery per §16: backoff x3 (done inside the provider, before this is
    ever raised) then DEGRADE — the caller (`agent/memory/embeddings.py`,
    not yet written) writes the row with `embedding IS NULL`; the backfill
    worker fills it later. **There is no fallback embedder** — a different
    model is a different vector space (invariant #2, HLD §3 D9/D12).
    """


class EmbeddingDimensionError(EngramError):
    """A returned vector's width is not exactly 1024.

    Recovery per §16: **never degrade, never write** — park immediately.
    A wrong width must not reach a `VECTOR(1024)` column. Unlike
    `EmbeddingProviderError`, this is never retried — a model returning the
    wrong width is a configuration bug, not a transient condition.
    """

    def __init__(self, got: int, expected: int = 1024) -> None:
        self.got = got
        self.expected = expected
        super().__init__(f"embedding dimension mismatch: got {got}, expected {expected}")
