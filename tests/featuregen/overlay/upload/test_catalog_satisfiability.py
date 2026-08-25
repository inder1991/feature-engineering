"""T5 — the pre-planning catalog verdict: refuse with directions, or pass through untouched.

The audit's arrangement, in miniature and with both halves present: ``cib`` is the customer master
the live AML run actually named (no monetary column, one event timestamp — a consent date), ``ftr``
is the transaction catalog that sat unplanned beside it. Every number asserted here was measured
against the real 317-recipe V2 registry, not chosen.

The floor's own edges get their own tests, because the floor is the design decision in this task:
it is CLASS-level (an earlier concept-level cut refused a catalog that serves cards today), the bar
is a MAJORITY (a lower one refuses every catalog over the known ``status`` mapping gap), and a tie
serves rather than refuses.

**Both of those decisions are mutation-proved, and the proof is recorded because the first attempt
at it was wrong.** Scope for every figure below: this file plus
``tests/featuregen/api/test_contract_catalog_satisfiability.py`` — 18 tests.

* **The unit (class vs concept).** Mutating ``assess_catalog_satisfiability``'s coverage test from
  "no concept in this class is covered" to "any concept in this class is uncovered" — i.e. back to
  the concept-level cut — kills **12 of 18**. It previously killed ZERO, over the entire 8,049-test
  gate, because ``test_a_missing_entity_concept_never_refuses_a_catalog_whose_class_is_covered``
  built ONE catalog: with nothing to point at, sentence 5 dropped the breach and the test passed
  under both cuts. One fixture line — a second catalog carrying ``account_id`` — is the whole fix,
  and it is called out in that test's own docstring so it cannot be removed as decoration.
* **The bar (strict majority).** Relaxing ``>`` to ``>=`` kills **exactly 1 of 18**:
  ``test_an_exact_fifty_percent_tie_serves_rather_than_refuses``. That test had to be rewritten to
  earn it — the earlier version used ``customer.clv`` (5 eligible, floor ``5//2`` = 2, measure
  2/5 = 40%), where a ``>=`` mutant means "at or below half refuses" rather than "a tie refuses".
  It now uses ``credit.collections.workout``: EIGHT eligible, floor 4, measure exactly 4 = 50.0%.
"""
from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_satisfiability import (
    CATALOG_CANNOT_SATISFY_SCOPE,
    assess_catalog_satisfiability,
    concept_inventory,
)
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope, ScopeExpansion

CIB = "cib"
FTR = "ftr"
BANK = "bank"
#: The AML brief's confirmed scope. 15 eligible recipes on the V2 registry (measured), of which 8
#: carry a REQUIRED ``measure`` operand — a bare majority, which is the floor.
AML = "aml_cft.suspicious_transaction_monitoring"
CHURN = "customer.relationship_attrition.churn"


def _build(db, source: str, rows) -> None:
    build_graph(db, source, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _cib(db) -> None:
    """The customer master, audit-shaped: identity, one event timestamp that is a CONSENT date,
    segment/flag/rating attributes — and no measurable magnitude of any kind."""
    _build(db, CIB, [
        (CanonicalRow(CIB, "customer", "cust_num", "integer", is_grain=True, entity="Customer",
                      definition="the customer number"), "customer_id"),
        (CanonicalRow(CIB, "customer", "consent_ts", "timestamp",
                      definition="when marketing consent was captured"), "event_timestamp"),
        (CanonicalRow(CIB, "customer", "segment_code", "text",
                      definition="the customer segment"), "category_code"),
        (CanonicalRow(CIB, "customer", "cust_susp_flg", "boolean",
                      definition="suspicious customer flag"), "boolean_flag"),
        (CanonicalRow(CIB, "customer", "risk_rating", "text",
                      definition="the customer risk rating"), "customer_risk_rating"),
    ])


def _ftr(db) -> None:
    """The transaction catalog: the amount, the direction, the clock, the account."""
    _build(db, FTR, [
        (CanonicalRow(FTR, "txn", "cust_num", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow(FTR, "txn", "amount", "numeric", additivity="additive", currency="USD",
                      definition="signed transaction amount"), "monetary_flow"),
        (CanonicalRow(FTR, "txn", "credit_amt", "numeric", additivity="additive", currency="USD",
                      definition="credit leg amount"), "monetary_flow"),
        (CanonicalRow(FTR, "txn", "dc_flag", "text"), "debit_credit_indicator"),
        (CanonicalRow(FTR, "txn", "booked_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(FTR, "txn", "acct_ref", "integer", entity="Account"), "account_id"),
    ])


# ── the audit's arrangement: refuse, name the class, the concepts, and the catalog ──────────────

def test_the_audit_arrangement_refuses_and_names_ftr(db):
    """THE pin. AML-shaped eligible recipes + ``catalog_source=cib`` → a typed refusal naming the
    unsatisfiable operand class, how many recipes require it, which concepts the corpus asks for in
    it with their counts, and the catalog that has them."""
    _cib(db)
    _ftr(db)
    verdict = assess_catalog_satisfiability(
        db, catalog_source=CIB, scope=ConfirmedScope(primary=AML))

    assert verdict.satisfied is False
    assert verdict.eligible_recipes == 15          # measured against the V2 registry
    assert verdict.majority_floor == 7             # a class must be required by MORE than this
    (unsatisfiable,) = verdict.unsatisfiable
    assert unsatisfiable.operand_class == "measure"
    assert unsatisfiable.required_by == 8          # measured: 8 of 15, the bare majority
    assert len(unsatisfiable.recipe_ids) == 8
    assert "structuring_smurfing" in unsatisfiable.recipe_ids
    (concept,) = unsatisfiable.concepts
    assert (concept.concept, concept.required_by) == ("monetary_flow", 8)
    assert concept.available_in == ((FTR, 2),)     # the two enriched columns, counted
    assert unsatisfiable.sentence() == (
        "no read-scoped column can serve a measure operand, which 8 of the eligible recipes "
        "require: monetary_flow (8) — in ftr (2 columns)")
    # ▲ The half `semantic_projection` structurally cannot say: WHICH catalog to aim at.
    assert verdict.satisfying_catalog_sources() == (FTR,)
    served = verdict.to_json()
    assert served["code"] == CATALOG_CANNOT_SATISFY_SCOPE
    assert served["catalog_source"] == CIB
    assert served["satisfying_catalog_sources"] == [FTR]


def test_the_catalog_that_can_serve_the_brief_passes_through_untouched(db):
    """The property that matters most: the floor never refuses the RIGHT catalog."""
    _cib(db)
    _ftr(db)
    verdict = assess_catalog_satisfiability(
        db, catalog_source=FTR, scope=ConfirmedScope(primary=AML))
    assert verdict.satisfied is True
    assert verdict.unsatisfiable == ()
    assert verdict.eligible_recipes == 15
    assert verdict.satisfying_catalog_sources() == ()


def test_a_gap_the_estate_cannot_answer_is_not_refused(db):
    """▲ SENTENCE 5 — a refusal must be ACTIONABLE, or it is not a refusal.

    The identical catalog and the identical brief, with ``ftr`` simply absent from the estate. The
    measurement is unchanged (8 of 15 eligible recipes need a measure and cib has none), but there
    is nowhere to point, so the run proceeds: T2's ``needs_setup`` lane says the same thing per
    card, in the binder's own words, WITHOUT withholding the candidates that do compute.

    This is not a hypothetical branch. Without it, the four-objective coverage journeys — narrow
    catalogs built one column per operand, each governing its hero recipe end to end — were refused
    outright, because 12 of their 17 eligible recipes genuinely cannot compute there and no readable
    catalog carries the 10 measure concepts they ask for."""
    _cib(db)                                       # cib alone: no ftr to point at
    verdict = assess_catalog_satisfiability(
        db, catalog_source=CIB, scope=ConfirmedScope(primary=AML))
    assert verdict.satisfied is True
    assert verdict.satisfying_catalog_sources() == ()


def test_a_concept_with_nowhere_to_point_is_worded_as_absence_not_direction():
    """Sentence 5 drops a class the estate cannot answer AT ALL, but a class that IS refused may
    still carry a concept nothing holds — the corpus asks for several measures and one catalog
    rarely has them all. That concept must be worded as absence, never as a direction. Pinned on
    the carrier, because on today's AML fixture the refused class asks for exactly one concept and
    a payload-driven assertion would be vacuous."""
    from featuregen.overlay.upload.catalog_satisfiability import UnsatisfiableConceptV1

    directed = UnsatisfiableConceptV1(concept="monetary_flow", required_by=8,
                                      available_in=((FTR, 2),))
    assert directed.phrase() == "monetary_flow (8) — in ftr (2 columns)"
    nowhere = UnsatisfiableConceptV1(concept="collateral_value", required_by=2)
    assert nowhere.phrase() == "collateral_value (2) — carried by no catalog you can read"
    assert "in " not in nowhere.phrase().split("—")[1].strip()[:3]
    singular = UnsatisfiableConceptV1(concept="ead", required_by=1,
                                      available_in=(("risk", 1),))
    assert singular.phrase() == "ead (1) — in risk (1 column)"


# ── the floor's own edges: why CLASS, why MAJORITY, why fail-open ───────────────────────────────

def test_a_missing_entity_concept_never_refuses_a_catalog_whose_class_is_covered(db):
    """▲ THE MEASUREMENT THAT CHOSE THE FLOOR'S UNIT. Under a churn scope, 30 of the 44 eligible
    recipes carry a REQUIRED ``account_id`` operand, and this customer-grained catalog has none —
    so a CONCEPT-level floor (the first cut) refused it, and it is the catalog the serving suite's
    fixtures plan on and get cards from. Its class ``entity_key`` IS covered, by ``customer_id``.
    "Cannot reach the account-grained recipes" is a grain fact the UOA fold owns and states per
    card; "has no key at all" is a catalog-aiming fact. Only the second is a refusal.

    ▲ ``ftr`` IS LOAD-BEARING HERE, and its absence is what made the first version of this test
    vacuous. Sentence 5 drops a breaching class the estate cannot answer, so with ``bank`` alone in
    the store the concept cut and the class cut BOTH returned satisfied and this test — the one
    that exists to pin the floor's unit — killed neither. Measured with ``ftr`` present: unmutated
    ``satisfied=True``; with the coverage test mutated to concept level, ``satisfied=False`` and
    the breach is ``entity_key`` required_by 44."""
    _build(db, BANK, [
        (CanonicalRow(BANK, "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(BANK, "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow(BANK, "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow(BANK, "accounts", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow(BANK, "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(BANK, "accounts", "churned", "boolean"), "outcome_label"),
    ])
    _ftr(db)                                       # carries account_id — so clause 5 CAN answer
    verdict = assess_catalog_satisfiability(
        db, catalog_source=BANK, scope=ConfirmedScope(primary=CHURN))
    assert verdict.eligible_recipes == 44
    assert verdict.satisfied is True
    # The direction exists — which is precisely why a concept-level cut would refuse here.
    assert concept_inventory(db, concepts=["account_id"], exclude_catalog_source=BANK,
                             roles=())["account_id"] == ((FTR, 1),)


def test_the_known_status_mapping_gap_sits_under_the_floor_on_every_catalog(db):
    """▲ THE MEASUREMENT THAT CHOSE THE BAR. ``status`` is a GAP on every catalog measured — the
    registry's ``booking_status`` / ``account_status`` concepts have no catalog mapping yet, an
    owner-gated SME item — and it is required by 40-43% of the eligible recipes in every scope
    tried. A floor set below a majority would refuse every catalog in the estate over a gap the
    platform has already looked at and already decided not to close here. Asserted on the catalog
    that DOES serve the brief, so the point cannot be made vacuously."""
    _cib(db)
    _ftr(db)
    verdict = assess_catalog_satisfiability(
        db, catalog_source=FTR, scope=ConfirmedScope(primary=AML))
    assert verdict.satisfied is True
    # The gap is real — it is simply under the bar. Measured: 6 of the 15 eligible recipes.
    statuses = {op.concept
                for rid in ("dormant_reactivation",)
                for op in _operands(rid) if op.operand_class == "status" and op.required}
    assert statuses, "the status class really is required by recipes in this scope"
    assert not statuses & _concepts_on(db, FTR), "and this catalog really does not carry it"


def _operands(recipe_id: str):
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    return v2_recipe_by_id(recipe_id).operands


def _concepts_on(db, source: str) -> frozenset[str]:
    from featuregen.overlay.upload.catalog_satisfiability import _catalog_concepts

    return _catalog_concepts(db, catalog_source=source, roles=())


def test_a_class_below_the_majority_never_refuses(db):
    """``aml_cft`` + descendants widens the corpus to 20 eligible recipes, of which 9 require a
    ``measure`` — 45%, under the floor. Measured, and deliberately NOT refused: the fail-open tie
    rule is a decision, pinned here so that changing it is another one rather than a drift."""
    _cib(db)
    _ftr(db)
    verdict = assess_catalog_satisfiability(
        db, catalog_source=CIB,
        scope=ConfirmedScope(primary="aml_cft", expansion=ScopeExpansion.INCLUDE_DESCENDANTS))
    assert verdict.eligible_recipes == 20
    assert verdict.majority_floor == 10
    assert verdict.satisfied is True


def test_an_exact_fifty_percent_tie_serves_rather_than_refuses(db):
    """▲ SENTENCE 4, AT ITS ACTUAL BOUNDARY. ``credit.collections.workout`` has EIGHT eligible
    recipes — an even corpus, so ``//2`` really is half — and EXACTLY FOUR of them
    (``cost_to_collect_ratio``, ``recovery_rate``, ``write_off_amount_sum``,
    ``write_off_severity_share``) require a ``measure``. 4 of 8 is 50.0%, the one arrangement where
    "strictly more than half" and "at least half" give different answers.

    The even-n fixture matters. An earlier version of this test used ``customer.clv`` (5 eligible,
    floor ``5//2`` = 2, measure 2/5 = 40%), where a ``>=`` mutant means "at or below half refuses",
    not "a tie refuses" — it killed the mutant, but it was not measuring the decision it named.

    Everything else about the floor is satisfied here so that only the comparison decides:
    ``entity_key`` (8/8) is covered by cib's ``customer_id``, ``event_timestamp`` (8/8) by its
    ``consent_ts``, ``status`` (3/8) and ``dimension`` (2/8) sit below the floor either way, and
    ``collections`` carries ``write_off_amount`` so clause 5 has a direction and cannot mask the
    result. Unmutated: satisfied. With ``>`` relaxed to ``>=``: refused, breach ``measure`` 4."""
    _cib(db)
    _build(db, "collections", [                    # gives clause 5 something to point at
        (CanonicalRow("collections", "facility", "fac_ref", "integer", is_grain=True,
                      entity="Facility"), "facility_id"),
        (CanonicalRow("collections", "facility", "wo_amt", "numeric", additivity="additive",
                      currency="USD"), "write_off_amount"),
    ])
    scope = ConfirmedScope(primary="credit.collections.workout")
    verdict = assess_catalog_satisfiability(db, catalog_source=CIB, scope=scope)

    assert verdict.eligible_recipes == 8           # EVEN, so the floor is exactly half
    assert verdict.majority_floor == 4
    # The arrangement really is a 50% tie on an UNCOVERED class — without this the pin could pass
    # because the class was covered, or because it was nowhere near the floor.
    measure_recipes = {rid for rid in ("cost_to_collect_ratio", "recovery_rate",
                                       "write_off_amount_sum", "write_off_severity_share")}
    assert all(any(op.operand_class == "measure" and op.required for op in _operands(rid))
               for rid in measure_recipes)
    assert not {"cost_to_collect", "ead", "recovery_amount", "write_off_amount"} & _concepts_on(
        db, CIB), "cib carries none of the measure concepts this scope asks for"

    assert verdict.satisfied is True, (
        "a class required by exactly half the eligible recipes is a TIE, and a tie serves")


def test_closure_coverage_counts_as_coverage(db):
    """The local test is deliberately GENEROUS — it is the binder's own retrieval law, so a
    catalog is refused only for a class the binder could not even have looked at a column for. A
    column enriched with a concept whose registered closure reaches ``monetary_flow`` is
    coverage."""
    from featuregen.overlay.upload.concepts import concept_path, namespace_mates

    # `interest_income` is-a `monetary_flow` in the registry, so a column enriched with it is
    # RETRIEVED for a `monetary_flow` operand — asserted rather than assumed, so a registry edit
    # that breaks the is-a edge fails this test instead of silently making it vacuous.
    reaching = "interest_income"
    assert "monetary_flow" in {*concept_path(reaching), *namespace_mates(reaching)}
    _build(db, CIB, [
        (CanonicalRow(CIB, "customer", "cust_num", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow(CIB, "customer", "consent_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(CIB, "customer", "flow_like", "numeric", additivity="additive",
                      currency="USD"), reaching),
    ])
    # ftr must exist, or sentence 5 would drop the class for want of directions and this test
    # would pass whatever `_covered` did — the coverage law has to be the thing being measured.
    _ftr(db)
    assert assess_catalog_satisfiability(
        db, catalog_source=CIB, scope=ConfirmedScope(primary=AML)).satisfied is True
    # And the control: the SAME catalog with a concept whose closure does NOT reach the operand's
    # is refused, so the pass above is the widening and not the fixture.
    _build(db, "narrow", [
        (CanonicalRow("narrow", "customer", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow("narrow", "customer", "consent_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("narrow", "customer", "segment_code", "text"), "category_code"),
    ])
    assert assess_catalog_satisfiability(
        db, catalog_source="narrow", scope=ConfirmedScope(primary=AML)).satisfied is False


def test_an_empty_eligible_set_is_a_scope_problem_not_a_catalog_one(db):
    """No eligible recipes, no verdict to give: this seam must not dress a scope with nothing in
    it up as a catalog that cannot serve."""
    from featuregen.overlay.upload.recipe_planning_lens import v2_applicability

    _cib(db)
    empty = ConfirmedScope(primary="financial_crime")   # a non-selectable domain parent
    assert not v2_applicability(empty).eligible_ids
    verdict = assess_catalog_satisfiability(db, catalog_source=CIB, scope=empty)
    assert verdict.satisfied is True
    assert verdict.eligible_recipes == 0


# ── the inventory read: scoped, grouped, and never naming the catalog it was asked about ────────

def test_the_inventory_excludes_the_named_catalog_and_ranks_by_column_count(db):
    _cib(db)
    _ftr(db)
    found = concept_inventory(db, concepts=["monetary_flow", "customer_id"],
                              exclude_catalog_source=CIB, roles=())
    assert found["monetary_flow"] == ((FTR, 2),)
    assert found["customer_id"] == ((FTR, 1),)     # cib's own customer_id is excluded
    assert concept_inventory(db, concepts=[], exclude_catalog_source=CIB, roles=()) == {}


def test_the_verdict_costs_one_query_when_the_catalog_satisfies_the_scope(db):
    """The budget: the happy path is every request on a correctly-aimed catalog, and it pays ONE
    indexed aggregate. The refusal path pays a second read, for the breaching concepts only."""
    _cib(db)
    _ftr(db)
    calls: list[str] = []
    original = db.execute

    def counting(query, *args, **kwargs):
        calls.append(str(query))
        return original(query, *args, **kwargs)

    db.execute = counting
    try:
        assess_catalog_satisfiability(db, catalog_source=FTR, scope=ConfirmedScope(primary=AML))
        satisfied_calls = len(calls)
        assess_catalog_satisfiability(db, catalog_source=CIB, scope=ConfirmedScope(primary=AML))
    finally:
        db.execute = original
    assert satisfied_calls == 1, calls
    assert len(calls) - satisfied_calls == 2, calls
