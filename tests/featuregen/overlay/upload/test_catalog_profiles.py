"""Release-A profile Task 2 — catalog narrative store, table projections, reconciliation.

Proves: deterministic content-derived revision ids (provenance is NOT identity), the bridge-store
CAS pointer discipline (fail-closed on any drift), read-back content verification (corruption
raises, never serves), bounded fields refused BEFORE persistence, the derived catalog visibility
for narrative reads, the table-node display projections riding the existing resolver + the
Task-0.6 unconditional re-projection (a re-upload cannot blank them), and the read-only
reconciliation report detecting a tampered projection.
"""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_profiles import (
    CatalogProfileError,
    build_catalog_profile_revision,
    parse_narrative_payload,
)
from featuregen.overlay.upload.dataset_profiles import build_dataset_profile
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_store import (
    CatalogProfileConflict,
    catalog_visible,
    current_catalog_profile,
    current_catalog_profile_revision_id,
    record_catalog_profile_revision,
    set_current_catalog_profile,
)

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))

_SRC = "bank"
_TABLE_OBJECT_REF = "public.orders"
_TABLE_REF = normalize_ref(_SRC, None, "orders")

_SCRIPT = Path(__file__).parents[4] / "scripts" / "profile_reconcile.py"


def _reconcile_module():
    spec = importlib.util.spec_from_file_location("profile_reconcile", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _revision(**kw):
    defaults = dict(catalog_source=_SRC, display_name="Bank Core",
                    description="Core banking catalog.", business_context="Retail book.",
                    business_domains=("retail", "payments"), producer_ref="user:priya")
    defaults.update(kw)
    return build_catalog_profile_revision(**defaults)


def _seed_graph(db):
    build_graph(db, _SRC, [CanonicalRow(_SRC, "orders", "amount", "numeric")])


def _correct(db, field, action, actor, idem, **kw):
    cas = read_field_cas(db, source=_SRC, object_ref=_TABLE_OBJECT_REF, field=field)
    res = apply_field_correction(
        db, source=_SRC, object_ref=_TABLE_OBJECT_REF, field=field, action=action, actor=actor,
        idempotency_key=idem, expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **kw)
    assert res["accepted"] is True, res
    return res


def _confirm_authority(db, value="system_of_record"):
    _correct(db, "authority_role", "propose_override", ADMIN_A, f"ar-p-{value}",
             replacement_value=value)
    _correct(db, "authority_role", "confirm_override", ADMIN_B, f"ar-c-{value}",
             replacement_value=value)


# ── revision identity ────────────────────────────────────────────────────────────────────────────


def test_revision_id_is_deterministic_from_content():
    assert _revision().revision_id == _revision().revision_id
    assert _revision().revision_id.startswith("cpr_")


def test_provenance_is_not_identity():
    """producer_ref / ingestion_run_id never move the revision id; the D2 axes DO."""
    base = _revision()
    assert _revision(producer_ref="user:sam").revision_id == base.revision_id
    assert _revision(ingestion_run_id="run-42").revision_id == base.revision_id
    assert _revision(strength="confirmed").revision_id != base.revision_id
    assert _revision(description="Different.").revision_id != base.revision_id


def test_bounds_are_enforced_before_persistence():
    with pytest.raises(CatalogProfileError):
        _revision(display_name="x" * 201)
    with pytest.raises(CatalogProfileError):
        _revision(description="x" * 4001)
    with pytest.raises(CatalogProfileError):
        _revision(business_domains=tuple(f"d{i}" for i in range(33)))
    with pytest.raises(CatalogProfileError):
        parse_narrative_payload({"display_name": "ok", "surprise_key": 1})
    with pytest.raises(CatalogProfileError):
        parse_narrative_payload({"business_domains": ["ok", 7]})
    with pytest.raises(CatalogProfileError):
        parse_narrative_payload(["not", "an", "object"])


# ── store: CAS pointer + read-back verification ──────────────────────────────────────────────────


def test_record_and_current_pointer_roundtrip(db):
    rev = _revision()
    record_catalog_profile_revision(db, rev)
    assert set_current_catalog_profile(
        db, catalog_source=_SRC, revision_id=rev.revision_id, expected_pointer_version=0) == 1
    current = current_catalog_profile(db, _SRC)
    assert current is not None
    stored, pointer = current
    assert stored == rev
    assert pointer.pointer_version == 1
    assert current_catalog_profile_revision_id(db, _SRC) == rev.revision_id


def test_pointer_cas_fails_closed_on_any_drift(db):
    rev = _revision()
    record_catalog_profile_revision(db, rev)
    set_current_catalog_profile(db, catalog_source=_SRC, revision_id=rev.revision_id,
                                expected_pointer_version=0)
    with pytest.raises(CatalogProfileConflict):   # stale first-write claim
        set_current_catalog_profile(db, catalog_source=_SRC, revision_id=rev.revision_id,
                                    expected_pointer_version=0)
    with pytest.raises(CatalogProfileConflict):   # stale expected version
        set_current_catalog_profile(db, catalog_source=_SRC, revision_id=rev.revision_id,
                                    expected_pointer_version=7)
    rev2 = _revision(description="Edited.")
    record_catalog_profile_revision(db, rev2)
    assert set_current_catalog_profile(
        db, catalog_source=_SRC, revision_id=rev2.revision_id, expected_pointer_version=1) == 2


def test_identical_re_authoring_is_idempotent_and_never_churns_profiles(db):
    _seed_graph(db)
    rev = _revision()
    record_catalog_profile_revision(db, rev)
    set_current_catalog_profile(db, catalog_source=_SRC, revision_id=rev.revision_id,
                                expected_pointer_version=0)
    before = build_dataset_profile(
        db, source=_SRC, dataset_logical_ref=_TABLE_REF,
        catalog_profile_revision_id=current_catalog_profile_revision_id(db, _SRC))
    # Byte-identical re-authoring: same deterministic id, one stored row, pointer re-set to the
    # SAME revision — dataset_profile_hash cannot move (D12.10 + §6.2 no-churn).
    again = _revision(producer_ref="user:sam")
    record_catalog_profile_revision(db, again)
    set_current_catalog_profile(db, catalog_source=_SRC, revision_id=again.revision_id,
                                expected_pointer_version=1)
    assert db.execute("SELECT count(*) FROM catalog_profile_revision").fetchone()[0] == 1
    after = build_dataset_profile(
        db, source=_SRC, dataset_logical_ref=_TABLE_REF,
        catalog_profile_revision_id=current_catalog_profile_revision_id(db, _SRC))
    assert after.dataset_profile_hash == before.dataset_profile_hash
    # A REAL narrative edit re-keys the dataset profile (narrative is meaning-bearing).
    rev3 = _revision(description="Materially different narrative.")
    record_catalog_profile_revision(db, rev3)
    set_current_catalog_profile(db, catalog_source=_SRC, revision_id=rev3.revision_id,
                                expected_pointer_version=2)
    rekeyed = build_dataset_profile(
        db, source=_SRC, dataset_logical_ref=_TABLE_REF,
        catalog_profile_revision_id=current_catalog_profile_revision_id(db, _SRC))
    assert rekeyed.dataset_profile_hash != before.dataset_profile_hash


def test_corrupted_revision_raises_instead_of_serving(db):
    rev = _revision()
    record_catalog_profile_revision(db, rev)
    set_current_catalog_profile(db, catalog_source=_SRC, revision_id=rev.revision_id,
                                expected_pointer_version=0)
    db.execute("UPDATE catalog_profile_revision SET description = 'tampered' "
               "WHERE revision_id = %s", (rev.revision_id,))
    with pytest.raises(CatalogProfileConflict):
        current_catalog_profile(db, _SRC)


def test_migration_1047_check_pins_the_revision_id_shape(db):
    with pytest.raises(Exception):  # noqa: B017 — the CHECK constraint must refuse
        with db.transaction():
            db.execute(
                "INSERT INTO catalog_profile_revision (revision_id, catalog_source, producer, "
                "strength, lifecycle, producer_ref, content_hash) "
                "VALUES ('not-a-cpr-id', 'x', 'human', 'proposed', 'active', 'u', %s)",
                ("0" * 64,))


# ── derived read scope for the narrative ─────────────────────────────────────────────────────────


def test_catalog_visibility_is_derived_from_visible_columns(db):
    build_graph(db, "hidden", [CanonicalRow("hidden", "t", "c", "text", sensitivity="pii")])
    _seed_graph(db)
    assert catalog_visible(db, _SRC, roles=()) is True
    assert catalog_visible(db, "hidden", roles=()) is False       # every column hidden
    assert catalog_visible(db, "hidden", roles=("pii_reader",)) is True
    assert catalog_visible(db, "ghost", roles=("pii_reader",)) is False   # absent == hidden


# ── table-node projections ride the existing resolver + re-projection ────────────────────────────


def test_confirmed_profile_fields_project_onto_the_table_node(db):
    _seed_graph(db)
    _confirm_authority(db)
    row = db.execute(
        "SELECT authority_role, authority_role_decision_id FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
        (_SRC, _TABLE_OBJECT_REF)).fetchone()
    assert row is not None
    assert row[0] == "system_of_record"
    assert row[1] is not None                       # display ≠ authority link recorded


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _upload_actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def test_re_upload_cannot_blank_the_profile_projection(db):
    """build_graph DELETEs every node; the Task-0.6 unconditional table_display_reprojection must
    restore the new profile display columns from the surviving evidence/decisions."""
    _seal_config()
    rows = [CanonicalRow(_SRC, "orders", "amount", "numeric")]
    now = datetime(2026, 8, 1, tzinfo=UTC)
    assert ingest_upload(db, _SRC, rows, actor=_upload_actor(), now=now).status == "ingested"
    _confirm_authority(db)
    result = ingest_upload(db, _SRC, rows, actor=_upload_actor(),
                           now=now + timedelta(hours=1))
    assert result.status == "ingested"
    row = db.execute(
        "SELECT authority_role FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
        (_SRC, _TABLE_OBJECT_REF)).fetchone()
    assert row is not None and row[0] == "system_of_record"


# ── reconciliation report ────────────────────────────────────────────────────────────────────────


def test_reconcile_is_clean_after_a_normal_confirm(db):
    _seed_graph(db)
    _confirm_authority(db)
    rev = _revision()
    record_catalog_profile_revision(db, rev)
    set_current_catalog_profile(db, catalog_source=_SRC, revision_id=rev.revision_id,
                                expected_pointer_version=0)
    assert _reconcile_module().reconcile(db) == []


def test_reconcile_detects_a_tampered_graph_projection(db):
    _seed_graph(db)
    _confirm_authority(db)
    db.execute("UPDATE graph_node SET authority_role = 'derived' "
               "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
               (_SRC, _TABLE_OBJECT_REF))
    problems = _reconcile_module().reconcile(db)
    assert any("authority_role" in p and "graph displays 'derived'" in p for p in problems)


def test_reconcile_detects_an_orphaned_narrative_pointer(db):
    rev = _revision(catalog_source="ghostcat")
    record_catalog_profile_revision(db, rev)
    set_current_catalog_profile(db, catalog_source="ghostcat", revision_id=rev.revision_id,
                                expected_pointer_version=0)
    problems = _reconcile_module().reconcile(db)
    assert any("orphaned narrative" in p for p in problems)
