"""Engram · typed exception taxonomy (design/02-low-level-design.md §16).  [BRAINS + PLUMBER]

Grown incrementally as each module needs one — only `StaleLeaseError` exists
so far, because it is the only exception `agent/memory/db.py` (§6.1) raises.
The rest of the taxonomy (`LlmRateLimitError`, `EmbeddingDimensionError`,
`McpTimeoutError`, ...) lands with the modules that actually raise them.
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
