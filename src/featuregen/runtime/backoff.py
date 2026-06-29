from __future__ import annotations

import random


def compute_backoff(
    attempts: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 3600.0,
    jitter: float = 0.5,
) -> float:
    """Exponential backoff with cap + symmetric jitter (§5.6 delivery retry).

    attempts < 1 is treated as the first attempt. Deterministic when jitter == 0.0.
    """
    n = attempts if attempts >= 1 else 1
    raw = min(base_seconds * (2 ** (n - 1)), cap_seconds)
    if jitter <= 0.0:
        return raw
    delta = raw * jitter
    return max(0.0, raw + random.uniform(-delta, delta))
