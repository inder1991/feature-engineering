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

THREE INDEPENDENT CONCEPTS (owner, lifecycle correction). Link AVAILABILITY ("may the platform
consider this link?"), AUTOMATIC EXECUTION SAFETY ("has the direction/cardinality/scope been
validated?") and HUMAN REVIEW ("does someone endorse the semantic relationship?") are separate, and
human review controls NEITHER of the first two:

    DRAFT / PARTIALLY_CONFIRMED  -> available, unreviewed
    VERIFIED                     -> available, human-reviewed
    REJECTED / STALE / REVERIFY  -> unavailable

so availability is decided by an ALLOW-LIST over the governed lifecycle, and ranking puts measured
safety ahead of endorsement.
"""
from __future__ import annotations

import json

import pytest

from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact

from featuregen.overlay import store
from featuregen.overlay.upload.cross_catalog_links import LinkStatus, cross_catalog_links

_LEFT_TABLE = "public.bo_cib_customer"
_RIGHT_TABLE = "public.comp_financial_tran_repos_dly"


def _govern(db, key, entity, left_col, right_col, *, status="DRAFT"):
    """Drive `key`'s overlay_fact stream to `status` with the SAME events production appends.

    A candidate row in the ledger ALWAYS has a governed fact behind it — `bridge_propose` writes the
    ledger only after `propose_fact` is accepted — so a fixture whose ledger row has no stream is not
    a shape the platform can produce. Building the stream is what makes the DRAFT arm below a real
    DRAFT rather than an unreadable void that happens to be let through.
    """
    return govern_bridge_fact(
        db, key, entity=entity, left_source="cib", left_ref=f"{_LEFT_TABLE}.{left_col}",
        right_source="ftr", right_ref=f"{_RIGHT_TABLE}.{right_col}", status=status)


def _candidate(db, entity, left_col, right_col, *, left_grain=False, right_grain=False,
               basis="declared", fact_key=None, status="DRAFT"):
    key = fact_key or f"fk-{left_col}"
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
         ev["candidate_id"], key, json.dumps(ev)))
    if status is not None:
        _govern(db, key, entity, left_col, right_col, status=status)
    return key


def _verify(db, fact_key, entity, left_col, right_col, *, stream=True):
    """Confirm the bridge: drive the FACT to VERIFIED and write the projection row it produces.

    ``stream=False`` writes the projection ALONE — the shape `multisource_gold` and the planner
    fixtures create, where the edge row is the only record of the sanction.
    """
    if stream:
        _govern(db, fact_key, entity, left_col, right_col, status="VERIFIED")
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


def test_a_confirmed_weak_link_does_not_outrank_an_unreviewed_safer_one(db):
    """MEASURED SAFETY RANKS FIRST. `_W_CONFIRMED` used to be 100 against a grain side's 10 and an
    attested type match's 5, so ONE approval outweighed every derived signal combined: a human
    endorsing a type-only pairing (`branch_nm <-> sol_desc`, one of the eight junk candidates on the
    live catalog) sorted above a grain-backed, attested link nobody had got round to reviewing.

    Endorsement is an opinion about the SEMANTIC relationship; grain and an attested type match are
    things the platform measured about the DATA. An opinion may not overrule a measurement."""
    _candidate(db, "customer", "cust_num", "cif_id", left_grain=True, basis="attested",
               fact_key="fk-1")
    _candidate(db, "branch", "c", "d", fact_key="fk-2")
    _verify(db, "fk-2", "branch", "c", "d")
    by_entity = {l.entity_id: l for l in cross_catalog_links(db)}
    assert by_entity["customer"].strength > by_entity["branch"].strength
    assert cross_catalog_links(db)[0].entity_id == "customer"


def test_confirmation_still_breaks_a_tie_between_two_equally_safe_links(db):
    """Human review must not be INERT either — it just may not outrank measurement. Between two
    links the platform can say nothing to separate, the reviewed one is shown first."""
    _candidate(db, "customer", "a", "b", left_grain=True, basis="attested", fact_key="fk-1")
    _candidate(db, "account", "c", "d", left_grain=True, basis="attested", fact_key="fk-2")
    _verify(db, "fk-2", "account", "c", "d")
    by_entity = {l.entity_id: l for l in cross_catalog_links(db)}
    assert by_entity["account"].strength > by_entity["customer"].strength
    assert cross_catalog_links(db)[0].entity_id == "account"


def test_no_amount_of_endorsement_reaches_the_next_safety_band(db):
    """The tie-breaker is bounded BY CONSTRUCTION, not by luck: a confirmed link's whole endorsement
    bonus is smaller than the smallest safety increment, so it can never cross a band."""
    from featuregen.overlay.upload.cross_catalog_links import (
        _W_ATTESTED, _W_CONFIRMED, _W_GRAIN_SIDE)
    assert _W_CONFIRMED < min(_W_ATTESTED, _W_GRAIN_SIDE)


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


# ── a VERIFIED edge with no candidate row must not vanish ────────────────────────────────────────

def test_a_verified_edge_with_no_candidate_row_is_still_returned(db):
    """Some verified bridges are written straight to the edge table (the multisource gold fixture
    does exactly that). Reading only the candidate ledger would silently DROP them — turning a
    "show unconfirmed too" change into a regression that loses confirmed links."""
    _verify(db, "fk-orphan", "customer", "cust_num", "cif_id")
    links = cross_catalog_links(db)
    assert [l.status for l in links] == [LinkStatus.CONFIRMED]
    assert links[0].entity_id == "customer"


def test_the_union_does_not_double_count(db):
    _candidate(db, "customer", "cust_num", "cif_id", fact_key="fk-1")
    _verify(db, "fk-1", "customer", "cust_num", "cif_id")
    assert len(cross_catalog_links(db)) == 1


# ── strength reflects the catalog NOW, not the catalog at derivation time ────────────────────────

def test_grain_established_AFTER_derivation_still_counts(db):
    """On the live catalog every link scored 0 — including `cust_num <-> cif_id`, where cust_num IS
    its table's grain. The candidate was derived before Pass B established the grain fact, and the
    strength read `evidence_json.left_is_grain`, frozen at derivation time.

    A rank that can never improve as the catalog is enriched is not a rank. Read the graph."""
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name,"
               " data_type, is_grain) VALUES ('cib','public.bo_cib_customer.cust_num','column',"
               " 'bo_cib_customer','cust_num','text', true)")
    _candidate(db, "customer", "cust_num", "cif_id", left_grain=False)   # evidence says NOT grain
    link = cross_catalog_links(db)[0]
    assert link.left_is_grain is True, "the graph says grain; the stale evidence said otherwise"
    assert link.strength >= 10
    assert "key" in link.why


def test_a_column_that_is_genuinely_not_a_key_still_ranks_low(db):
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name,"
               " data_type) VALUES ('cib','public.bo_cib_customer.branch_nm','column',"
               " 'bo_cib_customer','branch_nm','text')")
    _candidate(db, "branch", "branch_nm", "sol_desc")
    assert cross_catalog_links(db)[0].strength == 0


# ── AVAILABILITY is an allow-list over the governed lifecycle ────────────────────────────────────
#
# `_blocked` was a DENY-LIST of ("REJECTED", "STALE") wrapped in a bare `except: return False`. Both
# halves were wrong in the same direction — towards serving the link:
#
#   * REVERIFY was in neither set, so an EXPIRED bridge stayed available (and, before the projection
#     demote, stayed available AS CONFIRMED). This is the arm that actually leaked.
#   * an unreadable fact stream returned "not blocked", i.e. AVAILABLE. Trading a blank list for
#     silently serving links whose governance state cannot be read is the wrong resolution.
#
# The tests below are the three arms plus their controls. Test strength: none of them can pass
# against the deny-list, and none of them can pass against a fail-open `except`.

def test_control_a_draft_link_is_available_and_nothing_here_blocks_it(db):
    """MUST-SURVIVE no-op control. An allow-list wired too tight — or a harness that governs the
    fixture into the wrong state — passes every suppression test below and fails this one."""
    _candidate(db, "customer", "cust_num", "cif_id", status="DRAFT")
    assert [l.entity_id for l in cross_catalog_links(db)] == ["customer"]


def test_a_draft_link_is_available_and_traversable_with_no_human_involved(db):
    """THE product decision, pinned so nobody re-gates it. DRAFT means "derived, nobody has looked at
    it" — available and usable. Human review endorses the SEMANTIC relationship; it is not permission
    to execute, and its absence withholds nothing.

    `active_bridges` is the planner's own active set, so this asserts traversability rather than mere
    listing: the link the planner can actually cross, with no confirmation anywhere in the stream."""
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.upload.bridge_projection import active_bridges

    key = _candidate(db, "customer", "cust_num", "cif_id", status="DRAFT")
    state = fold_overlay_state(store.load_fact(db, key))
    assert state.status == "DRAFT" and not state.confirmers

    (link,) = cross_catalog_links(db)
    assert link.status is LinkStatus.PROPOSED and link.usable
    assert [b.fact_key for b in active_bridges(db)] == [key]


def test_a_partially_confirmed_link_is_available(db):
    """One approval of a two-approval gate. Half-reviewed is still unreviewed, and unreviewed is
    still available — the four-eyes gate governs the ENDORSEMENT, not the link."""
    _candidate(db, "customer", "cust_num", "cif_id", status="PARTIALLY_CONFIRMED")
    assert [l.entity_id for l in cross_catalog_links(db)] == ["customer"]


def test_an_expired_bridge_is_unavailable(db):
    """THE arm that leaked. `OVERLAY_FACT_EXPIRED` folds to REVERIFY (overlay/state.py) — NOT to any
    status the old deny-list named — so an expired bridge went on being served as an available link.
    An expiry means the sanction has lapsed and the relationship must be re-established; until it is,
    the platform may not consider the link."""
    _candidate(db, "customer", "cust_num", "cif_id", status="REVERIFY")
    assert cross_catalog_links(db) == ()


def test_a_rejected_bridge_is_unavailable(db):
    """A human's NO still counts. The direction is "confirmation is not REQUIRED", never
    "disapproval is ignored" — a governance surface whose rejections did nothing is decorative."""
    _candidate(db, "customer", "cust_num", "cif_id", status="REJECTED")
    assert cross_catalog_links(db) == ()


def test_a_drift_staled_bridge_is_unavailable(db):
    """Drift changed an endpoint: the link may no longer hold and has to be re-derived."""
    _candidate(db, "customer", "cust_num", "cif_id", status="STALE")
    assert cross_catalog_links(db) == ()


def test_an_unreadable_fact_stream_is_unavailable_not_available(db, monkeypatch):
    """The fail-OPEN half. The old `except: return False` said "not blocked" — so a link whose
    governance state could not be read was served as available, and a REJECTED bridge behind a
    failing read became indistinguishable from a sound one.

    Fail closed instead. A blank list is a visible, diagnosable outage; silently serving ungoverned
    joins is neither."""
    _candidate(db, "customer", "cust_num", "cif_id", status="DRAFT")
    assert cross_catalog_links(db) != (), "the control: readable and available before the fault"

    def _boom(*_a, **_k):
        raise RuntimeError("fact stream unreadable")

    monkeypatch.setattr(store, "load_fact", _boom)
    assert cross_catalog_links(db) == ()


def test_a_link_with_no_fact_key_is_unavailable(db):
    """`entity_bridge_candidate_evidence.fact_key` is NULLable, so a ledger row can name no governed
    fact at all. Under an allow-list there is nothing to read and therefore nothing to allow — and
    `active_bridges` already dropped these, so the read model now agrees with its own planner rather
    than advertising a link the planner will never cross."""
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        " left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        " data_type_family, evidence_json, derivation_version) "
        "VALUES ('customer','cib','public.bo_cib_customer.cust_num','ftr',"
        " 'public.comp_financial_tran_repos_dly.cif_id','c-1',NULL,'text','{}','1.0.0')")
    assert cross_catalog_links(db) == ()


def test_a_ledger_row_with_no_governance_record_at_all_is_unavailable(db):
    """A ledger row whose fact has no events is not a shape production can reach — `bridge_propose`
    writes the ledger only after `propose_fact` is accepted. Absence of a governance record is not
    evidence of a benign one, so it fails closed like every other unreadable state."""
    _candidate(db, "customer", "cust_num", "cif_id", status=None)   # ledger row, no fact stream
    assert cross_catalog_links(db) == ()


def test_a_verified_projection_with_no_stream_is_still_available(db):
    """The one place absence is allowed, and only because the ROW ITSELF is the positive reading:
    `project_verified_bridge` writes an `entity_bridge_edge` row ONLY under a VERIFIED fold, and
    `demote_bridge_edges` removes it on every exit from VERIFIED. `multisource_gold` and the planner
    fixtures seed bridges this way, so treating a projected VERIFIED row as unreadable would blank
    them."""
    _verify(db, "fk-gold", "customer", "cust_num", "cif_id", stream=False)
    assert [l.status for l in cross_catalog_links(db)] == [LinkStatus.CONFIRMED]
