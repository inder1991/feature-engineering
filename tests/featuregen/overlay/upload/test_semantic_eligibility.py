"""SE-4 — the eligibility fold: every known bad binding class refuses, honestly and actionably."""
from __future__ import annotations

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.column_capabilities import ColumnCapabilityV1
from featuregen.overlay.upload.concept_operand_classes import OPERAND_CLASS_MAP_VERSION
from featuregen.overlay.upload.feature_planning_contracts import RequiredOperandV1
from featuregen.overlay.upload.semantic_eligibility import (
    AUTHORITY_MATRIX,
    authority_matrix_hash,
    evaluate_operand,
)


def capability(**over) -> ColumnCapabilityV1:
    base = dict(
        object_ref="public.transactions.amount", table="transactions", column="amount",
        declared_type="numeric", type_family="numeric", is_grain=False, is_as_of=False,
        concept="monetary_flow", concept_authority="human/confirmed",
        identifier_namespace=None, identifier_like=False,
        leakage_anchor=False, blocked_sensitivity=False,
        possible_operand_classes=("measure",),
        operand_class_map_version=OPERAND_CLASS_MAP_VERSION,
        entity=None, entity_authority="absent",
        additivity="additive", additivity_authority="graph_hint",
        currency="USD", currency_authority="graph_hint",
        economic_role=None, economic_role_authority="absent",
        table_event_or_snapshot=None, table_event_or_snapshot_authority="absent",
        missing_context=("dataset_profile_absent", "relationship_state_absent",
                         "use_policy_absent"),
        retrieval_text="signed transaction amount")
    base.update(over)
    return ColumnCapabilityV1(**base)


def operand(**over) -> RequiredOperandV1:
    base = dict(role="amount", concept="monetary_flow", operand_class="measure")
    base.update(over)
    return RequiredOperandV1(**base)


def test_confirmed_authority_clears_the_declared_floor():
    verdict = evaluate_operand(operand(), capability())
    assert verdict.status == "eligible"
    assert verdict.reason_codes == ()
    assert verdict.authority_observed == "human/confirmed"
    assert verdict.policy_content_hash == authority_matrix_hash()


def test_a_proposal_retrieves_but_never_clears_and_names_the_funnel():
    verdict = evaluate_operand(operand(), capability(concept_authority="llm/proposed"))
    assert verdict.status == "provisional"
    assert verdict.primary_reason_code == R.PROPOSED_METADATA_ONLY
    assert verdict.primary_family == "undecided"
    assert "concept-confirmation queue" in verdict.resolution


def test_a_display_only_graph_value_clears_nothing():
    verdict = evaluate_operand(operand(), capability(concept_authority="graph_hint"))
    assert verdict.status == "provisional"
    assert verdict.primary_reason_code == R.SEMANTIC_AUTHORITY_INSUFFICIENT


def test_an_unknown_authority_class_fails_closed():
    verdict = evaluate_operand(operand(), capability(concept_authority="oracle/divined"))
    assert verdict.status == "provisional"
    assert AUTHORITY_MATRIX.get("oracle/divined") is None


def test_a_different_meaning_is_not_applicable_never_a_lesser_match():
    verdict = evaluate_operand(
        operand(), capability(concept="consent_status",
                              possible_operand_classes=("policy_input",)))
    assert verdict.status == "not_applicable"
    assert verdict.reason_codes == (R.CONCEPT_MISMATCH,)
    assert verdict.primary_family == "structurally_unsuitable"


def test_an_identifier_never_serves_a_measure_even_confirmed():
    verdict = evaluate_operand(
        operand(concept="customer_id"),
        capability(concept="customer_id", concept_authority="human/confirmed",
                   identifier_namespace="cif", identifier_like=True,
                   possible_operand_classes=("dimension", "entity_key")))
    assert verdict.status == "blocked"
    assert R.IDENTIFIER_NOT_A_MEASURE in verdict.reason_codes
    assert "never a quantity" in verdict.resolution


def test_all_applicable_codes_are_collected_with_a_precedence_primary():
    """A varchar identifier offered as a measure with an unmet economic role: THREE truths at
    once, all named, structural first."""
    verdict = evaluate_operand(
        operand(concept="customer_id", economic_role="drawn_credit_exposure"),
        capability(concept="customer_id", concept_authority="llm/proposed",
                   declared_type="varchar(30)", type_family="text",
                   identifier_namespace="cif", identifier_like=True,
                   possible_operand_classes=("dimension", "entity_key")))
    assert verdict.status == "blocked"
    assert set(verdict.reason_codes) == {
        R.IDENTIFIER_NOT_A_MEASURE, R.TYPE_INCOMPATIBLE, R.ECONOMIC_ROLE_UNPROVEN}
    assert verdict.primary_reason_code == R.IDENTIFIER_NOT_A_MEASURE


def test_missing_evidence_is_never_blocked_and_checks_are_split_out():
    """Invariant 6: missing and contradictory are different conditions — a currency-expecting
    operand over a currency-less column is SETUP work, not a contradiction."""
    verdict = evaluate_operand(
        operand(currency_expectation="per_row", relationship_requirement="verified_join"),
        capability(currency=None, currency_authority="absent"))
    assert verdict.status == "provisional"
    assert R.CURRENCY_POLICY_MISSING in verdict.missing_checks
    assert R.RELATIONSHIP_REQUIRED in verdict.missing_checks
    assert R.CURRENCY_POLICY_MISSING in verdict.reason_codes


def test_economic_role_binds_only_over_governed_evidence():
    demanding = operand(economic_role="drawn_credit_exposure")
    unproven = evaluate_operand(demanding, capability())
    assert unproven.status == "blocked"
    assert R.ECONOMIC_ROLE_UNPROVEN in unproven.reason_codes
    proven = evaluate_operand(demanding, capability(
        economic_role="drawn_credit_exposure",
        economic_role_authority="human/confirmed"))
    assert proven.status == "eligible"


def test_the_policy_identity_moves_with_the_matrix():
    baseline = authority_matrix_hash()
    assert baseline == authority_matrix_hash()                 # deterministic
    try:
        AUTHORITY_MATRIX["llm/proposed"]["suggestion_at_declared"] = True
        assert authority_matrix_hash() != baseline             # any policy edit is a new policy
    finally:
        AUTHORITY_MATRIX["llm/proposed"]["suggestion_at_declared"] = False
    assert authority_matrix_hash() == baseline


def test_every_reason_code_has_a_family():
    codes = [getattr(R, name) for name in dir(R)
             if name.isupper() and isinstance(getattr(R, name), str)
             and not name.startswith("REASON")]
    for code in codes:
        assert R.reason_family(code), code


def test_a_leakage_anchor_is_blocked_at_any_authority():
    """The legacy _safe_to_bind law, folded: a target-defining concept never binds — even
    human-confirmed, even when a definition is mis-authored to NEED it."""
    verdict = evaluate_operand(
        operand(concept="delinquency_flag", operand_class="status"),
        capability(concept="delinquency_flag", concept_authority="human/confirmed",
                   declared_type="boolean", type_family="boolean",
                   possible_operand_classes=("status",), leakage_anchor=True))
    assert verdict.status == "blocked"
    assert verdict.reason_codes == (R.TARGET_LEAKAGE_BLOCKED,)
    assert "leakage" in verdict.resolution


def test_a_protected_characteristic_is_blocked_at_any_authority():
    verdict = evaluate_operand(
        operand(concept="monetary_flow"),
        capability(blocked_sensitivity=True))
    assert verdict.status == "blocked"
    assert verdict.reason_codes == (R.PROTECTED_CHARACTERISTIC_BLOCKED,)
    assert "fair-lending" in verdict.resolution


def test_the_compiler_carries_the_safety_facts_from_the_registry(db):
    """End to end: a column classified with a leakage-anchor concept compiles with the flag
    set, so the fold blocks it in the capability PATH, not only in hand-built fixtures."""
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.column_capabilities import compile_capabilities
    from featuregen.overlay.upload.enrich import content_hash
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.graph import build_graph

    rows = [(CanonicalRow("safebank", "loans", "dpd_flag", "boolean",
                          definition="90+ days past due"), "delinquency_flag")]
    build_graph(db, "safebank", [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    context = build_generation_semantic_context(db, catalog_source="safebank")
    caps = compile_capabilities(db, context, ["public.loans.dpd_flag"])
    assert caps["public.loans.dpd_flag"].leakage_anchor is True


# ── C2: the four use columns ───────────────────────────────────────────────────────────────────

def test_the_matrix_grades_use_not_just_suggestion():
    """C2: four rungs per evidence class. The load-bearing rows: a source DECLARATION may be
    suggested and authored against but NEVER executed over; llm/proposed NEVER clears
    execution (or authoring); everything with a value retrieves; absence clears nothing."""
    from featuregen.overlay.upload.semantic_eligibility import AUTHORITY_MATRIX, clears

    for authority, row in AUTHORITY_MATRIX.items():
        assert set(row) == {"retrieval", "suggestion_at_declared", "authoring",
                            "execution_at_governed"}, authority
        # Monotone down the ladder: anything that executes can author; anything that
        # authors can be suggested; anything usable at all retrieves.
        assert not (row["execution_at_governed"] and not row["authoring"]), authority
        assert not (row["authoring"] and not row["suggestion_at_declared"]), authority
        assert not (row["suggestion_at_declared"] and not row["retrieval"]), authority

    assert clears("source/declared", "authoring")
    assert not clears("source/declared", "execution_at_governed")
    assert not clears("llm/proposed", "authoring")
    assert not clears("llm/proposed", "execution_at_governed")
    assert clears("human/confirmed", "execution_at_governed")
    assert clears("source/attested", "execution_at_governed")
    assert not clears("absent", "retrieval")
    assert not clears("unknown/thing", "authoring")      # fail-closed on both axes
    assert not clears("human/confirmed", "unknown_use")


def test_growing_the_matrix_moved_the_policy_hash_by_design():
    """The matrix content is part of the policy hash — frozen options pinned to the pre-C2
    hash now read as ACTIVATION_STATE_DRIFTED and regenerate, which is the intended rollout."""
    from featuregen.overlay.upload.semantic_eligibility import authority_matrix_hash

    assert "execution_at_governed" in str(sorted(
        __import__("featuregen.overlay.upload.semantic_eligibility",
                   fromlist=["AUTHORITY_MATRIX"]).AUTHORITY_MATRIX["human/confirmed"]))
    assert authority_matrix_hash()  # stable + computable; content-addressed by construction


# ── C3: the authored-but-unconsumed constraints now enforce or refuse, by name ─────────────────

class _Output:
    """The request-level output context the additivity law consumes — duck-shaped."""

    def __init__(self, *, entity_agg="sum over accounts", time_agg="sum over windows",
                 unit_kind="monetary", zero_denominator_policy=""):
        self.aggregation_over_entity = entity_agg
        self.aggregation_over_time = time_agg
        self.unit_kind = unit_kind
        self.zero_denominator_policy = zero_denominator_policy


def test_source_grain_enforces_the_axis_the_catalog_can_prove():
    """C3: an operand allowing only snapshot-shaped grains cannot bind a table DECLARED to be
    event-shaped — and a merely-proposed table fact blocks nothing (the SE-8p2 posture)."""
    snap_only = operand(allowed_source_grains=("account_day_snapshot", "deposit_snapshot"))
    declared_event = capability(table_event_or_snapshot="event",
                                table_event_or_snapshot_authority="source/declared")
    verdict = evaluate_operand(snap_only, declared_event)
    assert verdict.status == "blocked"
    assert R.SOURCE_GRAIN_MISMATCH in verdict.reason_codes

    proposed_event = capability(table_event_or_snapshot="event",
                                table_event_or_snapshot_authority="llm/proposed")
    assert evaluate_operand(snap_only, proposed_event).status == "eligible"

    # A shape the catalog has no fact for (interval/report/...) skips enforcement honestly.
    unverifiable = operand(allowed_source_grains=("product_holding_interval",))
    assert evaluate_operand(unverifiable, declared_event).status == "eligible"


def test_a_currency_bearing_column_cannot_serve_a_non_monetary_unit():
    counting = operand(unit_expectation="count")
    verdict = evaluate_operand(counting, capability(currency="USD"))
    assert verdict.status == "blocked"
    assert R.UNIT_INCOMPATIBLE in verdict.reason_codes

    assert evaluate_operand(operand(unit_expectation="monetary"),
                            capability(currency="USD")).status == "eligible"
    assert evaluate_operand(counting, capability(currency=None)).status == "eligible"


def test_an_unresolved_status_policy_is_named_setup_work_never_silent():
    gated = operand(status_policy_ref="eligible_status:foundation-posted-events")
    verdict = evaluate_operand(gated, capability())
    assert verdict.status == "provisional"          # visible and actionable — never blocked
    assert R.STATUS_POLICY_UNRESOLVED in verdict.reason_codes
    assert "status" in verdict.resolution


def test_summing_what_cannot_be_summed_is_blocked_by_declared_additivity():
    """C3: sum over non_additive never; sum over semi_additive only under an as-of anchor
    (a stock sums across entities at a point in time, never across time)."""
    summed = operand()
    ratio_col = capability(additivity="non_additive", currency=None)
    verdict = evaluate_operand(summed, ratio_col, output=_Output(), temporal_anchor="event")
    assert verdict.status == "blocked"
    assert R.ADDITIVITY_INCOMPATIBLE in verdict.reason_codes

    stock_col = capability(additivity="semi_additive")
    verdict = evaluate_operand(summed, stock_col, output=_Output(), temporal_anchor="event")
    assert R.ADDITIVITY_INCOMPATIBLE in verdict.reason_codes
    verdict = evaluate_operand(summed, stock_col, output=_Output(), temporal_anchor="as_of")
    assert R.ADDITIVITY_INCOMPATIBLE not in verdict.reason_codes

    # An averaging operation is untouched — the law is about SUM.
    averaged = _Output(entity_agg="average over accounts", time_agg="mean over window")
    verdict = evaluate_operand(summed, ratio_col, output=averaged, temporal_anchor="event")
    assert R.ADDITIVITY_INCOMPATIBLE not in verdict.reason_codes
