"""One fact_key, one link — and the same link whichever order the rows were written in.

Before bridge identity was canonicalized, a bridge proposed with its endpoints swapped wrote a
SECOND row into ``entity_bridge_candidate_evidence`` under the same ``fact_key`` (the table's
primary key is the ORDERED five-tuple). The reviewer saw the result on a live database:
``cross_catalog_links`` returning TWO links for one bridge, with contradictory evidence —
``text``/``attested`` against ``uuid``/``declared`` — while the governance queue showed ONE,
collapsed by an ``ORDER BY``-less dict comprehension that silently picked whichever row the planner
happened to return first.

The write side no longer creates such a pair. These tests are about the rows already in the
database, and about the property that makes the reader trustworthy regardless: reading is a MERGE
per ``fact_key``, not a pick. An arbitrary collapse is worse than a duplicate, because it answers
confidently with one of two contradictory readings and shows no sign of having chosen.

The merge is conservative and order-independent:

* a grain is positive evidence about a COLUMN, so grain flags are OR-ed once the rows are read in
  the canonical orientation;
* ``attested`` is claimed only when EVERY row claims it — a contradicted claim that the platform
  read the physical types ranks the link DOWN, never up;
* a contradicted ``data_type_family`` resolves to the same value every time (and is logged), rather
  than to whichever row arrived first.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import permutations

from tests.featuregen.overlay.upload.test_bridge_orientation_identity import (
    bridge_candidate,
    reject,
)

from featuregen.overlay.identity import EntityBridgeRef
from featuregen.overlay.upload.bridge_governance import list_bridge_proposals
from featuregen.overlay.upload.bridge_projection import active_bridges
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.cross_catalog_links import _merge_ledger_rows, cross_catalog_links
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR

_T0 = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

_CORE = ("core", "public.customer_master.customer_id")
_CRM = ("crm", "public.customers.customer_id")


def _legacy_row(db, *, key, left, right, family, basis, l_grain, r_grain):
    """A ledger row written the way the OLD code could write one: the ORDERED five-tuple as its
    primary key, so a swapped duplicate lands beside its twin under the same fact_key."""
    l_src, l_ref = left
    r_src, r_ref = right
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        "  left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        "  data_type_family, evidence_json, derivation_version, updated_at) "
        "VALUES ('customer',%s,%s,%s,%s,%s,%s,%s,%s,'legacy',now())",
        (l_src, l_ref, r_src, r_ref, f"c-{l_src}-{r_src}", key, family,
         json.dumps({"type_basis": basis, "left_is_grain": l_grain, "right_is_grain": r_grain})))


def _seed_contradictory_pair(db, *, forward_first: bool) -> str:
    """The shape the reviewer observed live: ONE governed bridge, TWO ledger rows disagreeing on
    family and on type basis. Seeded in either insert order."""
    cand = bridge_candidate(db)
    key = propose_bridge(db, cand, actor=_ENRICH_ACTOR, now=_T0)
    db.execute("DELETE FROM entity_bridge_candidate_evidence WHERE fact_key = %s", (key,))
    forward = dict(key=key, left=_CORE, right=_CRM, family="text", basis="attested",
                   l_grain=True, r_grain=False)
    backward = dict(key=key, left=_CRM, right=_CORE, family="uuid", basis="declared",
                    l_grain=False, r_grain=True)
    for row in ((forward, backward) if forward_first else (backward, forward)):
        _legacy_row(db, **row)
    assert db.execute("SELECT count(*) FROM entity_bridge_candidate_evidence").fetchone()[0] == 2
    return key


def test_cross_catalog_links_returns_one_link_per_fact_key(db):
    key = _seed_contradictory_pair(db, forward_first=True)
    assert [link.fact_key for link in cross_catalog_links(db)] == [key]


def test_the_collapsed_link_does_not_depend_on_insert_order(db):
    """The decisive property. An arbitrary collapse answers with one of two contradictory readings
    and looks identical to a considered one — so the answer must be the SAME whichever row was
    written first."""
    key_a = _seed_contradictory_pair(db, forward_first=True)
    forward = cross_catalog_links(db)
    db.execute("DELETE FROM entity_bridge_candidate_evidence")
    key_b = _seed_contradictory_pair(db, forward_first=False)
    backward = cross_catalog_links(db)
    assert key_a == key_b
    assert len(forward) == 1
    assert forward == backward


def test_the_collapsed_link_is_reported_in_canonical_orientation(db):
    _seed_contradictory_pair(db, forward_first=False)
    link = cross_catalog_links(db)[0]
    assert (link.left_catalog_source, link.left_object_ref) == _CORE
    assert (link.right_catalog_source, link.right_object_ref) == _CRM


def test_contradictory_evidence_is_not_resolved_in_the_optimistic_direction(db):
    """`attested` means the platform read the physical types; `declared` means a spreadsheet said
    so. One row contradicting the other is not a licence to claim the stronger reading — the link is
    ranked down rather than up, in both insert orders."""
    for forward_first in (True, False):
        db.execute("DELETE FROM entity_bridge_candidate_evidence")
        _seed_contradictory_pair(db, forward_first=forward_first)
        link = cross_catalog_links(db)[0]
        assert link.type_basis == "declared"


def test_grain_evidence_is_read_in_the_canonical_orientation_and_kept(db):
    """Each row says a different side is the grain — but they name the sides in OPPOSITE orders, so
    read canonically both are saying `core.customer_master.customer_id` is its table's key."""
    _seed_contradictory_pair(db, forward_first=True)
    link = cross_catalog_links(db)[0]
    assert link.left_is_grain is True


def test_the_governance_queue_agrees_with_the_read_model(db):
    """The queue used to collapse the ledger with an ORDER BY-less dict comprehension of its own,
    which is how one bridge could be shown to a confirmer with evidence the ranked read model did
    not agree with. Both now merge by the same rule."""
    for forward_first in (True, False):
        db.execute("DELETE FROM entity_bridge_candidate_evidence")
        _seed_contradictory_pair(db, forward_first=forward_first)
        rows = list_bridge_proposals(db)
        link = cross_catalog_links(db)[0]
        assert len(rows) == 1
        assert rows[0]["type_basis"] == link.type_basis == "declared"
        assert rows[0]["left_is_grain"] == link.left_is_grain
        assert rows[0]["data_type_family"] == link.data_type_family


def test_the_planner_active_set_holds_one_entry_per_bridge(db):
    """`active_bridges` is what the planner traverses. A duplicated link there is a duplicated join
    path for one bridge."""
    key = _seed_contradictory_pair(db, forward_first=True)
    assert [b.fact_key for b in active_bridges(db)] == [key]


def test_a_rejected_bridge_stays_absent_even_with_duplicate_ledger_rows(db):
    """Availability is decided by the governed fold, once per fact_key — duplicate rows must not
    give a rejected bridge a second chance to be returned."""
    key = _seed_contradictory_pair(db, forward_first=True)
    cand = bridge_candidate(db)
    reject(db, EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref), key)
    assert cross_catalog_links(db) == ()


def test_the_merge_is_order_independent_by_construction(db):
    """The query is ordered, so the tests above would still pass if the reader PICKED a row rather
    than merging — determinism would be coming from the `ORDER BY` alone. This pins the property at
    the merge itself: every permutation of the same rows yields the identical record, so no future
    change to the query (or to the planner) can reintroduce an order-dependent answer."""
    rows = [
        ("customer", *_CORE, *_CRM, "text", "attested", True, False),
        ("customer", *_CORE, *_CRM, "uuid", "declared", False, True),
        ("customer", *_CORE, *_CRM, "text", "", False, False),
    ]
    merged = {_merge_ledger_rows("k", list(order)) for order in permutations(rows)}
    assert len(merged) == 1
    only = merged.pop()
    assert (only.left_is_grain, only.right_is_grain) == (True, True)
    assert only.type_basis == ""            # the weakest reading present, not the first
    assert only.data_type_family == "text"


def test_two_genuinely_different_bridges_are_not_merged(db):
    """The merge is keyed by fact_key, so it can only ever collapse rows that ARE one bridge."""
    key = _seed_contradictory_pair(db, forward_first=True)
    other = "f" * 64
    db.execute("INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, "
               "  left_object_ref, right_catalog_source, right_object_ref, status) "
               "VALUES (%s,'account','core','public.accounts.account_id','crm',"
               "        'public.acct.acct_id','VERIFIED')", (other,))
    assert {link.fact_key for link in cross_catalog_links(db)} == {key, other}
