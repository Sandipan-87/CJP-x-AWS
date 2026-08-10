"""Engram · agent/memory/leases.py — retry/backoff/jitter policy.  [PLUMBER]

design/02-low-level-design.md §6.4 (invariant #5). `agent/memory/db.py`'s
`acquire_lease`/`takeover_lease`/`renew_lease`/`release_lease` are each ONE
attempt — no loop, no sleep, by design (see `db.py`'s own module docstring).
This module is that loop: the runbook comment §6.4 left unimplemented,
"if total affected rows == 1 -> we hold the lease. Else back off with
jitter (1-3s) and retry."

`LeaseHandle` is the caller-side lease-verification point LLD §6.4's DAO
note names as the alternative to threading `fence_token` through every
other mutating DAO call ("...or the caller verifies lease before write") —
`db.py` chose that alternative; this class is what makes it real: hold a
handle for the lifetime of a task's work, check `.is_valid` (or await
`.wait_until_lost()`) before any write that matters, and the background
renew loop parks the task the moment the lease is actually gone instead of
letting every future write silently no-op.
"""

from __future__ import annotations

import asyncio
import random

from agent.errors import LeaseAcquireTimeoutError, StaleLeaseError
from agent.memory.db import Database

DEFAULT_MAX_ATTEMPTS = 20
DEFAULT_JITTER_S = (1.0, 3.0)          # LLD §6.4's own numbers, verbatim
DEFAULT_RENEW_INTERVAL_S = 15          # LLD §2 config contract: ENGRAM_LEASE_RENEW_S


class LeaseHandle:
    """A held lease, auto-renewing in the background until released or lost.

    Not constructed directly — use `acquire()` or `takeover()` below, both
    of which return an already-started handle.
    """

    def __init__(
        self,
        db: Database,
        task_id: str,
        holder_id: str,
        fence_token: int,
        *,
        renew_interval_s: float = DEFAULT_RENEW_INTERVAL_S,
    ) -> None:
        self.task_id = task_id
        self.holder_id = holder_id
        self.fence_token = fence_token
        self._db = db
        self._renew_interval_s = renew_interval_s
        self._lost = asyncio.Event()
        self._renew_task: asyncio.Task | None = None

    @property
    def is_valid(self) -> bool:
        return not self._lost.is_set()

    async def wait_until_lost(self) -> None:
        """Blocks until the background renew loop observes a StaleLeaseError.
        A graph node awaiting this alongside its own work is how a lost
        lease actually interrupts in-flight work, not just future writes.
        """
        await self._lost.wait()

    def _start(self) -> None:
        self._renew_task = asyncio.ensure_future(self._renew_loop())

    async def _renew_loop(self) -> None:
        try:
            while not self._lost.is_set():
                await asyncio.sleep(self._renew_interval_s)
                if self._lost.is_set():
                    return
                try:
                    await self._db.renew_lease(self.task_id, self.holder_id, self.fence_token)
                except StaleLeaseError:
                    self._lost.set()
                    return
        except asyncio.CancelledError:
            pass  # normal path: release() cancels this on a clean exit

    async def release(self) -> None:
        """SIGTERM/normal-exit path. A no-op if the lease was already lost —
        releasing something you no longer hold would just affect 0 rows, so
        skip the call rather than let it silently do nothing.
        """
        if self._renew_task is not None:
            self._renew_task.cancel()
            try:
                await self._renew_task
            except asyncio.CancelledError:
                pass
        if self.is_valid:
            await self._db.release_lease(self.task_id, self.holder_id)

    async def __aenter__(self) -> "LeaseHandle":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()


async def _acquire_with_retry(
    db: Database,
    task_id: str,
    holder_id: str,
    *,
    takeover: bool,
    max_attempts: int,
    jitter_s: tuple[float, float],
    renew_interval_s: float,
) -> LeaseHandle:
    call = db.takeover_lease if takeover else db.acquire_lease
    for attempt in range(1, max_attempts + 1):
        won, fence_token = await call(task_id, holder_id)
        if won:
            handle = LeaseHandle(db, task_id, holder_id, fence_token,
                                  renew_interval_s=renew_interval_s)
            handle._start()
            return handle
        await asyncio.sleep(random.uniform(*jitter_s))
    raise LeaseAcquireTimeoutError(task_id, holder_id, max_attempts)


async def acquire(
    db: Database,
    task_id: str,
    holder_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    jitter_s: tuple[float, float] = DEFAULT_JITTER_S,
    renew_interval_s: float = DEFAULT_RENEW_INTERVAL_S,
) -> LeaseHandle:
    """First claim of a task. See `db.py`'s module docstring for why this and
    `takeover()` share one underlying DB call — only the demo narrative
    (first claim vs. reclaiming after a kill) tells them apart."""
    return await _acquire_with_retry(
        db, task_id, holder_id, takeover=False,
        max_attempts=max_attempts, jitter_s=jitter_s, renew_interval_s=renew_interval_s,
    )


async def takeover(
    db: Database,
    task_id: str,
    holder_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    jitter_s: tuple[float, float] = DEFAULT_JITTER_S,
    renew_interval_s: float = DEFAULT_RENEW_INTERVAL_S,
) -> LeaseHandle:
    """Reclaiming a lease after the previous holder died (the kill-and-resume
    demo beat: `aws ecs stop-task` mid-remediation, then a new task resumes).
    """
    return await _acquire_with_retry(
        db, task_id, holder_id, takeover=True,
        max_attempts=max_attempts, jitter_s=jitter_s, renew_interval_s=renew_interval_s,
    )
