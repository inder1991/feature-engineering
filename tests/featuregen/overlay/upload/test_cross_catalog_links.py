"""Cross-catalog links are visible and usable BEFORE anyone confirms them.

Owner's direction (2026-07-29): "we should join irrespective of the confirmation, confirmation can
mark it human approved but it shouldnt stop from showing on ui and consuming it in feature
generations and data agents."

The built model gated hard the other way. `entity_bridge_edge` holds VERIFIED bridges only and is
the only thing `multisource_compile`, `grounding` and `invalidation` read, while
`entity_bridge_candidate_evidence` — where every derived candidate lands — had ZERO readers in the
entire codebase. Nine real candidates, including `cib.cust_num <-> ftr.cif_id`, sat in the database
invisible to every consumer and to the screen.

This read model returns BOTH, each carrying its own status, so a caller ranks rather than being
barred.

STRENGTH matters here in a way it does not for a field label. A wrong label misdescribes a column; a
wrong JOIN corrupts numbers. Of the nine real candidates only one is sound — the other eight pair a
branch code with a branch DESCRIPTION. So each link carries a strength derived from evidence already
stored: whether either side is a grain (a grain-to-anything link is a real key), and whether the type
match was ATTESTED or merely declared in a spreadsheet.
"""
from __future__ import annotations

import json

import pytest

from featuregen.overlay.upload.cross_catalog_links import LinkStatus, cross_catalog_links


def _candidate(db, entity, left_col, right_col, *, left_grain=False, right_grain=False,
               basis="declared", fact_key=None):
    ev = {"entity_id": entity, "type_basis": basis, "candidate_id": f"{left_col}-{right_col}",
          "left_is_grain": left_grain, "right_is_grain": right_grain,
          "data_type_family": "text", "derivation_version": "1.0.0"}
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        " left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        " data_type_family, evidence_json, derivation_version) "
        "VALUES (%s,'cib',%s,'ftr',%s,%s,%s,'text',%s,'1.0.0')",
        (entity, f"public.bo_cib_customer.{left_col}",
         f"public.comp_financial_tran_repos_dly.{right_col}",
         ev["candidate_id"], fact_key or f"fk-{left_col}", json.dumps(ev)))


def _verify(db, fact_key, entity, left_col, right_col):
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, left_object_ref, "
        " right_catalog_source, right_object_ref, status) "
        "VALUES (%s,%s,'cib',%s,'ftr',%s,'VERIFIED')",
        (fact_key, entity, f"public.bo_cib_customer.{left_col}",
         f"public.comp_financial_tran_repos_dly.{right_col}"))


# ── an UNCONFIRMED link is returned, not withheld ────────────────────────────────────────────────

def test_a_candidate_nobody_has_confirmed_is_returned(db):
    """THE point. Before this, a derived candidate was invisible to every reader in the codebase."""
    _candidate(db, "customer", "cust_num", "cif_id")
    links = cross_catalog_links(db)
    assert len(links) == 1
    assert links[0].status is LinkStatus.PROPOSED


def test_a_confirmed_link_reports_as_confirmed(db):
    """Confirmation ANNOTATES — it is how a human says "approved", not a gate on being returned."""
    _candidate(db, "customer", "cust_num", "cif_id", fact_key="fk-1")
    _verify(db, "fk-1", "customer", "cust_num", "cif_id")
    links = cross_catalog_links(db)
    assert len(links) == 1, "the same link must not be listed twice"
    assert links[0].status is LinkStatus.CONFIRMED


def test_both_kinds_come_back_together(db):
    _candidate(db, "customer", "cust_num", "cif_id", fact_key="fk-1")
    _verify(db, "fk-1", "customer", "cust_num", "cif_id")
    _candidate(db, "branch", "cust_prim_branch_nm", "sol_desc", fact_key="fk-2")
    assert {l.status for l in cross_catalog_links(db)} == {LinkStatus.CONFIRMED, LinkStatus.PROPOSED}


# ── strength: rank, never bar ────────────────────────────────────────────────────────────────────

def test_a_grain_backed_link_outranks_one_with_no_key_on_either_side(db):
    """`cust_num` is its table's grain; the branch pairs are neither side's key. Both are returned —
    a weak candidate is not hidden — but the caller can tell them apart."""
    _candidate(db, "customer", "cust_num", "cif_id", left_grain=True, fact_key="fk-1")
    _candidate(db, "branch", "cust_prim_branch_nm", "sol_desc", fact_key="fk-2")
    by_entity = {l.entity_id: l for l in cross_catalog_links(db)}
    assert by_entity["customer"].strength > by_entity["branch"].strength


def test_an_attested_type_match_outranks_a_merely_declared_one(db):
    """`declared` means someone's spreadsheet said the types match; `attested` means the platform
    read them. Both link; they are not equally believable."""
    _candidate(db, "customer", "a", "b", basis="attested", fact_key="fk-1")
    _candidate(db, "branch", "c", "d", basis="declared", fact_key="fk-2")
    by_entity = {l.entity_id: l for l in cross_catalog_links(db)}
    assert by_entity["customer"].strength > by_entity["branch"].strength


def test_a_confirmed_link_outranks_every_unconfirmed_one(db):
    """A human's approval is the strongest signal there is — it just is not a precondition."""
    _candidate(db, "customer", "cust_num", "cif_id", left_grain=True, basis="attested",
               fact_key="fk-1")
    _candidate(db, "branch", "c", "d", fact_key="fk-2")
    _verify(db, "fk-2", "branch", "c", "d")
    by_entity = {l.entity_id: l for l in cross_catalog_links(db)}
    assert by_entity["branch"].strength > by_entity["customer"].strength


def test_the_strongest_link_is_listed_first(db):
    _candidate(db, "branch", "c", "d", fact_key="fk-2")
    _candidate(db, "customer", "cust_num", "cif_id", left_grain=True, basis="attested",
               fact_key="fk-1")
    assert cross_catalog_links(db)[0].entity_id == "customer"


# ── scoping ──────────────────────────────────────────────────────────────────────────────────────

def test_links_can_be_narrowed_to_one_column(db):
    """The asset screen asks "what does THIS column link to?"."""
    _candidate(db, "customer", "cust_num", "cif_id", fact_key="fk-1")
    _candidate(db, "branch", "cust_prim_branch_nm", "sol_desc", fact_key="fk-2")
    links = cross_catalog_links(db, object_ref="public.bo_cib_customer.cust_num")
    assert [l.entity_id for l in links] == ["customer"]


def test_a_column_matches_from_either_side_of_the_link(db):
    """A link is symmetric — opening the FTR side must find the same link the CIB side does."""
    _candidate(db, "customer", "cust_num", "cif_id", fact_key="fk-1")
    links = cross_catalog_links(db, object_ref="public.comp_financial_tran_repos_dly.cif_id")
    assert [l.entity_id for l in links] == ["customer"]


def test_no_links_is_an_empty_list_not_an_error(db):
    assert cross_catalog_links(db) == ()
