from __future__ import annotations

from featuregen.contracts import IdentityEnvelope
from featuregen.overlay import facts
from featuregen.overlay.catalog import CatalogFact, FixtureCatalog
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.projection import OverlayProjection, dependents_of
from featuregen.overlay.store import append_overlay_event
from featuregen.projections.runner import rebuild_projection, run_projection


def _human():
    return IdentityEnvelope(
        subject="owner", actor_kind="human", authenticated=True,
        auth_method="oidc", role_claims=("data_owner",),
    )


def _propose_and_confirm(db, catalog_source: str) -> str:
    # Same object NAME (public.customers), different catalog_source — the collision case.
    ref = CatalogObjectRef(
        catalog_source=catalog_source, object_kind="table", schema="public", table="customers"
    )
    fk = fact_key(ref, "grain")
    draft = append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED, actor=_human(), expected_version=0,
        payload={
            "catalog_object_ref": {"catalog_source": catalog_source, "object_kind": "table",
                                   "schema": "public", "table": "customers"},
            "object_ref": "public.customers", "fact_type": "grain",
            "proposed_value": {"columns": ["id"], "is_unique": True},
            "proposal_fingerprint": f"fp-{catalog_source}", "proposed_by": "owner",
        },
    )
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED, actor=_human(), expected_version=1,
        payload={
            "value": {"columns": ["id"], "is_unique": True},
            "confirmers": [{"subject": "owner", "role": "data_owner"}],
            "expires_at": None, "confirms_event_id": draft.event_id,
        },
    )
    return fk


def test_two_catalogs_same_object_do_not_collide(db):
    core_fk = _propose_and_confirm(db, "pg:core")
    mart_fk = _propose_and_confirm(db, "pg:mart")
    run_projection(db, OverlayProjection())

    assert core_fk != mart_fk  # fact_key already source-qualified

    # Two DISTINCT source-qualified state rows for the same object name.
    rows = db.execute(
        "SELECT catalog_source FROM overlay_fact_state WHERE object_ref='public.customers' "
        "ORDER BY catalog_source"
    ).fetchall()
    assert [r[0] for r in rows] == ["pg:core", "pg:mart"]

    # The dependency index is SOURCE-SCOPED: drift in pg:core never returns the pg:mart fact.
    assert dependents_of(db, "pg:core", "public.customers") == [core_fk]
    assert dependents_of(db, "pg:mart", "public.customers") == [mart_fk]
    assert mart_fk not in dependents_of(db, "pg:core", "public.customers")


def test_replay_rebuilds_source_qualified_rows(db):
    # The read models are droppable + deterministically rebuildable from history (F7 replay).
    core_fk = _propose_and_confirm(db, "pg:core")
    _propose_and_confirm(db, "pg:mart")
    rebuild_projection(db, OverlayProjection())  # reset() + replay from 0

    assert db.execute(
        "SELECT catalog_source FROM overlay_fact_state WHERE fact_key = %s", (core_fk,)
    ).fetchone()[0] == "pg:core"
    assert db.execute(
        "SELECT count(*) FROM overlay_fact_state WHERE object_ref='public.customers'"
    ).fetchone()[0] == 2  # both catalogs survive replay, distinct


def test_adapter_fails_closed_on_foreign_source(db):
    # F5: an adapter must never return a same-named object's fact/owner from ANOTHER catalog_source.
    adapter = FixtureCatalog(catalog_source="pg:core")
    core_ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="column", schema="public", table="customers",
        column="signup_at",
    )
    mart_ref = CatalogObjectRef(
        catalog_source="pg:mart", object_kind="column", schema="public", table="customers",
        column="signup_at",
    )
    adapter.set_fact(core_ref, "availability_time", {"column": "signup_at"}, authoritative=True)
    adapter.set_owner(core_ref, "team-core")

    assert adapter.get_fact(core_ref, "availability_time") == CatalogFact(
        value={"column": "signup_at"}, authoritative=True
    )
    assert adapter.get_fact(mart_ref, "availability_time") is None  # foreign source -> closed
    assert adapter.owner_of(core_ref) == "team-core"
    assert adapter.owner_of(mart_ref) is None
