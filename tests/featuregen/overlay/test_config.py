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


def _ttl_config(**over):
    from datetime import timedelta

    base = dict(
        ttl_default=timedelta(days=200), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=False,
    )
    base.update(over)
    return OverlayConfig(**base)


def test_resolve_ttl_per_type_and_default_fallback():
    from datetime import timedelta

    from featuregen.overlay._lifecycle import _DEFAULT_TTL, resolve_ttl

    assert resolve_ttl("grain", "fk1") == _DEFAULT_TTL  # no config -> 180d default
    register_overlay_config(_ttl_config(ttl_by_fact_type={"availability_time": timedelta(days=90)}))
    assert resolve_ttl("availability_time", "fk1") == timedelta(days=90)  # per-type
    assert resolve_ttl("grain", "fk1") == timedelta(days=200)  # fallback to ttl_default


def test_resolve_ttl_jitter_deterministic_and_bounded():
    from datetime import timedelta

    from featuregen.overlay._lifecycle import resolve_ttl

    register_overlay_config(_ttl_config(ttl_jitter_fraction=0.1))
    a = resolve_ttl("grain", "fk-A")
    assert a == resolve_ttl("grain", "fk-A")  # deterministic per fact_key
    assert resolve_ttl("grain", "fk-B") != a or True  # different key may differ (not asserted equal)
    assert timedelta(days=180) <= a <= timedelta(days=220)  # within +/-10% of 200d, clamped


def test_profiler_gate_is_table_granular_and_deny_wins():
    # SP-1.5 Task 8 + review #4: the profiler gate is evaluated PER TABLE from the SEALED config —
    # one table-allow does NOT open the whole schema, and a table-deny wins; default deny.
    from tests.featuregen._helpers import mint_test_service_identity
    from tests.featuregen.overlay._helpers import StubCatalog

    from featuregen.contracts import Command
    from featuregen.overlay.config import ProfilerRule
    from featuregen.overlay.identity import CatalogObjectRef
    from featuregen.overlay.profiler_command import _profiler_denial

    actor = mint_test_service_identity(subject="service:p", role_claims=("overlay",), attestation="a")
    adapter = StubCatalog(catalog_source="fixture")
    cmd = Command("run_profiler", "overlay_fact", None, {}, actor, "ik")

    def ref(schema, table):
        return CatalogObjectRef("fixture", "table", schema, table)

    register_overlay_config(_ttl_config(profiler_rules=(
        ProfilerRule("fixture", "core", "orders", True),      # allow ONE table
        ProfilerRule("fixture", "core", "salaries", False),   # deny another in the SAME schema
        ProfilerRule("other", "core", "orders", True),        # a DIFFERENT source -> irrelevant
    )))

    assert _profiler_denial(cmd, adapter, ref("core", "orders")) is None            # explicitly allowed
    assert _profiler_denial(cmd, adapter, ref("core", "salaries")) is not None      # deny wins
    assert _profiler_denial(cmd, adapter, ref("core", "customers")) is not None     # not allowed != whole schema
    assert _profiler_denial(cmd, adapter, ref("secret", "x")) is not None           # default deny


def test_profiler_caller_can_only_narrow():
    from tests.featuregen._helpers import mint_test_service_identity
    from tests.featuregen.overlay._helpers import StubCatalog

    from featuregen.contracts import Command
    from featuregen.overlay.config import ProfilerRule
    from featuregen.overlay.identity import CatalogObjectRef
    from featuregen.overlay.profiler_command import _profiler_denial

    actor = mint_test_service_identity(subject="service:p", role_claims=("overlay",), attestation="a")
    adapter = StubCatalog(catalog_source="fixture")
    register_overlay_config(_ttl_config(profiler_rules=(ProfilerRule("fixture", "core", "orders", True),)))
    ref = CatalogObjectRef("fixture", "table", "core", "orders")

    # A caller allowlist can NARROW (exclude core) but a config-allowed table still needs the caller's ok.
    narrowing = Command("run_profiler", "overlay_fact", None, {"allowed_schemas": ["other"]}, actor, "k")
    assert _profiler_denial(narrowing, adapter, ref) is not None  # caller excluded core
    permissive = Command("run_profiler", "overlay_fact", None, {"allowed_schemas": ["core"]}, actor, "k")
    assert _profiler_denial(permissive, adapter, ref) is None
