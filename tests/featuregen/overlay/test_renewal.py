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
from featuregen.overlay._lifecycle import within_renewal_grace
from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
from featuregen.overlay.catalog import register_catalog_adapter
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref, fact_key
from featuregen.overlay.projection import OverlayProjection, current_fact
from featuregen.overlay.state import OverlayState
from featuregen.overlay.store import append_overlay_event
from featuregen.projections.runner import run_projection


def _register_config(grace_days=14):
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=grace_days),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=False,
    ))


def test_within_renewal_grace_math():
    now = datetime.now(UTC)

    def st(status="VERIFIED", days=1):
        return OverlayState(status=status, expires_at=(now + timedelta(days=days)).isoformat())

    assert within_renewal_grace(st(days=1), now) is False  # no config -> off (backward-compat)
    _register_config(grace_days=14)
    assert within_renewal_grace(st(days=1), now) is True  # within 14d grace
    assert within_renewal_grace(st(days=100), now) is False  # far from expiry
    assert within_renewal_grace(st("REVERIFY", days=1), now) is False  # not VERIFIED
    past = OverlayState(status="VERIFIED", expires_at=(now - timedelta(days=1)).isoformat())
    assert within_renewal_grace(past, now) is True  # past expiry is still re-confirmable


def _stack(conn, ref, owner):
    register_overlay(_SeedRegistry())
    seed_authz_policy(conn)
    seed_overlay_authz(conn)
    register_command_authorizer(PolicyAuthorizer())
    cat = StubCatalog()
    cat.set_owner(ref, owner)
    register_catalog_adapter(cat)


def _seed_near_expiry(conn, *, ref, proposed_by, confirmer, days_to_expiry=1):
    """Hand-seed a VERIFIED fact whose expires_at is `days_to_expiry` out (inside grace), with a
    chosen `proposed_by` (four-eyes) and confirmer."""
    key = fact_key(ref, "grain")
    proposer_actor = mint_test_service_identity(
        subject=proposed_by, role_claims=("overlay",), attestation="att"
    ) if proposed_by.startswith("service:") else mint_test_identity(
        subject=proposed_by, role_claims=("data_owner",)
    )
    draft = append_overlay_event(
        conn, fact_key=key, type=facts.OVERLAY_FACT_PROPOSED, actor=proposer_actor,
        expected_version=0,
        payload={
            "catalog_object_ref": {"catalog_source": "fixture", "object_kind": "table",
                                   "schema": "core", "table": "t"},
            "object_ref": display_object_ref(ref), "fact_type": "grain",
            "proposed_value": {"columns": ["id"], "is_unique": True},
            "proposal_fingerprint": "fp", "proposed_by": proposed_by,
        },
    )
    exp = (datetime.now(UTC) + timedelta(days=days_to_expiry)).isoformat()
    append_overlay_event(
        conn, fact_key=key, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=mint_test_identity(subject=confirmer, role_claims=("data_owner",)),
        payload={
            "value": {"columns": ["id"], "is_unique": True},
            "confirmers": [{"subject": confirmer, "role": "data_owner"}],
            "expires_at": exp, "confirms_event_id": draft.event_id,
        },
    )
    run_projection(conn, OverlayProjection())
    return key


def _renew(conn, ref, renewer, target_event_id):
    return execute_command(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id},
        mint_test_identity(subject=renewer, role_claims=("data_owner",)),
        "ik-renew",
    ))


def test_verified_fact_renews_within_grace(db):
    ref = CatalogObjectRef(catalog_source="fixture", object_kind="table", schema="core", table="t")
    _stack(db, ref, owner="user:owner-a")
    _register_config(grace_days=14)
    key = _seed_near_expiry(db, ref=ref, proposed_by="service:seed", confirmer="user:owner-a")
    before = current_fact(db, key)

    # owner-a renews (owner-a != the service proposer -> four-eyes ok).
    res = _renew(db, ref, "user:owner-a", before["confirmed_event_id"])
    assert res.accepted, res.denied_reason
    run_projection(db, OverlayProjection())
    after = current_fact(db, key)

    assert after["status"] == "VERIFIED"  # never left VERIFIED — no outage
    assert after["confirmed_event_id"] != before["confirmed_event_id"]  # advanced
    assert after["expires_at"] > before["expires_at"]  # horizon pushed out (~now + 180d)


def test_renewal_preserves_four_eyes(db):
    # F8: a self-entered fact (proposed_by == the owner) cannot be renewed by that same owner.
    ref = CatalogObjectRef(catalog_source="fixture", object_kind="table", schema="core", table="t")
    _stack(db, ref, owner="user:owner-self")
    _register_config(grace_days=14)
    key = _seed_near_expiry(db, ref=ref, proposed_by="user:owner-self", confirmer="user:owner-self")
    before = current_fact(db, key)

    res = _renew(db, ref, "user:owner-self", before["confirmed_event_id"])
    assert not res.accepted
    assert "four-eyes" in (res.denied_reason or "")


def test_verified_fact_not_renewable_outside_grace(db):
    ref = CatalogObjectRef(catalog_source="fixture", object_kind="table", schema="core", table="t")
    _stack(db, ref, owner="user:owner-a")
    _register_config(grace_days=14)
    key = _seed_near_expiry(db, ref=ref, proposed_by="service:seed", confirmer="user:owner-a",
                            days_to_expiry=120)  # far from expiry -> outside grace
    before = current_fact(db, key)

    res = _renew(db, ref, "user:owner-a", before["confirmed_event_id"])
    assert not res.accepted
    assert "not awaiting confirmation" in (res.denied_reason or "")


def test_renewal_poller_opens_task_and_is_idempotent(db):
    # SP-1.5 Task 6: the poller opens ONE re-verify task for a within-grace fact, and a re-run opens
    # none (the NOT EXISTS open-task guard — no duplicate, the F1 case).
    from featuregen.overlay.expiry import fire_due_overlay_renewals

    ref = CatalogObjectRef(catalog_source="fixture", object_kind="table", schema="core", table="t")
    _stack(db, ref, owner="user:owner-a")
    _register_config(grace_days=14)
    _seed_near_expiry(db, ref=ref, proposed_by="service:seed", confirmer="user:owner-a")
    now = datetime.now(UTC)

    assert fire_due_overlay_renewals(db, now=now) == 1  # within grace, no task yet -> opens one
    assert fire_due_overlay_renewals(db, now=now) == 0  # task now open -> idempotent, no duplicate


def test_renewal_poller_skips_far_from_expiry(db):
    from featuregen.overlay.expiry import fire_due_overlay_renewals

    ref = CatalogObjectRef(catalog_source="fixture", object_kind="table", schema="core", table="t")
    _stack(db, ref, owner="user:owner-a")
    _register_config(grace_days=14)
    _seed_near_expiry(db, ref=ref, proposed_by="service:seed", confirmer="user:owner-a",
                      days_to_expiry=120)  # far outside the 14d grace window

    assert fire_due_overlay_renewals(db, now=datetime.now(UTC)) == 0
