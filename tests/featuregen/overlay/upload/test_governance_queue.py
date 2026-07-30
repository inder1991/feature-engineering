"""The CROSS-CATALOG governance queue read model (`governance_queue`) — Task 4.

Four properties separate a real implementation from a plausible-looking one, and each has a test
here that a shortcut fails.

* **THE MERGE TAKES NO SOURCE.** Only `list_bridge_proposals` accepts `source=None`; the join and
  table-fact listings REQUIRE a source, so they must be iterated per catalog. A single call to each
  cannot produce this list. `test_merges_all_three_kinds_across_all_catalogs_with_no_source_argument`
  asserts all three kinds AND every catalog are present from one no-source call.

* **THE MERGE MUST NOT WIDEN VISIBILITY.** The join / table-fact listings have NO role scoping of
  their own — their only scope is the source argument — so the queue's scope for them IS the visible
  catalog set. A caller who cannot see a catalog gets none of its items, asserted as ABSENCE and
  asserted against the SAME seeding under a granting role so the absence is provably the scope
  predicate rather than an empty fixture.

* **AN EMPTY CATEGORY IS `not tracked yet`, A POPULATED ONE IS `0`.** This pair is the whole point
  of the usage work: every plan-storage table is empty on the live database, so a naive
  implementation returns 0 everywhere and reads as "nothing depends on this" when the truth is
  "nothing has been recorded". `test_an_empty_usage_store_is_not_tracked_yet_never_zero` and
  `test_a_populated_store_that_does_not_name_this_bridge_is_a_real_zero` seed the SAME store either
  side of that line and demand different answers — in the second case, two DIFFERENT answers for two
  categories in ONE response, which only a per-category rule can produce.

* **UNREACHABLE IS NOT EMPTY.** An empty queue is `items == [] and complete`. A broken one is
  `items == [] and not complete` with the failure named. A payload without that flag could not tell
  an operator which they are looking at.

Seeding drives the REAL propose paths (`propose_bridge`, `propose_fact`) on the rolled-back `db`
connection, plus `build_graph` so the catalogs are genuinely visible to `list_visible_catalogs`
(there is no catalog-level visibility in this system — a catalog exists for a caller iff the caller
can see a column in it, so a governance proposal on a catalog with no visible column is correctly
absent). The usage recorders write the REAL migration-1019 / migration-1011 tables, so the counts are
exercised against the actual columns, FKs and CHECK constraints rather than a stand-in.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.conftest import _confirm_grain
from tests.featuregen.overlay.upload.passc.conftest import SERVICE_ACTOR
from tests.featuregen.overlay.upload.test_bridge_candidates import _load
from tests.featuregen.overlay.upload.test_join_governance import (
    _confirm_once,
    _seed_join_with_evidence,
)

from featuregen.contracts.envelopes import Command
from featuregen.overlay.commands import propose_fact
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.invalidation import bridge_fact_marker
from featuregen.overlay.upload.governance_queue import (
    FORBIDDEN_PHRASES,
    KIND_ORDER,
    NOT_TRACKED_DISPLAY,
    bridge_usage,
    governance_queue,
)
from featuregen.overlay.upload.governed_grain import GovernedGrain, read_governed_grain
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter, table_ref

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


# ── Seeding ──────────────────────────────────────────────────────────────────────────────────────

def _two_catalogs(db, *, right_sensitivity: str | None = None) -> None:
    """`core` + `crm`, bridgeable on `customer_id`. `right_sensitivity` tags EVERY `crm` column, so
    an unprivileged caller can see no column in `crm` — the existence-oracle fixture."""
    _load(db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True),
         "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text"), "categorical"),
    ])
    _load(db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True,
                      sensitivity=right_sensitivity), "customer_id"),
    ])


def _seed_bridge(db, *, roles: tuple[str, ...] = ()) -> str:
    """PROPOSE the core<->crm bridge via the real derive + propose path. ``roles`` is the DERIVER's
    scope, not the reader's: `derive_bridge_candidates` is itself read-scoped, so a pii endpoint is
    invisible to it and no candidate exists to propose. A privileged derive is what an ingest running
    with the data-sensitivity role does, and it is the only way to get a bridge that a LATER
    unprivileged read must be proven to hide."""
    cand = derive_bridge_candidates(db, roles=roles)[0]
    return propose_bridge(db, cand, actor=SERVICE_ACTOR, now=_NOW)


def _seed_grain(db, source: str, table: str = "customer_master") -> str:
    """A DRAFT grain proposal exactly as Pass B makes one — the real `propose_fact` from the service
    enrichment actor, which opens the platform-admin gate task the listing reads."""
    ref = table_ref(source, table)
    value = {"columns": ["customer_id"], "is_unique": True}
    res = propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return fact_key(ref, "grain")


def _verify_grain(db, source: str, table: str, columns: list[str], *, human) -> str:
    """A COMPLETE, VERIFIED, unique grain through the REAL four-eyes flow — the service actor
    proposes, a platform-admin confirms, the projection drains. This is the ONLY thing that makes a
    grain governed: `is_grain` on a `graph_node` row is the uploader's claim about their own file,
    which `governed_grain` forbids as evidence in as many words."""
    ref = table_ref(source, table)
    value = {"columns": columns, "is_unique": True}
    res = propose_fact(db, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    _confirm_grain(db, source, table, columns, actor=human)
    return fact_key(ref, "grain")


@dataclass
class Seeded:
    """The seeded connection plus the fact keys, so a test can assert on identity rather than order."""
    conn: object
    bridge: str = ""
    join: str = ""
    grain: str = ""


@pytest.fixture
def queue_db(db):
    ensure_upload_catalog_adapter()
    return db


@pytest.fixture
def all_three_kinds(queue_db):
    """One bridge (core <-> crm), one discovered join (catalog `src`) and one grain proposal
    (`core`) — the three listings the queue merges, spread over three catalogs."""
    _two_catalogs(queue_db)
    bridge = _seed_bridge(queue_db)
    grain = _seed_grain(queue_db, "core")
    _load(queue_db, "src", [
        (CanonicalRow("src", "transactions", "cif_id", "text"), "customer_id"),
        (CanonicalRow("src", "customers", "cif_id", "text", is_grain=True), "customer_id"),
    ])
    _, join = _seed_join_with_evidence(queue_db)
    return Seeded(conn=queue_db, bridge=bridge, join=join, grain=grain)


def _kinds(queue) -> set[str]:
    return {i.kind for i in queue.items}


def _keys(queue) -> set[str]:
    return {i.fact_key for i in queue.items}


def _bridge(queue):
    return next(i for i in queue.items if i.kind == "entity_bridge")


def _usage(item, category: str):
    return next(u for u in item.already_depended_on_by if u.category == category)


# ── The merge: all three kinds, all catalogs, no source argument ──────────────────────────────────

def test_merges_all_three_kinds_across_all_catalogs_with_no_source_argument(all_three_kinds):
    """THE POINT OF THE SURFACE. One call, no source: an entity bridge (the only source=None-capable
    listing), a discovered join and a table fact (both source-REQUIRED, so both had to be iterated
    per catalog) all land in one list."""
    queue = governance_queue(all_three_kinds.conn, roles=())
    assert _kinds(queue) == {"entity_bridge", "approved_join", "grain"}
    assert {all_three_kinds.bridge, all_three_kinds.join, all_three_kinds.grain} <= _keys(queue)
    # ...and the catalog set is the /catalogs one, not an enumeration of `overlay_proposal` (which
    # structurally never sees a bridge).
    assert {"core", "crm", "src"} <= set(queue.catalogs)
    assert queue.complete and queue.unreadable == ()


def test_bridge_items_carry_both_catalogs_and_join_items_carry_their_own(all_three_kinds):
    queue = governance_queue(all_three_kinds.conn, roles=())
    assert set(_bridge(queue).catalogs) == {"core", "crm"}
    join = next(i for i in queue.items if i.kind == "approved_join")
    assert join.catalogs == ("src",)


def test_items_are_grouped_by_kind_in_a_stable_order(all_three_kinds):
    order = [i.kind for i in governance_queue(all_three_kinds.conn, roles=()).items]
    assert order == sorted(order, key=KIND_ORDER.index)


def test_counts_are_per_catalog_and_named_as_scope_relative(all_three_kinds):
    """Two operators legitimately see different numbers for the same catalog, so nothing may be
    presented as a catalog TOTAL. The payload says so and the field name says so."""
    queue = governance_queue(all_three_kinds.conn, roles=())
    per_catalog = dict(queue.items_visible_to_you_by_catalog)
    assert per_catalog["core"] >= 2 and per_catalog["src"] >= 1
    assert queue.counts_are_scope_relative is True
    assert not any("total" in name for name, _ in queue.items_visible_to_you_by_catalog)


def test_an_empty_database_is_an_empty_but_COMPLETE_queue(queue_db):
    queue = governance_queue(queue_db, roles=())
    assert queue.items == () and queue.catalogs == ()
    assert queue.complete is True and queue.unreadable == ()


# ── Read-scoping: the merge must not widen visibility ─────────────────────────────────────────────

def test_a_caller_who_cannot_see_a_catalog_gets_none_of_its_items(queue_db):
    """THE EXISTENCE ORACLE, at the merge. `crm` is pii top to bottom, so an unprivileged caller can
    see no column in it — `GET /catalogs` withholds it, and the queue must withhold everything that
    names it: the bridge (whose endpoint is a hidden column) and the catalog's own grain proposal.

    The join / table-fact listings have NO role scoping of their own, so this can only hold if the
    queue never passes them a catalog outside the visible set. A merge that iterated every catalog
    would hand `crm`'s grain proposal to a caller who cannot see the table it is about."""
    _two_catalogs(queue_db, right_sensitivity="pii")
    bridge = _seed_bridge(queue_db, roles=("pii_reader",))
    hidden_grain = _seed_grain(queue_db, "crm", "customers")
    open_grain = _seed_grain(queue_db, "core")

    blind = governance_queue(queue_db, roles=())
    assert "crm" not in blind.catalogs
    assert hidden_grain not in _keys(blind)
    assert bridge not in _keys(blind)              # a bridge naming a hidden catalog is dropped
    assert open_grain in _keys(blind)              # ...but the visible catalog's own item stays
    assert "crm" not in repr(blind)                # no name, no count, nowhere in the payload

    # The SAME seeding under the granting role DOES return all three — so the absences above are the
    # scope predicate at work, not an empty fixture.
    privileged = governance_queue(queue_db, roles=("pii_reader",))
    assert "crm" in privileged.catalogs
    assert {bridge, hidden_grain, open_grain} <= _keys(privileged)


def test_per_catalog_counts_differ_between_two_operators_on_one_database(queue_db):
    """Scope-relative counts, demonstrated: the same catalog, the same instant, two answers."""
    _two_catalogs(queue_db, right_sensitivity="pii")
    _seed_bridge(queue_db, roles=("pii_reader",))
    _seed_grain(queue_db, "crm", "customers")
    blind = dict(governance_queue(queue_db, roles=()).items_visible_to_you_by_catalog)
    privileged = dict(
        governance_queue(queue_db, roles=("pii_reader",)).items_visible_to_you_by_catalog)
    assert "crm" not in blind
    assert privileged["crm"] >= 1


# ── The vocabulary ───────────────────────────────────────────────────────────────────────────────

def test_no_forbidden_phrase_appears_anywhere_in_the_queue(all_three_kinds):
    """The vocabulary is a product decision: review is accountability, availability is automatic.
    Every forbidden phrase asserts the opposite — that a confirmation is permission to execute."""
    rendered = repr(governance_queue(all_three_kinds.conn, roles=())).lower()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase.lower() not in rendered, phrase
    assert "block" not in rendered          # no `blocks` / `blocked` / `blocking`, in any field
    assert "enable" not in rendered


def test_an_unreviewed_relationship_is_labelled_available_for_use(all_three_kinds):
    """A bare "unreviewed" reads as "blocked" and would be false — a PROPOSED bridge is consumed
    immediately. The label carries the availability truth itself."""
    bridge = _bridge(governance_queue(all_three_kinds.conn, roles=()))
    assert bridge.state == "Unreviewed — available for use"
    assert bridge.state_code == "unreviewed_available"


def test_production_eligibility_is_a_SEPARATE_axis_from_review(all_three_kinds, human_actor):
    """Human review does not decide production eligibility; automatic validation of the directional
    realization does. The bridge is UNREVIEWED, and its cardinality IS resolved because `crm`'s
    endpoint column is the WHOLE, VERIFIED, unique grain of `crm.customers` — the same evidence,
    read through the same `governed_grain` reader, that makes `declarations._segment_cardinality`
    answer `bridge_far_grain` instead of `bridge_unattested`. The two axes must disagree, which is
    only expressible if they are two fields."""
    conn = all_three_kinds.conn
    _verify_grain(conn, "crm", "customers", ["customer_id"], human=human_actor)

    bridge = _bridge(governance_queue(conn, roles=()))
    assert bridge.state_code == "unreviewed_available"
    assert bridge.production_eligibility == "Automatically validated for production"
    assert bridge.production_eligibility_code == "grain_resolved"


def test_an_uploaders_own_is_grain_flag_is_never_production_validation(all_three_kinds):
    """THE OVER-CLAIM THIS AXIS EXISTED TO MAKE. Both endpoint columns are declared `is_grain` by
    their own upload and `core`'s grain fact is merely DRAFT — nobody has confirmed anything. The
    screen used to answer "Automatically validated for production" off those flags while the
    compiler resolved the identical crossing as `bridge_unattested` and refused to run it.

    `governed_grain`: *"`is_grain = true` alone is a claim an uploader made about their own file.
    That is why nothing here ever GRANTS grain from the flags."*"""
    conn = all_three_kinds.conn
    flags = conn.execute(
        "SELECT count(*) FROM graph_node WHERE kind = 'column' AND is_grain "
        "  AND (catalog_source, object_ref) IN "
        "      (('core', 'public.customer_master.customer_id'), "
        "       ('crm', 'public.customers.customer_id'))").fetchone()[0]
    assert flags == 2, "both endpoints carry the file-declared flag — the defect's whole input"

    bridge = _bridge(governance_queue(conn, roles=()))
    assert bridge.production_eligibility == "Cardinality unresolved — sandbox only"
    assert bridge.production_eligibility_code == "cardinality_unresolved"


def test_one_member_of_a_COMPOSITE_grain_resolves_nothing(queue_db, human_actor):
    """Set EQUALITY, exactly as `_segment_cardinality` has it (`grain == (column,)`). The bridge
    lands on `customer_id`, but `core.customer_master`'s VERIFIED grain is `(customer_id, segment)`:
    joining on `customer_id` alone lands on every `segment` for that customer, so the hop is as
    unbounded as one onto no key at all. A `column in grain` membership test would call this
    validated."""
    _load(queue_db, "core", [
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True),
         "customer_id"),
        (CanonicalRow("core", "customer_master", "segment", "text", is_grain=True), "categorical"),
    ])
    _load(queue_db, "crm", [
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _seed_bridge(queue_db)
    _verify_grain(queue_db, "core", "customer_master", ["customer_id", "segment"],
                  human=human_actor)
    # the composite grain really IS attested — this is complete-equality failing, not a refusal
    grain = read_governed_grain(queue_db, "core", "customer_master", now=_NOW)
    assert isinstance(grain, GovernedGrain) and grain.columns == ("customer_id", "segment")

    bridge = _bridge(governance_queue(queue_db, roles=(), now=_NOW))
    assert bridge.production_eligibility_code == "cardinality_unresolved"


def test_a_grain_governed_on_one_side_is_never_credited_to_the_other_endpoint(queue_db,
                                                                              human_actor):
    """DIRECTION. `_segment_cardinality` consults ONLY the table the hop lands on, because *"a grain
    confirmed on the near side is evidence for the opposite direction"*. Here `core.customer_master`
    has a complete VERIFIED grain on `customer_id` — but the bridge does not land on it: `core`'s
    endpoint is `cust_ref`, and the column actually named `customer_id` is `crm`'s endpoint, whose
    own table has no governed grain at all.

    So the only evidence in the database is grain evidence about the NEAR side of every crossing
    this bridge can realize, and the honest answer is sandbox-only. An implementation that pooled
    both tables' grain columns and matched either endpoint by NAME would answer validated — and so
    does the file-flag implementation this replaced (`crm.customers.customer_id` is flagged)."""
    _load(queue_db, "core", [
        # the governed grain of this table — and deliberately NOT a bridge endpoint (no entity)
        (CanonicalRow("core", "customer_master", "customer_id", "integer", is_grain=True),
         "categorical"),
        # the bridge endpoint on this side — not the grain, and not flagged as one
        (CanonicalRow("core", "customer_master", "cust_ref", "integer"), "customer_id"),
    ])
    _load(queue_db, "crm", [
        # the bridge endpoint on the other side, carrying the uploader's own is_grain claim
        (CanonicalRow("crm", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ])
    _seed_bridge(queue_db)
    _verify_grain(queue_db, "core", "customer_master", ["customer_id"], human=human_actor)

    bridge = _bridge(governance_queue(queue_db, roles=(), now=_NOW))
    assert bridge.detail["left"]["column"] == "cust_ref"          # the crossing really is off-grain
    assert bridge.detail["right"]["column"] == "customer_id"
    assert bridge.production_eligibility == "Cardinality unresolved — sandbox only"
    assert bridge.production_eligibility_code == "cardinality_unresolved"


def test_no_derivation_evidence_at_all_is_not_observed_never_a_verdict(all_three_kinds):
    """The W4 case: `bridge_propose`'s early return left no ledger row, so there is no derivation
    record to reason from. Absence of evidence is not evidence of unresolved cardinality, so the
    axis withholds a verdict entirely rather than reporting sandbox-only."""
    conn = all_three_kinds.conn
    conn.execute("DELETE FROM entity_bridge_candidate_evidence")

    bridge = _bridge(governance_queue(conn, roles=()))
    assert bridge.detail["evidence_present"] is False
    assert bridge.production_eligibility is None
    assert bridge.production_eligibility_code == "not_observed"


def test_a_table_fact_has_no_production_eligibility_rather_than_a_guess(all_three_kinds):
    """A table's grain is not a crossing — there is no directional realization to validate — so the
    axis says nothing instead of inventing a verdict."""
    grain = next(i for i in governance_queue(all_three_kinds.conn, roles=()).items
                 if i.kind == "grain")
    assert grain.production_eligibility is None
    assert grain.production_eligibility_code == "not_applicable"


# ── available_actions: four-eyes, PROJECTED from the write-side rule ──────────────────────────────

def test_an_admin_who_already_endorsed_a_join_is_not_offered_confirm_again(queue_db):
    """NEVER ADVERTISE AN ACTION THE WRITE SIDE DENIES — the invariant this surface exists to hold.

    A dual discovered join needs TWO DISTINCT platform-admins:
    `join_confirmation._confirm_approved_join` denies a repeat subject ("this owner already
    confirmed; awaiting the other owner") and the route renders that denial as a 409 "You already
    approved this — a different admin must confirm." So the admin who has already endorsed must be
    offered `reject` only, while any OTHER admin is still offered both.

    The defect was silent because the key was wrong, not the logic: `_approvals_from_stream` builds
    each entry as `{"subject": payload["by_owner"], …}` — `by_owner` is the EVENT payload's field
    and the approvals VIEW renames it — so reading `by_owner` off the view yielded a set of empty
    strings, the membership test never matched, and every caller was offered `confirm`."""
    _load(queue_db, "src", [
        (CanonicalRow("src", "transactions", "cif_id", "text"), "customer_id"),
        (CanonicalRow("src", "customers", "cif_id", "text", is_grain=True), "customer_id"),
    ])
    ref, key = _seed_join_with_evidence(queue_db)
    admin1 = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))
    admin2 = mint_test_identity(subject="user:admin2", role_claims=("platform-admin",))
    _confirm_once(queue_db, ref, key, admin1, "looks right")

    def _item(actor):
        queue = governance_queue(queue_db, roles=(), actor=actor)
        return next(i for i in queue.items if i.kind == "approved_join" and i.fact_key == key)

    # the endorsement really is recorded, and under the key the view publishes
    assert [a["subject"] for a in _item(admin1).detail["approvals"]] == ["user:admin1"]
    assert _item(admin1).state_code == "partially_endorsed_available"

    assert _item(admin1).available_actions == ("reject",)          # the write side would 409
    assert _item(admin2).available_actions == ("confirm", "reject")  # the second, DISTINCT admin
    assert _item(None).available_actions == ("confirm", "reject")    # unscoped internal read


def test_a_join_nobody_has_endorsed_offers_both_actions_to_any_admin(all_three_kinds):
    """The control for the test above: with an empty `approvals` list the projection must not
    withhold `confirm` from anyone, or a caller who has done nothing would be told to reject."""
    admin = mint_test_identity(subject="user:admin1", role_claims=("platform-admin",))
    queue = governance_queue(all_three_kinds.conn, roles=(), actor=admin)
    join = next(i for i in queue.items if i.kind == "approved_join")
    assert join.detail["approvals"] == []
    assert join.available_actions == ("confirm", "reject")


# ── "Already depended on by": measurability is decided PER CATEGORY ───────────────────────────────

_MEASURABLE = ("planned_candidates", "selected_plans", "published_features")
_BY_CONSTRUCTION = ("generated_artifacts", "data_agent_analyses")


def test_an_empty_usage_store_is_not_tracked_yet_never_zero(all_three_kinds):
    """HALF ONE OF THE DECISIVE PAIR. Every plan-storage table is empty, so a naive implementation
    answers 0 for all five categories and the screen reads "nothing depends on this bridge" — when
    the truth is "nothing has been recorded". No category may show a number here."""
    bridge = _bridge(governance_queue(all_three_kinds.conn, roles=()))
    assert {u.category for u in bridge.already_depended_on_by} == set(
        _MEASURABLE + _BY_CONSTRUCTION)
    for u in bridge.already_depended_on_by:
        assert u.state == "not_tracked_yet", (u.category, u.state)
        assert u.count is None, u.category
        assert u.display == NOT_TRACKED_DISPLAY
        assert "0" not in u.display


def test_the_two_by_construction_categories_say_WHY_and_never_become_countable(all_three_kinds):
    """`generated_artifacts` and `data_agent_analyses` have no store that names a bridge fact_key at
    all, so they are unmeasurable permanently — not "empty today". They carry the reason, and no
    amount of recorded plan telemetry can flip them."""
    conn = all_three_kinds.conn
    _record_candidate(conn, run="r1", intent="i1", plan="p1",
                      bridge_key=all_three_kinds.bridge, selected="p1")
    bridge = _bridge(governance_queue(conn, roles=()))
    for name in _BY_CONSTRUCTION:
        u = _usage(bridge, name)
        assert u.state == "not_tracked_yet"
        assert u.count is None
        assert u.reason and "not" in u.reason.lower()


def test_a_populated_store_that_does_not_name_this_bridge_is_a_real_zero(all_three_kinds):
    """HALF TWO OF THE DECISIVE PAIR, and the reason the first half is not just "always answer not
    tracked". A candidate plan is recorded whose operand crossed a DIFFERENT bridge: that store now
    demonstrably holds observations, so 0 for our bridge is a MEASUREMENT and must be reported as 0
    — while `selected_plans` (a different store, still empty: nothing was selected) stays
    `not tracked yet` in the SAME response. Per category, not per request."""
    conn = all_three_kinds.conn
    _record_candidate(conn, run="r1", intent="i1", plan="p1", bridge_key="bfk_someone_else",
                      selected=None)
    bridge = _bridge(governance_queue(conn, roles=()))
    assert bridge.fact_key == all_three_kinds.bridge

    planned = _usage(bridge, "planned_candidates")
    assert planned.state == "counted" and planned.count == 0 and planned.display == "0"
    selected = _usage(bridge, "selected_plans")
    assert selected.state == "not_tracked_yet" and selected.count is None


def test_a_candidate_that_names_this_bridge_is_counted(all_three_kinds):
    conn = all_three_kinds.conn
    ours = all_three_kinds.bridge
    _record_candidate(conn, run="r1", intent="i1", plan="p1", bridge_key=ours, selected="p1")
    _record_candidate(conn, run="r1", intent="i2", plan="p2", bridge_key=ours, selected=None)

    bridge = _bridge(governance_queue(conn, roles=()))
    assert _usage(bridge, "planned_candidates").count == 2      # both candidates crossed it
    assert _usage(bridge, "selected_plans").count == 1          # only one of them was SELECTED


def test_a_plan_spanning_two_catalogs_is_not_counted_without_explicit_lineage(all_three_kinds):
    """Usage counts ONLY from explicit lineage: a stored plan must DIRECTLY CONTAIN the fact_key. An
    operand observation on the two bridged catalogs that records NO crossing proves nothing about
    this bridge and must not be counted — while still making the store measurable, so the answer is
    a real 0 rather than `not tracked yet`."""
    conn = all_three_kinds.conn
    _record_candidate(conn, run="r1", intent="i1", plan="p1", bridge_key=None, selected="p1")
    bridge = _bridge(governance_queue(conn, roles=()))
    planned = _usage(bridge, "planned_candidates")
    assert planned.state == "counted" and planned.count == 0
    assert _usage(bridge, "selected_plans").count == 0   # the selection store observed it too


def test_published_feature_usage_reads_the_contract_bridge_MARKER(all_three_kinds):
    """`published_features` IS measurable, contrary to "publication lineage does not exist": a
    confirmed governed contract persists a governed_bridge path segment as `bridgefact:<fact_key>` in
    `contract_metadata_dependency.logical_ref`, and `feature_current_contract` points a registered
    feature at its current version."""
    conn = all_three_kinds.conn
    _record_published_feature(conn, feature="f1", contract="c1",
                              marker=bridge_fact_marker(all_three_kinds.bridge))
    _record_published_feature(conn, feature="f2", contract="c2",
                              marker=bridge_fact_marker("bfk_other"))

    published = _usage(_bridge(governance_queue(conn, roles=())), "published_features")
    assert published.state == "counted" and published.count == 1


def test_an_unreadable_usage_store_is_neither_zero_nor_not_tracked(all_three_kinds, monkeypatch):
    """A third state. If the store cannot be read the answer is `unreadable` — reporting 0 would be
    a fabricated measurement and `not tracked yet` would be a fabricated diagnosis."""
    from featuregen.overlay.upload import governance_queue as mod
    broken = mod._Category(
        name="planned_candidates", store="x", basis="y",
        probe_sql="SELECT * FROM a_table_that_does_not_exist", count_sql="SELECT 1")
    monkeypatch.setattr(mod, "_CATEGORIES", (broken,))
    usage = bridge_usage(all_three_kinds.conn, ["bfk_anything"])["bfk_anything"]
    assert [u.state for u in usage] == ["unreadable"]
    assert usage[0].count is None and usage[0].display == "unreadable"


def test_usage_is_attached_to_bridges_only(all_three_kinds):
    """A single-catalog join / table fact has no bridge anchor to count from, so it carries no usage
    block rather than an all-zero one."""
    for item in governance_queue(all_three_kinds.conn, roles=()).items:
        if item.kind == "entity_bridge":
            assert item.already_depended_on_by
        else:
            assert item.already_depended_on_by == ()


# ── Unreachable is distinguishable from empty ────────────────────────────────────────────────────

def test_an_unreadable_listing_is_reported_and_clears_complete(all_three_kinds, monkeypatch):
    """`items == [] and complete` means "nothing is waiting". `items == [] and not complete` means
    "we could not look"."""
    from featuregen.overlay.upload import governance_queue as mod

    def _boom(*_a, **_k):
        raise RuntimeError("the join listing is down")

    monkeypatch.setattr(mod, "list_open_approved_join_proposals", _boom)
    queue = governance_queue(all_three_kinds.conn, roles=())
    assert queue.complete is False
    assert any(u.listing == "approved_join" for u in queue.unreadable)
    # ...and the rest of the queue still rendered: one broken listing is not a blank screen.
    assert "entity_bridge" in _kinds(queue)


def test_an_unreadable_bridge_listing_does_not_blank_the_queue(all_three_kinds, monkeypatch):
    from featuregen.overlay.upload import governance_queue as mod

    def _boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(mod, "list_bridge_proposals", _boom)
    queue = governance_queue(all_three_kinds.conn, roles=())
    assert queue.complete is False
    assert "entity_bridge" not in _kinds(queue)
    assert {"approved_join", "grain"} <= _kinds(queue)


def test_limit_is_clamped_and_truncation_is_reported(all_three_kinds):
    conn = all_three_kinds.conn
    one = governance_queue(conn, roles=(), limit=1)
    assert len(one.items) == 1 and one.truncated is True
    assert governance_queue(conn, roles=(), limit=0).items != ()          # clamped up to 1
    assert governance_queue(conn, roles=(), limit=10_000).truncated is False


# ── Test-local recorders (the REAL stores' minimal legal rows) ─────────────────────────────────────

def _record_candidate(conn, *, run: str, intent: str, plan: str, bridge_key: str | None,
                      selected: str | None) -> None:
    """One minimal legal (dispatch, intent_result, candidate, operand_obs) chain — the real
    multisource assembly shadow store shape (migration 1019).

    `bridge_key=None` records an operand that crossed NOTHING (`crossings = []`), which is what a
    single-catalog operand looks like: a REAL observation of no crossing."""
    crossings = ("[]" if bridge_key is None else
                 '[{"kind": "governed_bridge", "catalog": "core", "table": "customer_master", '
                 f'"bridge_fact_key": "{bridge_key}", "realization_ref": null, '
                 '"authority": "verified_bridge", "confirmed_event_id": null}]')
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_dispatch (run_id, expected_intent_ids, "
        " expected_count, versions, versions_hash, shadow_flag, producer_commit, payload_hash, "
        " payload_schema_version) VALUES (%s, '[]'::jsonb, 0, '{}'::jsonb, 'vh', true, 'c', 'ph', "
        " 'v1') ON CONFLICT (run_id) DO NOTHING", (run,))
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_intent_result (run_id, intent_id, "
        " semantic_outcome, compile_completeness, technical_status, capture_status, "
        " normalized_intent_hash, selected_plan_id, payload_hash, payload_schema_version) "
        "VALUES (%s, %s, 'resolved', 'complete', 'ok', 'persisted', 'nh', %s, 'ph', 'v1') "
        "ON CONFLICT (run_id, intent_id) DO NOTHING", (run, intent, selected))
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_candidate (run_id, intent_id, plan_id, "
        " physical_landing, contract_input_hash, contract_output_hash, read_set_hash, "
        " replay_envelope_hash, rank, declaration_evidence, payload_schema_version) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'a', 'b', 'c', 'd', 0, '{}'::jsonb, 'v1')",
        (run, intent, plan))
    conn.execute(
        "INSERT INTO multisource_assembly_shadow_operand_obs (run_id, intent_id, plan_id, slot_id, "
        " pin, role, path_strategy, governed_endpoints, source_binding, crossings, "
        " payload_schema_version) "
        "VALUES (%s, %s, %s, 'slot1', '{}'::jsonb, 'measure', '{}'::jsonb, '[]'::jsonb, "
        " '{}'::jsonb, %s::jsonb, 'v1')", (run, intent, plan, crossings))


def _record_published_feature(conn, *, feature: str, contract: str, marker: str) -> None:
    """A registered feature -> its CURRENT contract version -> that version's bridge-marker
    dependency row: the real `feature` / `contract` / `feature_current_contract` /
    `contract_metadata_dependency` chain, whose FKs and composite UNIQUE the insert must satisfy."""
    conn.execute("INSERT INTO feature (feature_id, name) VALUES (%s, %s) "
                 "ON CONFLICT (feature_id) DO NOTHING", (feature, f"feature-{feature}"))
    conn.execute("INSERT INTO contract (contract_id, feature_id, feature_name, version) "
                 "VALUES (%s, %s, %s, 1) ON CONFLICT (contract_id) DO NOTHING",
                 (contract, feature, f"feature-{feature}"))
    conn.execute("INSERT INTO feature_current_contract (feature_id, contract_id, pointer_version) "
                 "VALUES (%s, %s, 1) ON CONFLICT (feature_id) DO NOTHING", (feature, contract))
    conn.execute(
        "INSERT INTO contract_metadata_dependency (contract_id, catalog_source, graph_ref, "
        " logical_ref, item_hash) VALUES (%s, 'core', %s, %s, %s) "
        "ON CONFLICT (contract_id, item_hash) DO NOTHING",
        (contract, marker, marker, f"h-{contract}-{marker}"))
