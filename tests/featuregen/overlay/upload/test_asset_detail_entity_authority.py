"""Program-audit G3 / F12 — ``effective_metadata``'s ``entity`` field must apply the E3 second gate.

``graph_node.entity`` holds the GOVERNED value when ``entity_status = 'VERIFIED'`` (the same gate
``verified_entity_of`` and the F2b semantic subsection apply), but C1's ``read_operational_value``
has no entity fact/decision wiring, so ``_authority_label`` rendered a bank-confirmed entity and a
just-withdrawn one as the identical ``authority='hint'``. The F0 metadata section of one payload must
agree with its own relationships.semantic subsection: VERIFIED ⟶ ``governed`` + fact provenance;
demoted/absent ⟶ ``hint``/``missing``.
"""
from __future__ import annotations

from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

ROLES = ("catalog_viewer",)


def _seed(db):
    build_graph(db, "bank", [CanonicalRow("bank", "trades", "notional", "numeric",
                                          entity="cust_file")])


def _entity_field(db):
    body = build_asset_detail(db, source="bank", object_ref="public.trades.notional", roles=ROLES,
                              include=["effective_metadata"])
    return body["effective_metadata"]["fields"]["entity"]


def test_verified_governed_entity_reads_governed_with_fact_provenance(db):
    _seed(db)
    db.execute(
        "UPDATE graph_node SET entity = 'customer', declared_entity = 'cust_file', "
        "entity_status = 'VERIFIED', entity_fact_key = 'e-fk', entity_fact_event_id = 'e-ev' "
        "WHERE catalog_source = 'bank' AND object_ref = 'public.trades.notional'")

    ent = _entity_field(db)
    assert ent["value"] == "customer"
    assert ent["authority"] == "governed", (
        "a bank-confirmed (entity_status='VERIFIED') entity renders authority='hint' — the F0 "
        "metadata section ignores the E3 second gate its own semantic subsection applies")
    assert ent["provenance"] == "e-ev"


def test_withdrawn_entity_falls_back_to_hint(db):
    _seed(db)
    db.execute(
        "UPDATE graph_node SET entity = 'customer', declared_entity = 'cust_file', "
        "entity_status = 'VERIFIED', entity_fact_key = 'e-fk', entity_fact_event_id = 'e-ev' "
        "WHERE catalog_source = 'bank' AND object_ref = 'public.trades.notional'")
    assert _entity_field(db)["authority"] == "governed"

    # withdraw_binding demotes: entity restored to the file value, provenance cleared.
    db.execute(
        "UPDATE graph_node SET entity = declared_entity, declared_entity = NULL, "
        "entity_status = NULL, entity_fact_key = NULL, entity_fact_event_id = NULL "
        "WHERE catalog_source = 'bank' AND object_ref = 'public.trades.notional'")

    ent = _entity_field(db)
    assert ent["value"] == "cust_file"
    assert ent["authority"] == "hint"     # revoked ⟹ back to an advisory hint, distinguishable
