"""Task S1A-5a §5 — the C2 authority floors, per catalog and conflict-gated.

**The pair arm.** A governed cross-catalog plan reads columns in several catalogs, so the floor has
to qualify each entry with ITS OWN catalog before asking the resolver about it. The old fold could
only qualify a read set with one plan-wide ``catalog_source``; given a cross-catalog plan it would
have attributed every foreign column to whichever catalog the plan happened to name — measuring the
authority of columns that do not exist and calling the answer a floor.

**One fold, two arms.** The arms differ only in how an entry is qualified into a logical ref.
Everything after that — the batched pin read, the authority rule, the two ``clears`` folds — is one
private helper, so the two cannot drift into two answers about the same pin. Wherever it matters
the tests assert exactly that: same evidence, both shapes, same three answers.

**The conflict gate, and the clause that is deliberately absent.** An authority is the WINNING
evidence's ``producer/strength``, and a pin the resolver calls a CONFLICT lends none. The gate does
NOT also require ``load_bearing`` / ``conflict_state == "resolved"`` — the verbatim D4 clause — and
``test_a_human_confirmed_concept_is_NOT_load_bearing_and_must_still_clear`` is the regression pin
that says why: ``concept`` — the only field these floors read — is a RECOMMENDATION-tier policy, so
no concept pin is EVER load-bearing or ever "resolved". Gating on either clause would make both
floors permanently unmeetable for every column in every catalog. That test fails the moment somebody
re-applies the D4 clause here, which is the whole point of it.

**S1A-5b collapsed the two copies into one.** ``contract/governed_lens`` used to apply the verbatim
D4 clause on the SERVING side, so the same pin could lend ``human/confirmed`` to a floor here and
read ``absent`` on the card it was served from — and D4 was inert against real data for exactly the
reason above. Both sides now call ``field_resolution.pin_authority``;
``test_the_lens_and_the_activation_floors_share_ONE_authority_law``
(``contract/test_governed_lens_requests.py``) pins the sharing by identity.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload import field_resolution
from featuregen.overlay.upload.activation_policy import FrozenOptionFactsV1
from featuregen.overlay.upload.field_resolution import (
    ResolutionPinV1,
    current_resolution_pins,
)
from featuregen.overlay.upload.semantic_option_decision import assemble_current_activation_state

BANK_REF = "public.accounts.acct_id"
CRM_REF = "sales.customers.cust_id"
BANK_LOGICAL = "bank::public.accounts.acct_id"
CRM_LOGICAL = "crm::sales.customers.cust_id"


def _seed(conn, logical_ref: str, *, producer: str, strength: str, value: str,
          marker: str) -> None:
    """One ACTIVE field_evidence row, read back through the REAL resolver — never a stubbed pin."""
    conn.execute(
        "INSERT INTO field_evidence (evidence_id, logical_ref, field_name, proposed_value, "
        "proposed_value_hash, producer, strength, lifecycle, producer_ref, source_snapshot_id, "
        "input_hash) VALUES (%s, %s, 'concept', %s, %s, %s, %s, 'active', 'test', 'snap', %s)",
        (f"ev-{marker}", logical_ref, f'"{value}"', f"vh-{marker}", producer, strength,
         f"ih-{marker}"))


def _pairs_frozen(*pairs: tuple[str, str]) -> FrozenOptionFactsV1:
    """A frozen option whose plan is governed cross-catalog. ``llm_intent`` origin keeps the
    recipe registry, review store and readiness re-fold out of the way — this suite measures the
    floors and nothing else."""
    return FrozenOptionFactsV1(
        binding_state="bound", generation_source="llm_intent",
        computation_kind="deterministic_formula", readiness="FORMULA_AUTHORABLE",
        review_current=False, plan_kind="governed_cross_catalog", read_set_pairs=pairs)


def _single_frozen(catalog: str, *refs: str) -> FrozenOptionFactsV1:
    """The LEGACY shape: bare refs plus one plan-wide catalog every entry inherits."""
    return FrozenOptionFactsV1(
        binding_state="bound", generation_source="llm_intent",
        computation_kind="deterministic_formula", readiness="FORMULA_AUTHORABLE",
        review_current=False, plan_kind="single_source", read_set=refs,
        plan_catalog_source=catalog)


def _floors(conn, frozen: FrozenOptionFactsV1) -> tuple[bool, bool, bool]:
    """(execution_evaluated, execution_floor_met, authoring_floor_met) — the three answers."""
    state = assemble_current_activation_state(conn, frozen=frozen, snapshot_id=None)
    return (state.execution_authority_evaluated, state.execution_floor_met,
            state.authoring_floor_met)


# ── the pair arm: per-catalog attribution ────────────────────────────────────────────────────────


def test_two_catalogs_that_both_clear_meet_both_floors(conn):
    """The forward case, over REAL evidence in two different catalogs — the thing the old fold
    could not express at all."""
    _seed(conn, BANK_LOGICAL, producer="human", strength="confirmed", value="account_identifier",
          marker="b1")
    _seed(conn, CRM_LOGICAL, producer="human", strength="confirmed", value="customer_identifier",
          marker="c1")

    assert _floors(conn, _pairs_frozen(("bank", BANK_REF), ("crm", CRM_REF))) == (True, True, True)


def test_one_catalog_short_of_the_EXECUTION_floor_fails_the_whole_set(conn):
    """THE DISCRIMINATOR the task names: catalog A's pin clears ``execution_at_governed`` and
    catalog B's does not. The floor is measured (``evaluated``) and NOT met — a read set is
    authorized as a whole or not at all, because a pipeline reads every column in it.

    Asserted as a DELTA against the same fixture minus catalog B, so the refusal is provably
    catalog B's and not the fixture's."""
    _seed(conn, BANK_LOGICAL, producer="human", strength="confirmed", value="account_identifier",
          marker="b2")
    _seed(conn, CRM_LOGICAL, producer="llm", strength="proposed", value="customer_identifier",
          marker="c2")

    assert _floors(conn, _pairs_frozen(("bank", BANK_REF))) == (True, True, True)
    assert _floors(conn, _pairs_frozen(("bank", BANK_REF), ("crm", CRM_REF))) == (True, False,
                                                                                  False)


def test_a_catalog_with_no_evidence_at_all_is_absent_and_clears_nothing(conn):
    """The other half of "measured": the pair was looked at, and the answer is that the second
    catalog has nothing to lend. Absence is a measured NO, never an unmeasured pass."""
    _seed(conn, BANK_LOGICAL, producer="human", strength="confirmed", value="account_identifier",
          marker="b3")

    assert _floors(conn, _pairs_frozen(("bank", BANK_REF), ("crm", CRM_REF))) == (True, False,
                                                                                  False)


def test_each_pair_is_qualified_with_its_OWN_catalog(conn):
    """The defect the pair arm exists to prevent, stated as a test: the SAME bare ref in two
    catalogs is two objects. Evidence on ``bank``'s copy must not clear ``crm``'s."""
    _seed(conn, BANK_LOGICAL, producer="human", strength="confirmed", value="account_identifier",
          marker="b4")

    assert _floors(conn, _pairs_frozen(("bank", BANK_REF))) == (True, True, True)
    assert _floors(conn, _pairs_frozen(("crm", BANK_REF))) == (True, False, False)


def test_an_llm_proposed_concept_never_clears_either_floor(conn):
    """The matrix, not the pin, is what decides what a concept's authority may authorize — and it
    refuses ``llm/proposed`` at every rung. This is the rule the floors actually rest on."""
    _seed(conn, BANK_LOGICAL, producer="llm", strength="proposed", value="account_identifier",
          marker="b5")

    assert _floors(conn, _pairs_frozen(("bank", BANK_REF))) == (True, False, False)


def test_a_governed_plan_with_no_pairs_leaves_the_floors_UNEVALUATED(conn):
    """Nothing to measure is not a pass — it is the unevaluated state the policy blocks on."""
    assert _floors(conn, _pairs_frozen()) == (False, False, False)


# ── the conflict gate, in BOTH arms, over REAL contradictory evidence ────────────────────────────


def _conflict(conn, logical_ref: str, marker: str) -> None:
    """Two human CONFIRMATIONS that disagree — the state the resolver refuses to resolve."""
    _seed(conn, logical_ref, producer="human", strength="confirmed", value="account_identifier",
          marker=f"{marker}-a")
    _seed(conn, logical_ref, producer="human", strength="confirmed", value="customer_identifier",
          marker=f"{marker}-b")


def test_the_fixture_really_produces_a_CONFLICTED_pin(conn):
    """The suite proves its own fixture before resting anything on it: two contradictory human
    confirmations really do come back as the resolver's ``conflict`` verdict."""
    _conflict(conn, BANK_LOGICAL, "fix")

    pin = current_resolution_pins(
        conn, logical_refs=[BANK_LOGICAL], fields=("concept",))[(BANK_LOGICAL, "concept")]
    assert pin.conflict_state == "conflict"
    assert pin.load_bearing is False


def test_a_conflicted_concept_reads_absent_in_the_PAIR_arm(conn):
    _conflict(conn, BANK_LOGICAL, "p")

    assert _floors(conn, _pairs_frozen(("bank", BANK_REF))) == (True, False, False)


def test_a_conflicted_concept_reads_absent_in_the_SINGLE_CATALOG_arm(conn):
    """A column whose meaning is actively disputed must not authorize a materialization. Note
    what this test is NOT: a behavior change on real data. The resolver blanks
    ``producer``/``strength`` on a conflict (there is no winning view), so the old fold's
    ``if pin.producer`` check already answered ``absent`` here. The gate is now STATED rather than
    incidental — the safety no longer depends on that blanking."""
    _conflict(conn, BANK_LOGICAL, "s")

    assert _floors(conn, _single_frozen("bank", BANK_REF)) == (True, False, False)


def test_a_conflicted_pin_that_still_names_a_winner_reads_absent_in_BOTH_arms(conn, monkeypatch):
    """The case the explicit gate exists for, and the ONE behavior change in this task: a pin the
    resolver calls a CONFLICT while still naming a winning view. The old fold read
    ``human/confirmed`` off it and cleared the execution floor; both arms now read ``absent``.

    It is reachable in principle — a display selection can succeed while the operational strategy
    ties — but not on ``concept`` today, which is why it is asserted against a fixed pin rather
    than seeded evidence: the honest way to test a state real data cannot currently reach."""
    contested = ResolutionPinV1(
        value="account_identifier", producer="human", strength="confirmed",
        evidence_id="ev-contested", conflict_state="conflict", load_bearing=False)

    def _pins(conn, *, logical_refs, fields):
        del conn, fields
        return {(ref, "concept"): contested for ref in logical_refs}

    monkeypatch.setattr(field_resolution, "current_resolution_pins", _pins)

    assert (_floors(conn, _pairs_frozen(("bank", BANK_REF)))
            == _floors(conn, _single_frozen("bank", BANK_REF))
            == (True, False, False))


def test_the_two_arms_answer_IDENTICALLY_about_the_same_evidence(conn):
    """The anti-drift pin, and the reason the fold was extracted into one helper. A human
    PROPOSAL is not a confirmation: it clears nothing, in either shape."""
    _seed(conn, BANK_LOGICAL, producer="human", strength="proposed", value="account_identifier",
          marker="d1")

    assert (_floors(conn, _pairs_frozen(("bank", BANK_REF)))
            == _floors(conn, _single_frozen("bank", BANK_REF))
            == (True, False, False))


# ── the clause that must NOT be added (the fatal one) ────────────────────────────────────────────


def test_a_human_confirmed_concept_is_NOT_load_bearing_and_must_still_clear(conn):
    """**THE REGRESSION PIN, and the reason ``pin_authority`` diverges from D4.**

    ``field_policies._CONCEPT`` is RECOMMENDATION-tier, so ``resolve_field_authority``
    short-circuits on the influence ceiling before it ever selects a load-bearing value: EVERY
    concept pin — a human confirmation included — comes back ``load_bearing=False`` with
    ``conflict_state='influence_not_operational'``, never ``'resolved'``.

    So a floor gated on ``load_bearing`` or on ``conflict_state == 'resolved'`` (the verbatim D4
    rule ``contract/governed_lens`` applied on the serving side until S1A-5b) would read ``absent``
    for every column in every catalog, and BOTH floors would be permanently unmeetable — no governed
    contract, no materialization, anywhere. For an advisory-tier field the load-bearing question
    is not the right one; ``AUTHORITY_MATRIX`` is the gate, and it already refuses what it should.

    This test asserts the pin's shape AND the floor's answer together, so it fails the moment the
    clause is re-applied here, and the failure explains itself."""
    _seed(conn, BANK_LOGICAL, producer="human", strength="confirmed", value="account_identifier",
          marker="rec")

    pin = current_resolution_pins(
        conn, logical_refs=[BANK_LOGICAL], fields=("concept",))[(BANK_LOGICAL, "concept")]
    assert (pin.producer, pin.strength) == ("human", "confirmed")
    assert pin.load_bearing is False                       # the ceiling, not a data problem
    assert pin.conflict_state == "influence_not_operational"

    assert (_floors(conn, _pairs_frozen(("bank", BANK_REF)))
            == _floors(conn, _single_frozen("bank", BANK_REF))
            == (True, True, True))


# ── the legacy arm, otherwise unchanged ──────────────────────────────────────────────────────────


def test_a_legacy_single_catalog_row_is_UNCHANGED(conn):
    """Over real, uncontested evidence the legacy arm answers exactly what it always did."""
    _seed(conn, BANK_LOGICAL, producer="human", strength="confirmed", value="account_identifier",
          marker="l1")

    assert _floors(conn, _single_frozen("bank", BANK_REF)) == (True, True, True)


def test_the_legacy_arm_still_needs_BOTH_a_read_set_and_a_catalog(conn):
    """Unchanged: a bare read set with no catalog cannot be qualified, so nothing is measured."""
    assert _floors(conn, _single_frozen("", BANK_REF)) == (False, False, False)
    assert _floors(conn, _single_frozen("bank")) == (False, False, False)


def test_the_PAIR_arm_takes_precedence_when_a_row_carries_both(conn):
    """Ordering, asserted rather than assumed. A row carrying both shapes is answered from the
    pairs — the qualified attribution is the precise one, and the plan-wide catalog is exactly
    what a cross-catalog plan must not fall back to."""
    _seed(conn, BANK_LOGICAL, producer="human", strength="confirmed", value="account_identifier",
          marker="p1")
    both = FrozenOptionFactsV1(
        binding_state="bound", generation_source="llm_intent",
        computation_kind="deterministic_formula", readiness="FORMULA_AUTHORABLE",
        review_current=False, plan_kind="governed_cross_catalog",
        # the pairs name the catalog that HAS the evidence; the legacy fields name one that
        # does not, so the two shapes give opposite answers and only one can be the source.
        read_set=(BANK_REF,), plan_catalog_source="crm",
        read_set_pairs=(("bank", BANK_REF),))

    assert _floors(conn, both) == (True, True, True)


def test_an_unreadable_floor_fails_closed_in_the_pair_arm(conn, monkeypatch):
    """The same posture the legacy arm has always had: a resolver read that raises leaves the
    floor UNEVALUATED, never quietly met."""
    def _boom(conn, *, logical_refs, fields):
        raise RuntimeError("the resolution store is unreadable")

    monkeypatch.setattr(field_resolution, "current_resolution_pins", _boom)

    assert _floors(conn, _pairs_frozen(("bank", BANK_REF))) == (False, False, False)


@pytest.mark.parametrize("pair", [("bank", "acct_id"), ("bank", "")])
def test_a_pair_that_cannot_be_qualified_fails_the_floor_CLOSED(conn, pair):
    """Defence in depth against a pair the loader could not have produced: qualifying it raises,
    and the arm's own except leaves the floors unevaluated rather than partially measured."""
    assert _floors(conn, _pairs_frozen(pair)) == (False, False, False)
