"""Engram · workers/common/scoring.py — duplicated pure scoring helpers for `decayer`/`consolidator`.  [PLUMBER]

design/02-low-level-design.md §6.6/§9 (invariants #9, #10). `wilson_lb` and the 90-day
time-decay multiplier are copied (not imported) from `agent/memory/scoring.py` -- same reason
`workers/common/incident.py` copies `normalize_query_text`/`fingerprint`: two small, pure,
dependency-free functions are cheaper and more honest to duplicate here than to reach across the
`workers`/`agent` boundary this project has otherwise kept clean. A canary test
(`tests/test_workers_decayer.py`) asserts the formula stays byte-for-byte in lockstep with
`agent/memory/scoring.py`'s own `wilson_lb`, the same lockstep discipline
`agent/telemetry.py`'s `METRIC_UNITS` already uses against `workers/metrics/handler.py`.
"""

from __future__ import annotations

from math import exp, sqrt

CONFIDENCE_FLOOR = 0.15  # invariant #9's hard filter; also decayer's retire threshold


def wilson_lb(successes: int, attempts: int, z: float = 1.96) -> float:
    if attempts == 0:
        return 0.0
    p = successes / attempts
    denom = 1 + z * z / attempts
    centre = p + z * z / (2 * attempts)
    margin = z * sqrt((p * (1 - p) + z * z / (4 * attempts)) / attempts)
    return max(0.0, (centre - margin) / denom)


def decayed_confidence(successes: int, attempts: int, age_days: float, z: float = 1.96) -> float:
    """LLD §9's exact formula: `wilson(successes,attempts) * exp(-(now()-updated_at)/90d)`."""
    return wilson_lb(successes, attempts, z) * exp(-age_days / 90)
