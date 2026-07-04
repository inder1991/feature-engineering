from __future__ import annotations

from tests.featuregen._helpers import mint_test_service_identity
from tests.featuregen.overlay._helpers import StubCatalog

from featuregen.contracts import Command
from featuregen.overlay import facts
from featuregen.overlay.catalog import register_catalog_adapter
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair, fact_key
from featuregen.overlay.projection import OverlayProjection, dependents_of
from featuregen.overlay.proposal_commands import propose_fact
from featuregen.overlay.store import append_overlay_event
from featuregen.projections.runner import run_projection


def _svc():
    return mint_test_service_identity(subject="service:p", role_claims=("overlay",), attestation="a")


def _join_ref(from_src, to_src):
    return ApprovedJoinRef(
        from_ref=CatalogObjectRef(from_src, "table", "core", "orders"),
        to_ref=CatalogObjectRef(to_src, "table", "core", "customers"),
        column_pairs=(ColumnPair("customer_id", "id"),),
        cardinality="N:1",
    )


def _join_value(from_src, to_src, to_table="customers"):
    return {
        "from_ref": {"catalog_source": from_src, "object_kind": "table", "schema": "core",
                     "table": "orders", "column": None},
        "to_ref": {"catalog_source": to_src, "object_kind": "table", "schema": "core",
                   "table": to_table, "column": None},
        "column_pairs": [{"from_col": "customer_id", "to_col": "id"}],
        "cardinality": "N:1",
    }


def _propose(db, ref, value):
    register_catalog_adapter(StubCatalog(catalog_source="pg:core"))
    return propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "proposed_value": value}, _svc(), "ik",
    ))


def test_cross_catalog_join_rejected(db):
    # F4: a join spanning two catalog_sources cannot be attested by SP-1.5's single adapter.
    r = _propose(db, _join_ref("pg:core", "pg:mart"), _join_value("pg:core", "pg:mart"))
    assert not r.accepted and "cross-catalog" in (r.denied_reason or "")


def test_join_ref_value_mismatch_rejected(db):
    # ref names core.customers as the to-side; the value names core.accounts -> reject the divergence.
    r = _propose(db, _join_ref("pg:core", "pg:core"), _join_value("pg:core", "pg:core", "accounts"))
    assert not r.accepted and "does not match ref" in (r.denied_reason or "")


def test_consistent_same_source_join_accepted(db):
    r = _propose(db, _join_ref("pg:core", "pg:core"), _join_value("pg:core", "pg:core"))
    assert r.accepted, r.denied_reason


def test_projection_source_qualifies_join_dependencies_per_referent(db):
    # Defense-in-depth: even a (bypass/historical) cross-catalog join must index each side under ITS
    # OWN source, so to-catalog drift + the F1 read guard can see the to-side.
    ref = _join_ref("pg:core", "pg:mart")
    fk = fact_key(ref, "approved_join")
    val = _join_value("pg:core", "pg:mart")
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED, actor=_svc(), expected_version=0,
        payload={
            "catalog_object_ref": val, "object_ref": "core.orders -> core.customers",
            "fact_type": "approved_join", "proposed_value": val,
            "proposal_fingerprint": "fp", "proposed_by": "service:p",
        },
    )
    run_projection(db, OverlayProjection())

    assert fk in dependents_of(db, "pg:core", "core.orders")      # from-side under pg:core
    assert fk in dependents_of(db, "pg:mart", "core.customers")   # to-side under pg:mart
    assert fk not in dependents_of(db, "pg:core", "core.customers")  # NOT laundered to from-source
