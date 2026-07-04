from __future__ import annotations

from datetime import timedelta

import pytest

from featuregen.overlay.config import (
    OverlayConfig,
    OverlayConfigError,
    ProfilerRule,
    current_overlay_config,
    register_overlay_config,
)


def _config(**overrides) -> OverlayConfig:
    base = dict(
        ttl_by_fact_type={"availability_time": timedelta(days=90)},
        ttl_default=timedelta(days=180),
        ttl_min=timedelta(days=30),  # must exceed renewal_grace so renewal never arms in the past
        ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1,
        renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15),
        drift_freshness_sla=timedelta(minutes=60),
        profiler_rules=(ProfilerRule("pg:core", "public", "customers", allow=True),),
        profiler_require_restricted_role=True,
    )
    base.update(overrides)
    return OverlayConfig(**base)


def test_current_overlay_config_fails_closed_when_unregistered():
    # Mirrors current_catalog_adapter: no silent default — a stage that needs config it cannot
    # resolve must fail closed (SP-1.5 §3.1).
    with pytest.raises(RuntimeError):
        current_overlay_config()


def test_register_and_resolve_round_trips():
    cfg = _config()
    register_overlay_config(cfg)
    assert current_overlay_config() is cfg


def test_valid_config_constructs():
    cfg = _config()
    assert cfg.ttl_default == timedelta(days=180)
    assert cfg.profiler_require_restricted_role is True


def test_register_overlay_seals_config():
    # The composition root wires the config the same way it wires the catalog adapter (§10).
    from featuregen.overlay.bootstrap import register_overlay
    from featuregen.runtime.handlers import HandlerRegistry

    cfg = _config()
    register_overlay(HandlerRegistry(), config=cfg)
    assert current_overlay_config() is cfg


@pytest.mark.parametrize(
    "overrides",
    [
        {"ttl_default": timedelta(days=400)},  # > ttl_max
        {"ttl_default": timedelta(hours=1)},  # < ttl_min
        {"ttl_min": timedelta(days=400)},  # ttl_min > ttl_max
        {"ttl_jitter_fraction": 1.0},  # not in [0, 1)
        {"ttl_jitter_fraction": -0.1},
        {"drift_freshness_sla": timedelta(minutes=5)},  # < drift_scan_interval
        {"ttl_by_fact_type": {"x": timedelta(days=999)}},  # value outside [min, max]
        {"renewal_grace": timedelta(days=2, hours=1), "ttl_min": timedelta(days=2)},  # grace >= ttl_min
        {"drift_scan_interval": timedelta(0)},  # non-positive
    ],
)
def test_out_of_range_config_is_rejected_fail_closed(overrides):
    # Invalid config is rejected at construction (register_overlay time), never silently defaulted.
    with pytest.raises(OverlayConfigError):
        _config(**overrides)
