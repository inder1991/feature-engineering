"""One bridge, one row — the candidate ledger and the verified-edge projection.

``entity_bridge_candidate_evidence``'s primary key is the ORDERED five-tuple ``(entity_id, left
catalog + ref, right catalog + ref)``, so a bridge written with its endpoints swapped lands on a
SECOND row under the same ``fact_key``. The reviewer saw exactly that on a live database: one bridge
returning two links with contradictory evidence (``text``/``attested`` against ``uuid``/``declared``).

The identity fix stops the swapped orientation from being ACCEPTED onto a rejected fact, but a
first derivation is free to name a bridge either way round — so the write side has to store the
canonical orientation rather than whichever one it was handed. Two things travel together here:

* the endpoints, and
* the flags that are ABOUT an endpoint (``left_is_grain`` / ``right_is_grain``). Reordering the
  refs while leaving the flags behind would record the grain of the wrong side — a silent lie in
  the evidence a human confirms against, and an input to the link ranking.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload.test_bridge_orientation_identity import (
    bridge_candidate,
    swap,
)

from featuregen.overlay.identity import CatalogObjectRef, EntityBridgeRef, fact_key
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_projection import project_verified_bridge
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR

_T0 = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

_CORE = ("core", "public.customer_master.customer_id")
_CRM = ("crm", "public.customers.customer_id")


def test_a_bridge_derived_swapped_first_lands_on_the_canonical_row(db):
    """Insert order must not decide the stored orientation. Deriving the SWAPPED candidate first —
    with no prior row to conflict with — still writes the canonical endpoints, so a later derivation
    in either orientation updates that one row instead of forking a second."""
    cand = bridge_candidate(db)
    key = propose_bridge(db, swap(cand), actor=_ENRICH_ACTOR, now=_T0)
    row = db.execute(
        "SELECT left_catalog_source, left_object_ref, right_catalog_source, right_object_ref "
        "FROM entity_bridge_candidate_evidence WHERE fact_key = %s", (key,)).fetchone()
    assert row == (*_CORE, *_CRM)


def test_the_per_side_flags_travel_with_the_swapped_endpoints(db):
    """`left_is_grain` describes the LEFT endpoint. Canonicalizing the refs without moving the flags
    would credit `core`'s grain to `crm`."""
    cand = replace(bridge_candidate(db), left_is_grain=True, right_is_grain=False)
    key = propose_bridge(db, swap(cand), actor=_ENRICH_ACTOR, now=_T0)
    ev = db.execute("SELECT evidence_json FROM entity_bridge_candidate_evidence "
                    "WHERE fact_key = %s", (key,)).fetchone()[0]
    assert (ev["left_is_grain"], ev["right_is_grain"]) == (True, False)


def test_the_stored_fact_value_is_canonical_however_the_candidate_was_named(db):
    """The value is what every consumer reads and what the verified-edge projection is built from,
    so it carries the canonical orientation too — not merely the fingerprint derived from it."""
    cand = bridge_candidate(db)
    key = propose_bridge(db, swap(cand), actor=_ENRICH_ACTOR, now=_T0)
    proposed = [e for e in load_fact(db, key) if e.type == "OVERLAY_FACT_PROPOSED"][-1]
    value = proposed.payload["proposed_value"]
    assert value["left_ref"]["catalog_source"] == "core"
    assert value["right_ref"]["catalog_source"] == "crm"
    # and the ref stored beside it agrees — one orientation, not two
    assert proposed.payload["catalog_object_ref"]["left_ref"]["catalog_source"] == "core"


def test_both_orientations_of_one_candidate_reach_the_same_single_row(db):
    cand = bridge_candidate(db)
    key = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)
    assert propose_bridge(db, swap(cand), actor=_ENRICH_ACTOR, now=_T0) == key
    assert db.execute("SELECT count(*) FROM entity_bridge_candidate_evidence").fetchone()[0] == 1


def test_a_legacy_non_canonical_value_projects_a_canonical_edge(db):
    """Values stored BEFORE canonicalization keep whatever orientation they were proposed with. The
    projection re-derives the edge from that value, so it canonicalizes on the way out — otherwise
    `entity_bridge_edge` and the candidate ledger describe one bridge with two shapes, and the read
    model has to reconcile them."""
    ref = EntityBridgeRef(
        "customer",
        CatalogObjectRef("core", "column", "public", "customer_master", "customer_id"),
        CatalogObjectRef("crm", "column", "public", "customers", "customer_id"))
    key = fact_key(ref, "entity_bridge")
    # the fixture writes the value exactly as given — the legacy shape, endpoints reversed
    govern_bridge_fact(db, key, entity="customer", left_source="crm", left_ref=_CRM[1],
                       right_source="core", right_ref=_CORE[1], status="VERIFIED")
    assert project_verified_bridge(db, ref, now=_T0) == "projected"
    row = db.execute(
        "SELECT left_catalog_source, left_object_ref, right_catalog_source, right_object_ref "
        "FROM entity_bridge_edge WHERE fact_key = %s", (key,)).fetchone()
    assert row == (*_CORE, *_CRM)
