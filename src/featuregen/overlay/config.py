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


def overlay_config_from_env(env: Mapping[str, str] | None = None) -> OverlayConfig:
    """Build the sealed OverlayConfig from the deployment environment (single-node defaults). Wired
    by register_overlay in PRODUCTION so the SP-1.5 guards (drift-freshness, renewal, referent
    validation, profiler policy) are ACTIVE rather than silently off. Invalid combinations raise
    OverlayConfigError at deploy (fail-closed). profiler_rules default to EMPTY -> the profiler
    default-denies every target until a deployment configures OVERLAY_PROFILER_RULES (fail-safe)."""
    import json
    import os

    e = os.environ if env is None else env

    def _days(key: str, default: float) -> timedelta:
        return timedelta(days=float(e.get(key, default)))

    def _mins(key: str, default: float) -> timedelta:
        return timedelta(minutes=float(e.get(key, default)))

    def _strict_bool(raw: object, key: str) -> bool:
        # Robust + strict (review #6/#10): a JSON bool passes through; a string is parsed against a
        # KNOWN set; anything else (a typo like "ture", a number) FAILS at boot rather than silently
        # becoming True (bool("false") == True) or False (an unrecognized truthy value).
        if isinstance(raw, bool):
            return raw
        token = str(raw).strip().lower()
        if token in ("true", "1", "yes"):
            return True
        if token in ("false", "0", "no", ""):
            return False
        raise OverlayConfigError(f"{key} must be a boolean, got {raw!r}")

    rules_raw = e.get("OVERLAY_PROFILER_RULES", "")
    rules = tuple(
        ProfilerRule(r["catalog_source"], r["schema"], r["table"],
                     _strict_bool(r["allow"], "profiler rule 'allow'"))
        for r in (json.loads(rules_raw) if rules_raw else [])
    )
    return OverlayConfig(
        ttl_default=_days("OVERLAY_TTL_DEFAULT_DAYS", 180),
        ttl_min=_days("OVERLAY_TTL_MIN_DAYS", 30),
        ttl_max=_days("OVERLAY_TTL_MAX_DAYS", 365),
        ttl_jitter_fraction=float(e.get("OVERLAY_TTL_JITTER_FRACTION", 0.1)),
        renewal_grace=_days("OVERLAY_RENEWAL_GRACE_DAYS", 14),
        drift_scan_interval=_mins("OVERLAY_DRIFT_SCAN_INTERVAL_MIN", 15),
        drift_freshness_sla=_mins("OVERLAY_DRIFT_FRESHNESS_SLA_MIN", 60),
        profiler_require_restricted_role=_strict_bool(
            e.get("OVERLAY_PROFILER_REQUIRE_RESTRICTED_ROLE", "false"),
            "OVERLAY_PROFILER_REQUIRE_RESTRICTED_ROLE",
        ),
        profiler_rules=rules,
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
