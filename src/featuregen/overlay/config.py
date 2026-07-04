"""Sealed server-side overlay configuration (SP-1.5 §3.1, §10).

Fixes 3 (renewal/TTL), 4 (drift driver), and 6 (profiler) all need configuration a CALLER cannot
forge (never from ``cmd.args``). ``OverlayConfig`` is constructed at deploy / ``register_overlay``
time and resolved through a fail-closed accessor, mirroring ``current_catalog_adapter``. Invalid
config is rejected at construction (fail-closed) rather than silently defaulted.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

_ZERO = timedelta(0)


class OverlayConfigError(ValueError):
    """An OverlayConfig value is out of range / internally inconsistent (fail-closed at deploy)."""


@dataclass(frozen=True, slots=True)
class ProfilerRule:
    """A table-granular profiler allow/deny rule (§9). A ``run_profiler`` target is permitted iff it
    matches an ``allow`` rule and no ``deny`` rule."""

    catalog_source: str
    schema: str
    table: str
    allow: bool


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    """Sealed overlay config. Validated on construction — see OverlayConfigError cases below."""

    ttl_default: timedelta
    ttl_min: timedelta
    ttl_max: timedelta
    ttl_jitter_fraction: float
    renewal_grace: timedelta
    drift_scan_interval: timedelta
    drift_freshness_sla: timedelta
    profiler_require_restricted_role: bool
    ttl_by_fact_type: Mapping[str, timedelta] = field(default_factory=dict)
    profiler_rules: tuple[ProfilerRule, ...] = ()

    def __post_init__(self) -> None:
        if self.ttl_min <= _ZERO:
            raise OverlayConfigError(f"ttl_min must be positive, got {self.ttl_min}")
        if self.ttl_max < self.ttl_min:
            raise OverlayConfigError(f"ttl_max {self.ttl_max} < ttl_min {self.ttl_min}")
        if not (self.ttl_min <= self.ttl_default <= self.ttl_max):
            raise OverlayConfigError(
                f"ttl_default {self.ttl_default} outside [{self.ttl_min}, {self.ttl_max}]"
            )
        for fact_type, ttl in self.ttl_by_fact_type.items():
            if not (self.ttl_min <= ttl <= self.ttl_max):
                raise OverlayConfigError(
                    f"ttl_by_fact_type[{fact_type!r}]={ttl} outside [{self.ttl_min}, {self.ttl_max}]"
                )
        if not (0.0 <= self.ttl_jitter_fraction < 1.0):
            raise OverlayConfigError(
                f"ttl_jitter_fraction must be in [0, 1), got {self.ttl_jitter_fraction}"
            )
        if self.renewal_grace <= _ZERO:
            raise OverlayConfigError(f"renewal_grace must be positive, got {self.renewal_grace}")
        if self.renewal_grace >= self.ttl_min:
            # else renewal (armed at expires_at - grace) would fall before confirmation for a
            # fact at the shortest allowed TTL.
            raise OverlayConfigError(
                f"renewal_grace {self.renewal_grace} must be < ttl_min {self.ttl_min}"
            )
        if self.drift_scan_interval <= _ZERO:
            raise OverlayConfigError(
                f"drift_scan_interval must be positive, got {self.drift_scan_interval}"
            )
        if self.drift_freshness_sla < self.drift_scan_interval:
            # reads must not fail closed on a watermark that is younger than one scan cadence.
            raise OverlayConfigError(
                f"drift_freshness_sla {self.drift_freshness_sla} must be >= "
                f"drift_scan_interval {self.drift_scan_interval}"
            )


# --- Sealed, fail-closed accessor (mirrors current_catalog_adapter) ------------------------
_OVERLAY_CONFIG: OverlayConfig | None = None


def register_overlay_config(config: OverlayConfig) -> None:
    """Seal the process-wide overlay config (deployment-injected; last writer wins)."""
    global _OVERLAY_CONFIG
    _OVERLAY_CONFIG = config


def current_overlay_config() -> OverlayConfig:
    """Return the sealed overlay config. Fails closed: raises RuntimeError if none is registered, so
    a command/stage that needs config never resolves against a silent default."""
    if _OVERLAY_CONFIG is None:
        raise RuntimeError(
            "no overlay config registered; call register_overlay_config(...) "
            "(register_overlay(config=...) does this in production)"
        )
    return _OVERLAY_CONFIG


def _clear_overlay_config() -> None:
    """Test-only reset of the module-global config (call from the overlay conftest)."""
    global _OVERLAY_CONFIG
    _OVERLAY_CONFIG = None
