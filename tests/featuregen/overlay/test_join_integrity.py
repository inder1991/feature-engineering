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


def _same_owner_adapter():
    cat = StubCatalog(catalog_source="pg:core")
    cat.set_owner(_join_ref("pg:core", "pg:core").from_ref, "user:alice")
    cat.set_owner(_join_ref("pg:core", "pg:core").to_ref, "user:alice")  # same owner -> single path
    register_catalog_adapter(cat)
    return cat


def _propose_join(db, ref, value):
    from featuregen.overlay.proposal_commands import propose_fact
    return propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "proposed_value": value}, _svc(), "p",
    )).produced_event_ids[0]


def test_confirm_override_rejects_mismatched_join_value(db):
    # SP-1.5 re-review #1: a same-owner join confirmer must not be able to OVERRIDE with a value
    # describing a different (here cross-catalog) join than the ref they have authority over.
    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay.confirmation_commands import confirm_fact

    ref = _join_ref("pg:core", "pg:core")
    _same_owner_adapter()
    draft = _propose_join(db, ref, _join_value("pg:core", "pg:core"))

    bad = _join_value("pg:core", "pg:mart", to_table="customers")  # cross-catalog / other tables
    res = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "target_event_id": draft, "value": bad},
        mint_test_identity(subject="user:alice", role_claims=("data_owner",)), "c",
    ))
    assert not res.accepted
    assert "does not match ref" in (res.denied_reason or "")


def test_confirm_same_owner_join_without_override_still_verifies(db):
    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay.confirmation_commands import confirm_fact
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact

    ref = _join_ref("pg:core", "pg:core")
    _same_owner_adapter()
    draft = _propose_join(db, ref, _join_value("pg:core", "pg:core"))
    res = confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "target_event_id": draft},
        mint_test_identity(subject="user:alice", role_claims=("data_owner",)), "c",
    ))
    assert res.accepted, res.denied_reason
    assert fold_overlay_state(load_fact(db, fact_key(ref, "approved_join"))).status == "VERIFIED"
