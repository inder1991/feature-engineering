from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity
from tests.featuregen.overlay._helpers import StubCatalog, _SeedRegistry

from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.contracts import Command
from featuregen.overlay import facts
from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
from featuregen.overlay.catalog import CatalogObject, register_catalog_adapter
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.store import append_overlay_event
from featuregen.projections.runner import run_projection

REF = CatalogObjectRef(catalog_source="fixture", object_kind="table", schema="core", table="t")


def _table_obj():
    return CatalogObject("core.t", "table", "core", "t", None, None, "1")


def _col_obj():
    return CatalogObject("core.t.id", "column", "core", "t", "id", "integer", "1:1")


def _config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=False,
    ))


def _stack(conn, adapter):
    register_overlay(_SeedRegistry())
    seed_authz_policy(conn)
    seed_overlay_authz(conn)
    register_command_authorizer(PolicyAuthorizer())
    register_catalog_adapter(adapter)


def _seed_staled(conn):
    key = fact_key(REF, "grain")
    svc = mint_test_service_identity(subject="service:seed", role_claims=("overlay",), attestation="a")
    draft = append_overlay_event(
        conn, fact_key=key, type=facts.OVERLAY_FACT_PROPOSED, actor=svc, expected_version=0,
        payload={
            "catalog_object_ref": {"catalog_source": "fixture", "object_kind": "table",
                                   "schema": "core", "table": "t"},
            "object_ref": "core.t", "fact_type": "grain",
            "proposed_value": {"columns": ["id"], "is_unique": True},
            "proposal_fingerprint": "fp", "proposed_by": "service:seed",
        },
    )
    confirmed = append_overlay_event(
        conn, fact_key=key, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=mint_test_identity(subject="user:owner-a", role_claims=("data_owner",)),
        payload={
            "value": {"columns": ["id"], "is_unique": True},
            "confirmers": [{"subject": "user:owner-a", "role": "data_owner"}],
            "expires_at": (datetime.now(UTC) + timedelta(days=180)).isoformat(),
            "confirms_event_id": draft.event_id,
        },
    )
    append_overlay_event(
        conn, fact_key=key, type=facts.OVERLAY_FACT_STALED, actor=svc,
        payload={"catalog_change_ref": "drop:core.t.id",
                 "stales_confirmed_event_id": confirmed.event_id},
    )
    run_projection(conn, OverlayProjection())
    return confirmed.event_id


def _reconfirm(conn, target):
    return execute_command(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": REF, "fact_type": "grain", "target_event_id": target},
        mint_test_identity(subject="user:owner-a", role_claims=("data_owner",)), "ik-reconf",
    ))


def test_stale_reconfirm_blocked_when_referent_missing(db):
    # SP-1.5 Task 7: column core.t.id is gone -> re-confirming the STALEd grain is rejected.
    adapter = StubCatalog(objects=[_table_obj()])  # table present, column MISSING
    adapter.set_owner(REF, "user:owner-a")
    _stack(db, adapter)
    _config()
    res = _reconfirm(db, _seed_staled(db))
    assert not res.accepted
    assert "referent no longer in catalog" in (res.denied_reason or "")


def test_stale_reconfirm_allowed_when_referent_restored(db):
    adapter = StubCatalog(objects=[_table_obj(), _col_obj()])  # both present
    adapter.set_owner(REF, "user:owner-a")
    _stack(db, adapter)
    _config()
    res = _reconfirm(db, _seed_staled(db))
    assert res.accepted, res.denied_reason


def test_stale_reconfirm_ungated_without_config(db):
    # Backward-compat: with no OverlayConfig sealed, the referent check is OFF (re-confirm allowed).
    adapter = StubCatalog(objects=[_table_obj()])  # column missing, but no config -> not checked
    adapter.set_owner(REF, "user:owner-a")
    _stack(db, adapter)
    res = _reconfirm(db, _seed_staled(db))
    assert res.accepted, res.denied_reason
