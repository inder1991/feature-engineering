from __future__ import annotations

from featuregen.runtime.backoff import compute_backoff


def test_doubling_without_jitter() -> None:
    assert compute_backoff(1, base_seconds=2.0, jitter=0.0) == 2.0
    assert compute_backoff(2, base_seconds=2.0, jitter=0.0) == 4.0
    assert compute_backoff(3, base_seconds=2.0, jitter=0.0) == 8.0


def test_cap_applied() -> None:
    assert compute_backoff(40, base_seconds=1.0, cap_seconds=60.0, jitter=0.0) == 60.0


def test_floor_on_zero_or_negative_attempts() -> None:
    # treated as the first attempt
    assert compute_backoff(0, base_seconds=1.0, jitter=0.0) == 1.0
    assert compute_backoff(-5, base_seconds=1.0, jitter=0.0) == 1.0


def test_jitter_stays_within_bounds_and_nonnegative() -> None:
    for _ in range(200):
        v = compute_backoff(3, base_seconds=2.0, cap_seconds=100.0, jitter=0.5)
        # raw=8.0; ±50% => [4.0, 12.0]; never negative
        assert 0.0 <= v <= 12.0
        assert v >= 4.0
