"""Targeted semantic adjudication + ontology-gap suggestions (semantic plan Task 5).

The contract under test, per the plan and the controlling interfaces doc:

* selection is a PURE, deterministic function of computed facts — never a model-confidence
  threshold, and never fired for an already-clear column;
* the answer is closed and registry-validated, EVERY accepted concept passes
  `canonical_concept_name` (D12.1), and free text never becomes a concept;
* `confidence_band` is explanation, not authority — an off-vocabulary band invalidates the result
  rather than being reinterpreted, and nothing branches on the value;
* the validated output persists as `structured_result` type `semantic_adjudication` v2 with its
  `llm_call` provenance through the EXISTING `structured_result_provenance` mechanism;
* an ontology gap writes NO concept evidence and never touches `CONCEPT_REGISTRY`; it rides the
  migration-1046 subject/current pointer (CAS, idempotent, supersedable);
* the versioned output schema has a REAL registered body — `_require_schema` proves it (D10) — and
  every payload key is classified in the egress allowlists;
* the six stage OUTCOMES live in the stage DETAIL; the stage STATE stays inside the 0996 vocabulary
  (D12.3).

FakeLLM plays the provider throughout; the real audited dispatch seam, the real 1039 store and the
real 1046 pointer run underneath. No live LLM.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import semantic_adjudication as adj
from featuregen.overlay.upload import semantic_gap as gap
from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY, UNCLASSIFIED
from featuregen.overlay.upload.enrich_llm import (
    _redact_free_text_meta,
    _require_schema,
    register_enrichment_schemas,
)
from featuregen.overlay.upload.semantic_context import MISSING_CONTEXT_CODES, REASON_CODES
from featuregen.overlay.upload.stage_report import CANONICAL_STAGES, STAGE_STATES
from featuregen.overlay.upload.structured_results import (
    StructuredResultPointerConflict,
    list_current_structured_results,
    load_current_structured_result,
    load_structured_result,
    record_structured_result,
    set_current_structured_result,
    withdraw_current_structured_result,
)

_REF = "cib::dpl_eib_compliance.bo_cib_customer.sol_desc"


def _candidate(**kw) -> adj.AdjudicationCandidateV1:
    base = {"logical_ref": _REF, "column_name": "sol_desc", "declared_type": "varchar(255)",
            "definition": "Branch description", "concept": "customer_id"}
    base.update(kw)
    return adj.AdjudicationCandidateV1(**base)


def _answer(**kw) -> dict:
    out = {"selected_concept": "branch_id", "alternatives": ["customer_id"],
           "confidence_band": "medium", "reason_codes": ["name_only_signal"],
           "missing_context": ["definition_missing"]}
    out.update(kw)
    return out


def _client(output: dict | None = None) -> FakeLLM:
    return FakeLLM(script={
        adj.SEMANTIC_ADJUDICATION_TASK: FakeResponse(output=output or _answer()),
    })


def _target(candidate=None, reasons=("unclassified_column",), context=None):
    return adj.AdjudicationTargetV1(
        candidate=candidate or _candidate(concept=None), selection_reasons=tuple(reasons),
        context=context)


# ── selection: deterministic, pure, and never for a clear column ────────────────────────────────


def test_a_clear_column_is_never_adjudicated():
    """The whole point of "targeted": a registry concept with no conflict, no critic objection and
    no source disagreement produces NO reasons, so it is never referred and never costs a call."""
    assert adj.selection_reasons(_candidate(column_name="cif_id", definition="Customer CIF")) == ()
    assert adj.select_adjudication_targets(
        [_candidate(column_name="cif_id", definition="Customer CIF")]) == ()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"concept": None}, "unclassified_column"),
        ({"concept": ""}, "unclassified_column"),
        ({"concept": UNCLASSIFIED}, "unclassified_column"),
        ({"concept": "not_a_registry_member"}, "unclassified_column"),
        ({"shape_conflicts": ("measure_not_identifier",)}, "deterministic_shape_conflict"),
        ({"critic_disposition": "refuted"}, "critic_refuted"),
        ({"critic_reason_codes": ("llm_uncertain",)}, "critic_uncertain"),
        ({"source_entity": "account"}, "source_llm_conflict"),
    ],
)
def test_each_selection_rule_names_a_deterministic_signal(kwargs, expected):
    """Every rule fires on a COMPUTED fact — no model confidence anywhere in the selector."""
    assert expected in adj.selection_reasons(_candidate(**kwargs))


def test_selection_is_pure_and_repeatable():
    candidate = _candidate(concept=None, shape_conflicts=("measure_not_identifier",))
    first = adj.selection_reasons(candidate)
    assert first == adj.selection_reasons(candidate) == adj.selection_reasons(_candidate(
        concept=None, shape_conflicts=("measure_not_identifier",)))
    assert set(first) <= REASON_CODES


def test_critic_abstention_that_is_not_uncertainty_does_not_refer():
    """An abstain because the provider was DOWN says nothing about the data. Only the model's own
    `llm_uncertain` is uncertainty about this column."""
    assert adj.selection_reasons(_candidate(
        critic_disposition="abstained",
        critic_reason_codes=("llm_critic_unavailable",))) == ()


def test_the_counterparty_alias_is_not_a_source_conflict():
    """`counterparty_id` links entity `counterparty`, which DISPLAYS as `customer` (D12.1-revised).
    A source declaring `customer` therefore agrees; reading it as a conflict would refer every
    counterparty column in the catalog."""
    assert adj.selection_reasons(_candidate(
        column_name="counter_party_cif_id", concept="counterparty_id",
        definition="Counterparty CIF", source_entity="customer")) == ()
    assert "source_llm_conflict" in adj.selection_reasons(_candidate(
        column_name="counter_party_cif_id", concept="counterparty_id",
        definition="Counterparty CIF", source_entity="branch"))


def test_the_entity_alias_seam_normalizes_both_operands():
    """The alias seam is SYMMETRIC or it is not a seam.

    D12.1-revised leaves the two operands on opposite sides of the alias: the STORED entity keeps
    saying `counterparty` (fact keys are byte-stable) while a NEW classification canonicalizes to
    `customer_id`, whose entity link is `customer`. Normalizing only when the CONCEPT is the alias
    therefore read exactly the production shape — concept `customer_id`, source entity
    `counterparty` — as a contradiction, and every such column burned one of the twelve calls on a
    non-conflict. Both operands go through the entity-level seam, so neither direction conflicts;
    a genuinely different entity still does."""
    assert adj.selection_reasons(_candidate(
        column_name="counter_party_cif_id", concept="customer_id",
        definition="Counterparty CIF", source_entity="counterparty")) == ()
    assert adj.selection_reasons(_candidate(
        column_name="counter_party_cif_id", concept="counterparty_id",
        definition="Counterparty CIF", source_entity="customer")) == ()
    # A real disagreement survives the seam in both directions.
    assert "source_llm_conflict" in adj.selection_reasons(_candidate(
        column_name="owning_bank", concept="customer_id", definition="The owning bank",
        source_entity="bank"))
    assert "source_llm_conflict" in adj.selection_reasons(_candidate(
        column_name="owning_bank", concept="counterparty_id", definition="The owning bank",
        source_entity="bank"))


def test_the_entity_alias_map_is_derived_from_the_one_alias_table():
    """Not a second hardcoded map: the entity mapping is DERIVED from `_CANONICAL_ALIAS_TARGETS`,
    so an alias added (or retired) there cannot leave a stale entity pair behind."""
    from featuregen.overlay.upload import concepts as concepts_mod

    assert concepts_mod.canonical_entity("counterparty") == "customer"
    derived = {
        CONCEPT_REGISTRY[alias].entity_link: CONCEPT_REGISTRY[target].entity_link
        for alias, target in concepts_mod._CANONICAL_ALIAS_TARGETS.items()
        if CONCEPT_REGISTRY[alias].entity_link and CONCEPT_REGISTRY[target].entity_link
        and CONCEPT_REGISTRY[alias].entity_link != CONCEPT_REGISTRY[target].entity_link
    }
    assert concepts_mod._ENTITY_ALIAS_TARGETS == derived
    # A name nobody aliased is returned untouched, and a blank is not an entity.
    assert concepts_mod.canonical_entity("bank") == "bank"
    assert concepts_mod.canonical_entity(None) is None


def test_targets_sort_most_referred_first_so_a_capped_run_truncates_the_least_referred():
    one = _candidate(logical_ref="src::s.t.a", concept=None)
    three = _candidate(logical_ref="src::s.t.b", concept=None,
                       shape_conflicts=("measure_not_identifier",),
                       critic_disposition="refuted")
    targets = adj.select_adjudication_targets([one, three], max_targets=1)
    assert [t.logical_ref for t in targets] == ["src::s.t.b"]


# ── validation: closed, registry-checked, canonicalized ─────────────────────────────────────────


def test_an_adjudicated_concept_canonicalizes_through_the_alias_seam():
    """D12.1 at EVERY acceptance seam: an answer of `counterparty_id` lands as `customer_id`."""
    result = adj.adjudication_from_output(_answer(selected_concept="counterparty_id"))
    assert result is not None
    assert result.selected_concept == "customer_id"


def test_free_text_never_becomes_a_concept():
    assert adj.adjudication_from_output(
        _answer(selected_concept="the branch this account belongs to")) is None


def test_unclassified_is_a_valid_answer():
    result = adj.adjudication_from_output(_answer(selected_concept="unclassified"))
    assert result is not None and result.selected_concept == UNCLASSIFIED


def test_off_registry_alternatives_are_dropped_not_fatal():
    result = adj.adjudication_from_output(
        _answer(alternatives=["customer_id", "not_a_concept", "branch_id", "account_id",
                              "customer_id"]))
    assert result is not None
    # deduped, selection excluded, capped at three, every member a registry concept
    assert result.alternatives == ("customer_id", "account_id")
    assert all(a in CONCEPT_REGISTRY for a in result.alternatives)
    assert len(result.alternatives) <= adj.MAX_ALTERNATIVES


def test_off_vocabulary_codes_are_dropped():
    result = adj.adjudication_from_output(
        _answer(reason_codes=["name_only_signal", "the model felt unsure"],
                missing_context=["definition_missing", "vibes_missing"]))
    assert result is not None
    assert set(result.reason_codes) <= REASON_CODES
    assert set(result.missing_context) <= MISSING_CONTEXT_CODES
    assert "the model felt unsure" not in result.reason_codes


def test_an_off_vocabulary_confidence_band_invalidates_rather_than_being_reinterpreted():
    assert adj.adjudication_from_output(_answer(confidence_band="very high")) is None


def test_confidence_band_is_carried_but_never_changes_the_outcome():
    """Explanation, not authority: the SAME answer at every band produces the same disposition and
    the same corrected concept."""
    outcomes = set()
    for band in sorted(adj.CONFIDENCE_BANDS):
        result = adj.adjudication_from_output(_answer(confidence_band=band))
        assert result is not None and result.confidence_band == band
        outcomes.add(adj._outcome_for(_target(_candidate(concept="customer_id")), result))
    assert len(outcomes) == 1


# ── ontology gaps ───────────────────────────────────────────────────────────────────────────────


def test_a_gap_normalizes_to_the_registry_naming_shape():
    suggestion = gap.gap_from_output({
        "proposed_label": "Settlement Instruction", "parent_concept": "customer_id",
        "definition": "A standing instruction for settling a trade.",
        "aliases": ["SSI", "standing settlement instruction", "SSI"]})
    assert suggestion is not None
    assert suggestion.proposed_label == "settlement_instruction"
    assert suggestion.aliases == ("ssi", "standing_settlement_instruction")


def test_a_label_the_vocabulary_already_has_is_not_a_gap():
    assert gap.gap_from_output({"proposed_label": "customer_id", "definition": "x"}) is None
    assert gap.gap_from_output({"proposed_label": "unclassified", "definition": "x"}) is None


def test_an_unattachable_parent_drops_the_gap_not_the_adjudication():
    result = adj.adjudication_from_output(_answer(ontology_gap={
        "proposed_label": "settlement_instruction", "parent_concept": "invented_parent",
        "definition": "x"}))
    assert result is not None                 # the concept selection survives
    assert result.ontology_gap is None        # the unusable gap does not


def test_a_gap_never_grows_the_registry():
    before = dict(CONCEPT_REGISTRY)
    suggestion = gap.gap_from_output({"proposed_label": "settlement_instruction",
                                      "definition": "x"})
    assert suggestion is not None
    assert dict(CONCEPT_REGISTRY) == before
    assert suggestion.proposed_label not in CONCEPT_REGISTRY


def test_a_gap_content_hash_is_the_shared_canonical_scheme():
    """D1: `materialize_hash` (RFC 8785 JCS), never an inline scheme — so key order is not
    identity."""
    from featuregen.materialize.canonical import materialize_hash

    suggestion = gap.OntologyGapSuggestionV1("settlement_instruction", None, "x", ("ssi",))
    assert suggestion.content_hash() == materialize_hash(suggestion.as_dict())


# ── the subject/current pointer (migration 1046) ────────────────────────────────────────────────


def _seed_result(db, *, input_hash: str = "h1", value: str = "a") -> str:
    return record_structured_result(
        db, result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE,
        result_version=adj.SEMANTIC_ADJUDICATION_VERSION, input_content_hash=input_hash,
        output={"selected_concept": value}, producer_kind="llm_call",
        producer_ref="llm-1").structured_result_id


def test_pointer_is_cas_and_retry_idempotent(db):
    first = _seed_result(db)
    p1 = set_current_structured_result(
        db, subject_kind=adj.SUBJECT_COLUMN, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE, structured_result_id=first)
    assert p1.pointer_version == 1
    # A retry of the SAME revision does not churn the version (no phantom supersession).
    p2 = set_current_structured_result(
        db, subject_kind=adj.SUBJECT_COLUMN, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE, structured_result_id=first)
    assert p2 == p1
    second = _seed_result(db, input_hash="h2", value="b")
    p3 = set_current_structured_result(
        db, subject_kind=adj.SUBJECT_COLUMN, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE, structured_result_id=second)
    assert (p3.pointer_version, p3.structured_result_id) == (2, second)


def test_a_lost_cas_race_refuses_instead_of_overwriting(db):
    first = _seed_result(db)
    second = _seed_result(db, input_hash="h2", value="b")
    set_current_structured_result(
        db, subject_kind=adj.SUBJECT_COLUMN, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE, structured_result_id=first)
    # A writer holding the pre-insert snapshot (version 0) must NOT be able to clobber version 1.
    with pytest.raises(StructuredResultPointerConflict):
        set_current_structured_result(
            db, subject_kind=adj.SUBJECT_COLUMN, subject_ref=_REF,
            result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE, structured_result_id=second,
            expected_pointer_version=0)
    still = load_current_structured_result(
        db, subject_kind=adj.SUBJECT_COLUMN, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE)
    assert still.structured_result_id == first


def test_withdrawal_supersedes_without_deleting_history(db):
    first = _seed_result(db)
    set_current_structured_result(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE, structured_result_id=first)
    assert [p.subject_ref for p in list_current_structured_results(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE)] == [_REF]
    withdrawn = withdraw_current_structured_result(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE)
    assert withdrawn.lifecycle == "withdrawn"
    assert list_current_structured_results(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE) == ()
    # The row and its revision reference survive — supersession is information.
    row = load_current_structured_result(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE)
    assert row.structured_result_id == first
    # Withdrawing twice is a no-op, not an error.
    assert withdraw_current_structured_result(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE) is None


def test_the_gap_discovery_read_never_touches_output_json(db):
    """The pointer exists precisely so the review surface answers "which columns suggest a gap?"
    from pointer rows alone."""
    first = _seed_result(db)
    set_current_structured_result(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE, structured_result_id=first)
    pointers = list_current_structured_results(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE, subject_refs=[_REF])
    assert len(pointers) == 1
    assert not hasattr(pointers[0], "output")


# ── the audited call, replay, persistence and provenance ────────────────────────────────────────


def _actor():
    return mint_test_identity(subject="user:admin", role_claims=("platform-admin",))


def test_the_adjudication_persists_as_a_versioned_structured_result_with_llm_provenance(db):
    results = adj.adjudicate_targets(db, _client(), [_target()], catalog_revision="rev1",
                                     actor=_actor())
    assert len(results) == 1
    result = results[0]
    assert result.outcome is adj.AdjudicationOutcome.SELECTED
    assert result.structured_result_id and result.llm_call_ref
    stored = load_structured_result(db, result.structured_result_id)
    assert stored.result_type == "semantic_adjudication"
    assert stored.result_version == adj.SEMANTIC_ADJUDICATION_VERSION == 2
    assert stored.output["selected_concept"] == "branch_id"
    assert stored.output["selection_reasons"] == ["unclassified_column"]
    # llm_call linkage rides the EXISTING structured_result_provenance mechanism.
    provenance = db.execute(
        "SELECT producer_kind, producer_ref FROM structured_result_provenance "
        "WHERE structured_result_id=%s", (result.structured_result_id,)).fetchall()
    assert provenance == [("llm_call", result.llm_call_ref)]


def test_an_identical_question_replays_without_a_provider_call(db):
    client = _client()
    adj.adjudicate_targets(db, client, [_target()], catalog_revision="rev1", actor=_actor())
    calls = db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s",
                       (adj.SEMANTIC_ADJUDICATION_RUN_ID,)).fetchone()[0]
    again = adj.adjudicate_targets(db, client, [_target()], catalog_revision="rev1",
                                   actor=_actor())
    assert again[0].outcome is adj.AdjudicationOutcome.SELECTED
    assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s",
                      (adj.SEMANTIC_ADJUDICATION_RUN_ID,)).fetchone()[0] == calls


def test_the_call_cap_reports_not_attempted_rather_than_dropping_items(db):
    targets = [_target(_candidate(logical_ref=f"src::s.t.c{i}", concept=None)) for i in range(3)]
    results = adj.adjudicate_targets(
        db, _client(), targets, catalog_revision="rev1", actor=_actor(),
        bounds=adj.AdjudicationBounds(max_provider_calls=1, max_targets=10))
    assert len(results) == len(targets)          # every item accounted for
    counts = adj.outcome_counts(results)
    assert counts["not_attempted"] == 2
    assert [r.skipped_reason for r in results if r.outcome is
            adj.AdjudicationOutcome.NOT_ATTEMPTED] == ["call_cap_reached"] * 2


def test_no_client_is_not_attempted_never_a_guess(db):
    results = adj.adjudicate_targets(db, None, [_target()], catalog_revision="rev1")
    assert results[0].outcome is adj.AdjudicationOutcome.NOT_ATTEMPTED
    assert results[0].adjudication is None
    assert results[0].skipped_reason == "adjudicator_unavailable"


def test_an_output_that_fails_the_code_side_gate_is_invalid_not_silently_accepted(db):
    results = adj.adjudicate_targets(
        db, _client(_answer(selected_concept="a branch, probably")), [_target()],
        catalog_revision="rev1", actor=_actor())
    assert results[0].outcome is adj.AdjudicationOutcome.INVALID
    assert results[0].adjudication is None
    # Nothing invalid is ever stored or pointed at.
    assert db.execute("SELECT count(*) FROM structured_result WHERE result_type=%s",
                      (adj.SEMANTIC_ADJUDICATION_RESULT_TYPE,)).fetchone()[0] == 0
    assert load_current_structured_result(
        db, subject_kind=adj.SUBJECT_COLUMN, subject_ref=_REF,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE) is None


def test_an_upheld_concept_is_unchanged_and_a_gap_answer_is_gap_suggested(db):
    upheld = adj.adjudicate_targets(
        db, _client(_answer(selected_concept="customer_id", alternatives=[])),
        [_target(_candidate(concept="customer_id"), reasons=("critic_uncertain",))],
        catalog_revision="rev1", actor=_actor())
    assert upheld[0].outcome is adj.AdjudicationOutcome.UNCHANGED
    assert upheld[0].corrected_concept is None          # nothing to write

    gapped = adj.adjudicate_targets(
        db, _client(_answer(selected_concept="unclassified", alternatives=[], ontology_gap={
            "proposed_label": "settlement_instruction", "parent_concept": "customer_id",
            "definition": "A standing settlement instruction."})),
        [_target(_candidate(logical_ref="src::s.t.ssi", concept=None))],
        catalog_revision="rev1", actor=_actor())
    assert gapped[0].outcome is adj.AdjudicationOutcome.GAP_SUGGESTED
    assert gapped[0].corrected_concept is None          # a gap never proposes a concept
    pointers = list_current_structured_results(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE)
    assert [p.subject_ref for p in pointers] == ["src::s.t.ssi"]


def test_a_gap_suggestion_never_suppresses_a_selected_correction(db):
    """The display RANKING is display-only, which is only true if nothing about persistence reads
    it. It did: `_OUTCOME_ORDER` puts `gap_suggested` above `selected` and `corrected_concept`
    returned non-None only for a `selected` HEADLINE, so an answer that both named a better concept
    AND flagged a vocabulary gap wrote ZERO concept evidence — the correction was silently
    swallowed by the label. The two verdicts are now independent: one headline for the reader, one
    concept verdict for the writer."""
    results = adj.adjudicate_targets(
        db, _client(_answer(selected_concept="branch_id", alternatives=[], ontology_gap={
            "proposed_label": "branch_description", "parent_concept": "branch_id",
            "definition": "A human-readable name for a branch."})),
        [_target(_candidate(logical_ref="src::s.t.both", concept=None))],
        catalog_revision="rev1", actor=_actor())
    result = results[0]
    assert result.outcome is adj.AdjudicationOutcome.GAP_SUGGESTED     # headline: the rarer event
    assert result.concept_verdict is adj.AdjudicationOutcome.SELECTED  # what the WRITER reads
    assert result.corrected_concept == "branch_id"
    # Both are true of this one item, so the detail says both. The counts OVERLAP by design.
    counts = adj.outcome_counts(results)
    assert counts["selected"] == 1 and counts["gap_suggested"] == 1
    # ...and the gap is still recorded and pointed at, not traded away for the correction.
    assert [p.subject_ref for p in list_current_structured_results(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE)] == ["src::s.t.both"]


def test_a_gap_only_answer_still_corrects_nothing(db):
    """The other side of the decoupling: a gap beside an `unclassified` (or upheld) verdict must
    STILL write no concept evidence. Decoupling the write path from the ranking must not turn every
    gap into a correction."""
    results = adj.adjudicate_targets(
        db, _client(_answer(selected_concept="unclassified", alternatives=[], ontology_gap={
            "proposed_label": "settlement_instruction", "definition": "A standing instruction."})),
        [_target(_candidate(logical_ref="src::s.t.gaponly", concept=None))],
        catalog_revision="rev1", actor=_actor())
    assert results[0].outcome is adj.AdjudicationOutcome.GAP_SUGGESTED
    assert results[0].concept_verdict is adj.AdjudicationOutcome.UNCLASSIFIED
    assert results[0].corrected_concept is None
    counts = adj.outcome_counts(results)
    assert counts["unclassified"] == 1 and counts["gap_suggested"] == 1
    assert counts["selected"] == 0


def test_a_rederivation_without_a_gap_withdraws_the_previous_suggestion(db):
    target = _target(_candidate(logical_ref="src::s.t.ssi", concept=None))
    adj.adjudicate_targets(
        db, _client(_answer(selected_concept="unclassified", alternatives=[], ontology_gap={
            "proposed_label": "settlement_instruction", "definition": "x"})),
        [target], catalog_revision="rev1", actor=_actor())
    adj.adjudicate_targets(
        db, _client(_answer(selected_concept="branch_id", alternatives=[])), [target],
        catalog_revision="rev2", actor=_actor())
    assert list_current_structured_results(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE) == ()


def test_the_current_pointer_read_revalidates_and_serves_the_latest(db):
    target = _target(_candidate(logical_ref="src::s.t.x", concept=None))
    adj.adjudicate_targets(db, _client(_answer(selected_concept="branch_id", alternatives=[])),
                           [target], catalog_revision="rev1", actor=_actor())
    adjudication, result_id = adj.load_current_adjudication(db, "src::s.t.x")
    assert adjudication is not None and adjudication.selected_concept == "branch_id"
    adj.adjudicate_targets(db, _client(_answer(selected_concept="customer_id", alternatives=[])),
                           [target], catalog_revision="rev2", actor=_actor())
    later, later_id = adj.load_current_adjudication(db, "src::s.t.x")
    assert later.selected_concept == "customer_id"
    assert later_id != result_id


# ── D10: the schema is REAL and registered, and every payload key is classified ─────────────────


def test_the_versioned_output_schema_has_a_real_registered_body(db):
    register_enrichment_schemas(db)
    schema = _require_schema(db, DocumentSchemaRegistry(db),
                             adj.SEMANTIC_ADJUDICATION_SCHEMA_ID,
                             adj.SEMANTIC_ADJUDICATION_SCHEMA_VERSION)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"selected_concept", "alternatives", "confidence_band",
                                       "reason_codes", "missing_context"}
    # `ontology_gap` is OPTIONAL — requiring it would manufacture a gap per call.
    assert "ontology_gap" not in schema["required"]
    assert schema["properties"]["confidence_band"]["enum"] == ["high", "medium", "low"]


def test_requesting_an_unregistered_version_raises_rather_than_dispatching_unenforced(db):
    from featuregen.overlay.upload.enrich_llm import SchemaUnregisteredError

    with pytest.raises(SchemaUnregisteredError):
        _require_schema(db, DocumentSchemaRegistry(db), adj.SEMANTIC_ADJUDICATION_SCHEMA_ID, 99)


def test_every_adjudication_payload_key_is_egress_classified():
    """Golden egress test: the whole payload passes the fail-closed classification gate, and the
    frozen key list matches what the builder actually emits."""
    payload = adj.adjudication_payload(_target(context={"term_name": "Branch description"}))
    redacted, _spans, _audits, _version = _redact_free_text_meta(payload)
    assert redacted is not None, "an unclassified key fails the whole payload closed"
    assert set(adj.ADJUDICATION_PAYLOAD_KEYS) <= set(payload)


def test_an_unclassified_payload_key_still_fails_closed():
    payload = adj.adjudication_payload(_target())
    payload["an_unclassified_key"] = "anything"
    redacted, _spans, _audits, _version = _redact_free_text_meta(payload)
    assert redacted is None


def test_the_payload_carries_the_current_concept_and_the_closed_selection_reasons():
    payload = adj.adjudication_payload(_target(
        _candidate(concept="customer_id"), reasons=("critic_refuted", "unclassified_column")))
    assert payload["proposed_concept"] == "customer_id"
    assert payload["selection_reasons"] == ["critic_refuted", "unclassified_column"]
    assert set(payload["selection_reasons"]) <= REASON_CODES
    assert set(payload["missing_context"]) == MISSING_CONTEXT_CODES
    # An unclassified column tells the model so explicitly rather than omitting the key.
    assert adj.adjudication_payload(_target())["proposed_concept"] == UNCLASSIFIED


def test_the_replay_identity_covers_the_vocabulary_the_model_was_shown(monkeypatch):
    payload = adj.adjudication_payload(_target())
    before = adj.adjudication_input_hash(payload, "rev1")
    monkeypatch.setattr(adj, "_vocabulary_fingerprint", lambda: "a-different-vocabulary")
    assert adj.adjudication_input_hash(payload, "rev1") != before


# ── D12.3: outcomes are stage DETAIL, states stay in the 0996 vocabulary ────────────────────────


def test_the_six_outcomes_are_not_stage_states():
    outcomes = {o.value for o in adj.AdjudicationOutcome}
    assert outcomes == {"selected", "unchanged", "unclassified", "gap_suggested", "invalid",
                        "not_attempted"}
    assert outcomes.isdisjoint(STAGE_STATES)


def test_the_stage_is_registered_between_pass_a_and_graph_persistence():
    stages = list(CANONICAL_STAGES)
    assert stages.index("enrich_unit") < stages.index("semantic_adjudication")
    assert stages.index("semantic_adjudication") < stages.index("graph_persistence")


def test_outcome_counts_always_carry_all_six_keys():
    counts = adj.outcome_counts(())
    assert set(counts) == {o.value for o in adj.AdjudicationOutcome}
    assert set(counts.values()) == {0}


# ── wired through a real ingest: the stage, the correction, and the read surface ────────────────


def _ingest_actor():
    from featuregen.contracts.envelopes import IdentityEnvelope

    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_overlay_config():
    from datetime import timedelta

    from featuregen.overlay.config import OverlayConfig, register_overlay_config

    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


class _UnclassifiedThenAdjudicated:
    """Pass A abstains (``unclassified``); the adjudicator names a registry concept. The exact
    live shape the stage exists for."""

    def __init__(self, selected: str = "branch_id", gap: dict | None = None,
                 answer: dict | None = None):
        answer = answer or _answer(selected_concept=selected, alternatives=[])
        if gap is not None:
            answer["ontology_gap"] = gap
        self._inner = FakeLLM(script={
            "overlay.enrich.concept": FakeResponse(output={"concept": "unclassified"}),
            "overlay.enrich.concept_batch": FakeResponse(output={"results": []}),
            "overlay.enrich.definition": FakeResponse(output={"definition": "drafted"}),
            "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
            "overlay.enrich.synonyms": FakeResponse(output={"synonyms": "alias"}),
            "overlay.enrich.unit": FakeResponse(output={"unit": "count"}),
            "overlay.enrich.summary": FakeResponse(output={"summary": "a column"}),
            adj.SEMANTIC_ADJUDICATION_TASK: FakeResponse(output=answer),
        })

    def call(self, request):
        return self._inner.call(request)


def _ingest_one(db, client, monkeypatch, *, column: str = "sol_desc"):
    from datetime import UTC, datetime

    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.stage_report import StageRecorder

    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "single")
    monkeypatch.setenv("OVERLAY_ENRICH_DEFINITION_MODE", "single")
    monkeypatch.setenv("OVERLAY_ENRICH_DOMAIN_MODE", "single")
    monkeypatch.setenv("OVERLAY_ENRICH_SYNONYMS_MODE", "single")
    monkeypatch.setenv("OVERLAY_ENRICH_UNIT_MODE", "single")
    monkeypatch.setenv("OVERLAY_ENRICH_SUMMARY_MODE", "single")
    _seal_overlay_config()
    rec = StageRecorder()
    result = ingest_upload(
        db, "deposits", [CanonicalRow("deposits", "accounts", column, "varchar(255)")],
        actor=_ingest_actor(), now=datetime(2026, 7, 16, tzinfo=UTC), client=client,
        stage_recorder=rec)
    return result, rec


# ── the GLOSSARY ingest: the only path that can write field evidence ────────────────────────────
# A technical upload has no `source_snapshot_id` (`ingest.py`: `snapshot_id = mint_id("ing") if
# is_glossary else None`), so its corrections reach `graph_node` with no evidence rows at all —
# Pass A's pre-existing behaviour, documented at that seam. Proving "graph_node AND field_evidence
# together" therefore REQUIRES a glossary upload.
_GLOSSARY_SOURCE = "cibgloss"
_GLOSSARY_CSV = (
    "physical_name,business_term,description_business_definition,data_domain\n"
    "DPL_EIB_COMPLIANCE.BO_CIB_CUSTOMER.SOL_DESC,Sol Desc,"
    "The description of the branch that owns the relationship.,Party\n"
)


def _glossary_ref():
    from featuregen.overlay.upload.object_ref import normalize_ref

    return normalize_ref(_GLOSSARY_SOURCE, "DPL_EIB_COMPLIANCE", "BO_CIB_CUSTOMER", "SOL_DESC")


def _ingest_glossary(db, client, monkeypatch):
    from datetime import UTC, datetime

    from featuregen.overlay.upload.glossary_reader import read_glossary
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.stage_report import StageRecorder

    for var in ("CONCEPT", "DEFINITION", "DOMAIN", "SYNONYMS", "UNIT", "SUMMARY"):
        monkeypatch.setenv(f"OVERLAY_ENRICH_{var}_MODE", "single")
    _seal_overlay_config()
    upload = read_glossary(_GLOSSARY_CSV, source=_GLOSSARY_SOURCE)
    rec = StageRecorder()
    result = ingest_upload(
        db, _GLOSSARY_SOURCE, upload.rows, actor=_ingest_actor(),
        now=datetime(2026, 7, 16, tzinfo=UTC), client=client, glossary=upload,
        stage_recorder=rec)
    return result, rec


def _concept_evidence(db):
    from featuregen.overlay.field_evidence import read_active_field_evidence

    return [e for e in read_active_field_evidence(db, _glossary_ref(), "concept")
            if e.producer == "llm"]


def _stage(rec, name):
    return next(r for r in rec.reports if r.stage == name)


def test_the_ingest_stage_reports_outcomes_in_its_detail_and_a_state_from_the_0996_vocabulary(
        db, monkeypatch):
    result, rec = _ingest_one(db, _UnclassifiedThenAdjudicated(), monkeypatch)
    assert result.status == "ingested"
    stage = _stage(rec, "semantic_adjudication")
    assert stage.state in STAGE_STATES
    assert stage.detail["targets"] == 1
    assert stage.detail["selected"] == 1
    # All six outcomes are always present — a zero is information.
    assert {o.value for o in adj.AdjudicationOutcome} <= set(stage.detail)


def test_a_selected_correction_reaches_graph_node_and_field_evidence_together(db, monkeypatch):
    """The correction is applied BEFORE `build_graph`, so the display value and the evidence that
    justifies it can never disagree by a run — and the evidence row is ASSERTED here, not implied
    by the test's name (the earlier version checked only `graph_node`, on a technical upload that
    writes no evidence at all)."""
    _ingest_glossary(db, _UnclassifiedThenAdjudicated(selected="branch_id"), monkeypatch)
    node = db.execute(
        "SELECT concept FROM graph_node WHERE catalog_source=%s "
        "AND object_ref='public.bo_cib_customer.sol_desc'",
        (_GLOSSARY_SOURCE,)).fetchone()
    assert node == ("branch_id",)
    evidence = _concept_evidence(db)
    assert [(e.proposed_value, e.strength) for e in evidence] == [("branch_id", "proposed")]


def test_a_gap_beside_a_correction_writes_the_evidence_and_keeps_the_gap(db, monkeypatch):
    """Wired end to end: the answer names a better concept AND a missing word. Both land — the
    correction as `llm/proposed` concept evidence on `graph_node`'s column, the gap as a current
    pointer — and the stage detail counts BOTH rather than letting the headline hide one."""
    _result, rec = _ingest_glossary(
        db, _UnclassifiedThenAdjudicated(
            selected="branch_id",
            gap={"proposed_label": "branch_description", "parent_concept": "branch_id",
                 "definition": "A human-readable name for a branch."}),
        monkeypatch)
    assert [(e.proposed_value, e.strength) for e in _concept_evidence(db)] == \
        [("branch_id", "proposed")]
    node = db.execute(
        "SELECT concept FROM graph_node WHERE catalog_source=%s "
        "AND object_ref='public.bo_cib_customer.sol_desc'",
        (_GLOSSARY_SOURCE,)).fetchone()
    assert node == ("branch_id",)
    assert [p.subject_ref for p in list_current_structured_results(
        db, subject_kind=adj.SUBJECT_ONTOLOGY_GAP,
        result_type=adj.SEMANTIC_ADJUDICATION_RESULT_TYPE)] == [_glossary_ref()]
    detail = _stage(rec, "semantic_adjudication").detail
    assert detail["selected"] == 1
    assert detail["gap_suggested"] == 1
    assert detail["gaps"] == 1
    assert detail["evidence_written"] == 1


def test_a_gap_without_a_correction_writes_no_concept_evidence(db, monkeypatch):
    """Unchanged by the decoupling: an honest abstention plus a gap proposes no concept, so there
    is nothing to write (C3) — and the stage still says so out loud."""
    _result, rec = _ingest_glossary(
        db, _UnclassifiedThenAdjudicated(answer={
            "selected_concept": "unclassified", "alternatives": [], "confidence_band": "low",
            "reason_codes": ["name_only_signal"], "missing_context": ["definition_missing"],
            "ontology_gap": {"proposed_label": "branch_description", "parent_concept": "branch_id",
                             "definition": "A human-readable name for a branch."}}),
        monkeypatch)
    assert _concept_evidence(db) == []
    detail = _stage(rec, "semantic_adjudication").detail
    assert detail["selected"] == 0
    assert detail["unclassified"] == 1
    assert detail["gap_suggested"] == 1
    assert detail["evidence_written"] == 0


def test_an_unclear_column_with_no_client_is_skipped_not_guessed(db, monkeypatch):
    _result, rec = _ingest_one(db, None, monkeypatch)
    assert _stage(rec, "semantic_adjudication").state == "skipped_no_client"


def test_a_clear_run_records_not_applicable_never_a_failure(db, monkeypatch):
    class _Clear(_UnclassifiedThenAdjudicated):
        def __init__(self):
            super().__init__()
            self._inner = FakeLLM(script={
                "overlay.enrich.concept": FakeResponse(output={"concept": "branch_id"}),
                "overlay.enrich.definition": FakeResponse(output={"definition": "drafted"}),
                "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
                "overlay.enrich.synonyms": FakeResponse(output={"synonyms": "alias"}),
                "overlay.enrich.unit": FakeResponse(output={"unit": "count"}),
                "overlay.enrich.summary": FakeResponse(output={"summary": "a column"}),
            })

    _result, rec = _ingest_one(db, _Clear(), monkeypatch, column="branch_id")
    stage = _stage(rec, "semantic_adjudication")
    assert (stage.state, stage.reason_code) == ("not_applicable", "no_unclear_columns")
    assert stage.detail["targets"] == 0


def test_the_asset_detail_section_serves_alternatives_reasons_context_and_the_gap(db, monkeypatch):
    from featuregen.overlay.upload.asset_detail import build_asset_detail

    client = _UnclassifiedThenAdjudicated(answer={
        "selected_concept": "unclassified", "alternatives": ["branch_id", "customer_id"],
        "confidence_band": "low", "reason_codes": ["name_only_signal", "insufficient_context"],
        "missing_context": ["definition_missing"],
        "ontology_gap": {"proposed_label": "branch_description", "parent_concept": "branch_id",
                         "definition": "A human-readable name for a branch.",
                         "aliases": ["sol desc"]}})
    _ingest_one(db, client, monkeypatch)
    body = build_asset_detail(db, source="deposits", object_ref="public.accounts.sol_desc",
                              roles=("platform-admin",))
    section = body["semantic_adjudication"]
    assert section["status"] == "available"
    assert section["selected_concept"] == "unclassified"
    assert section["alternatives"] == ["branch_id", "customer_id"]
    assert section["reason_codes"] == ["name_only_signal", "insufficient_context"]
    assert section["missing_context"] == ["definition_missing"]
    assert section["confidence_band"] == "low"
    assert section["ontology_gap"]["proposed_label"] == "branch_description"
    # The section rides the dossier's ONE transaction, so the consistency token covers it.
    assert body["consistency_token"]
    assert "semantic_adjudication" in body["included_sections"]


def test_a_table_anchor_gets_an_honest_empty_section(db, monkeypatch):
    from featuregen.overlay.upload.asset_detail import build_asset_detail

    _ingest_one(db, _UnclassifiedThenAdjudicated(), monkeypatch)
    body = build_asset_detail(db, source="deposits", object_ref="public.accounts",
                              roles=("platform-admin",))
    assert body["semantic_adjudication"]["status"] == "absent"


def test_dispatches_carry_the_run_stage_and_column_subject_attribution():
    """C5-T5. Attribution rides the PRE-DISPATCH audit gate, so this is also what makes the stage
    fail CLOSED — no egress — when the audit store is unavailable (the whole-ingest proof of that
    lives in test_dispatch_gate.py, which drives a real durable run)."""
    context = adj._dispatch_context(_target(), "ingrun_1")
    assert context.ingestion_run_id == "ingrun_1"
    assert context.stage == adj.SEMANTIC_ADJUDICATION_STAGE
    assert context.stage in CANONICAL_STAGES        # one name for the stage and its dispatches
    assert context.subjects == ({"catalog_source": "cib", "object_ref": None,
                                 "logical_ref": _REF, "field_names": ["concept"]},)
    # A direct caller with no run dispatches unattributed, exactly like every other seam.
    assert adj._dispatch_context(_target(), None) is None
