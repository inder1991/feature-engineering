"""Release-B Task 7 — the closed selection-refusal vocabulary across its three real consumers.

The plan's item: "add closed refusal codes for population undeclared, source ambiguous, binding
missing, authority insufficient, temporal model unknown, historical current-only, SCD overlap and
snapshot tie across all actual consumers: `analysis.intent.UNRESOLVED_CODES`, the wire schema,
`analysis.clarify`, `data_agent.learning.GAP_CODES` and `REFUSAL_TO_GAP`. Keep SCD overlap a data
quality refusal, not an ontology-learning gap."

The two judgements this suite pins:

1. The WIRE SCHEMA is deliberately NOT widened. The schema enum is derived from the model's own
   vocabulary, and functional rule 1 says the LLM never attests uniqueness, overlap, population
   completeness or row-history correctness — so a model must not be able to claim
   `TEMPORAL_SCD_OVERLAP`. Keeping it narrow also leaves the request body byte-identical: no prompt
   or schema version bump, no replay churn (D10).
2. SCD OVERLAP ROUTES TO NO LEARNING GAP, honouring the existing deliberate exclusion of
   `ATTRIBUTION_OVERLAPPING_RECORDS`. It still refuses and still renders as a clarification — a user
   is told why their question stopped — but nobody is waiting to DECIDE anything, so recording it
   would put an uncloseable item in a reviewer queue.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.clarify import ClarificationError, _build, apply_answer
from featuregen.analysis.intent import (
    INTENT_SCHEMA,
    MODEL_UNRESOLVED_CODES,
    UNRESOLVED_CODES,
    IntentCandidates,
    validate_intent,
)
from featuregen.contracts import SchemaValidationError
from featuregen.data_agent.learning import (
    CHOICE_VOCABULARIES,
    GAP_CODES,
    REFUSAL_TO_GAP,
    RequiredAction,
    record_refusal,
)
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import (
    SELECTION_AUTHORITY_INSUFFICIENT,
    SELECTION_BINDING_MISSING,
    SELECTION_POPULATION_UNDECLARED,
    SELECTION_REFUSAL_CODES,
    SELECTION_SOURCE_AMBIGUOUS,
    TEMPORAL_HISTORICAL_CURRENT_ONLY,
    TEMPORAL_MODEL_UNKNOWN,
    TEMPORAL_SCD_OVERLAP,
    TEMPORAL_SNAPSHOT_TIE,
)

_TABLE = "bank::public.customer_master"
_COL = "bank::public.customer_master.cif_id"
_AS_OF = "bank::public.customer_master.snapshot_date"

CANDIDATES = IntentCandidates(
    column_refs=frozenset({_COL, _AS_OF}),
    table_refs=frozenset({_TABLE, "bank::public.customer_ods"}),
    grain_refs=frozenset({_COL}),
    as_of_refs=frozenset({_AS_OF}),
)


def _valid_output(**over) -> dict:
    out = {
        "entity": "customer", "entity_ref": _COL, "base_table_ref": _TABLE,
        "measure": {"op": "count", "logical_ref": ""}, "windows": [], "dimensions": [],
        "comparison": "", "unresolved": [],
    }
    out.update(over)
    return out


# ── one vocabulary, two audiences ───────────────────────────────────────────────────────────────

def test_every_refusal_code_is_clarifiable_and_none_is_model_answerable():
    assert SELECTION_REFUSAL_CODES <= UNRESOLVED_CODES
    assert not (SELECTION_REFUSAL_CODES & MODEL_UNRESOLVED_CODES)
    assert UNRESOLVED_CODES == MODEL_UNRESOLVED_CODES | SELECTION_REFUSAL_CODES


def test_the_wire_schema_admits_only_the_models_own_vocabulary():
    """A widened enum would INVITE the model to assert a condition it cannot observe, and would
    change the request body — a schema change is a real version bump, never a silent widening."""
    enum = INTENT_SCHEMA["properties"]["unresolved"]["items"]["enum"]
    assert enum == sorted(MODEL_UNRESOLVED_CODES)
    assert not (set(enum) & SELECTION_REFUSAL_CODES)


@pytest.mark.parametrize("code", sorted(SELECTION_REFUSAL_CODES))
def test_a_model_claiming_a_selection_condition_is_refused(code):
    with pytest.raises(SchemaValidationError, match="not actionable"):
        validate_intent(_valid_output(unresolved=[code]), CANDIDATES)


# ── clarify: every refusal becomes a question (rule 10) ─────────────────────────────────────────

@pytest.mark.parametrize("code", sorted(SELECTION_REFUSAL_CODES))
def test_every_refusal_renders_as_a_clarification(code):
    clarification = _build(code, CANDIDATES)
    assert clarification.code == code
    assert clarification.question.strip()
    # No-blocked framing: a question, never a failure string.
    assert not any(word in clarification.question.lower()
                   for word in ("error", "failed", "invalid", "blocked")), clarification.question


@pytest.mark.parametrize("code", sorted(SELECTION_REFUSAL_CODES - {TEMPORAL_SCD_OVERLAP}))
def test_a_refusal_that_a_person_can_answer_offers_bounded_options(code):
    """Options stay inside the candidate set the plan was grounded against, or a CLOSED server
    vocabulary — a clarification may never introduce a ref the plan never saw."""
    clarification = _build(code, CANDIDATES)
    assert clarification.options
    allowed = CANDIDATES.column_refs | CANDIDATES.table_refs | {
        m.value for m in TemporalStorageModel}
    assert {o.value for o in clarification.options} <= allowed


def test_scd_overlap_is_shown_but_offers_no_choice_that_would_paper_over_bad_data():
    clarification = _build(TEMPORAL_SCD_OVERLAP, CANDIDATES)
    assert clarification.options == ()
    assert "data-quality" in clarification.question


# ── ONE code, TWO situations: the ambiguity question tells the truth about which ────────────────
#
# `source_selector` reuses SELECTION_SOURCE_AMBIGUOUS for "NOTHING is eligible" because the
# vocabulary is closed at eight and the gap/action (DATASET_SOURCE_UNRESOLVED /
# DECLARE_SERVING_POLICY) are right for both. The QUESTION was not: it said "more than one dataset
# is equally eligible" and offered the candidates that had each already failed.


def _refusal(*dispositions):
    from featuregen.overlay.upload.source_selection import CandidateDecisionV1, CandidateDisposition
    from featuregen.overlay.upload.source_selector import SelectionRefusalV1

    refs = (_TABLE, "bank::public.customer_ods")
    return SelectionRefusalV1(
        code=SELECTION_SOURCE_AMBIGUOUS, subject_refs=refs, detail="x",
        considered_candidates=tuple(
            CandidateDecisionV1(dataset_ref=ref, dataset_profile_hash="h",
                                binding_revision_id=None,
                                disposition=CandidateDisposition(disposition))
            for ref, disposition in zip(refs, dispositions, strict=True)))


def test_the_ZERO_candidate_question_says_so_and_offers_no_failed_candidates():
    """Both candidates were REJECTED — nothing has a profile and an address. Offering them as
    choices invites an answer the selector will refuse again, and tells the reader the system found
    something it did not."""
    clarification = _build(SELECTION_SOURCE_AMBIGUOUS, CANDIDATES,
                           _refusal("rejected", "rejected"))
    assert clarification.question.startswith("No dataset is eligible to serve this need")
    assert "serving policy" in clarification.question
    assert clarification.options == ()


def test_the_MANY_candidate_question_still_asks_which_one():
    clarification = _build(SELECTION_SOURCE_AMBIGUOUS, CANDIDATES, _refusal("tied", "tied"))
    assert clarification.question.startswith("More than one dataset is equally eligible")
    assert {o.value for o in clarification.options} == set(CANDIDATES.table_refs)


def test_a_caller_with_only_the_CODE_gets_the_common_case():
    """The vocabulary is the contract; the payload is an enrichment. A caller holding codes alone
    still gets a question, and it is the one the code's common case describes."""
    assert _build(SELECTION_SOURCE_AMBIGUOUS, CANDIDATES).question == _build(
        SELECTION_SOURCE_AMBIGUOUS, CANDIDATES, _refusal("tied", "tied")).question


def test_both_situations_stay_ONE_code_and_ONE_action():
    """The wording branches; the vocabulary does not. Both are DATASET_SOURCE_UNRESOLVED, and the
    action for both is to declare a serving policy."""
    assert REFUSAL_TO_GAP[SELECTION_SOURCE_AMBIGUOUS] == (
        "DATASET_SOURCE_UNRESOLVED", RequiredAction.DECLARE_SERVING_POLICY)
    for refusal in (_refusal("rejected", "rejected"), _refusal("tied", "tied")):
        assert _build(SELECTION_SOURCE_AMBIGUOUS, CANDIDATES, refusal).code == \
            SELECTION_SOURCE_AMBIGUOUS


def test_the_population_refusal_asks_exactly_what_the_population_abstention_asks():
    """Same decision reached from the selector instead of the model — so the same question and the
    same options, with only the code differing so the answer routes back correctly."""
    from_model = _build("population", CANDIDATES)
    from_selector = _build(SELECTION_POPULATION_UNDECLARED, CANDIDATES)
    assert from_selector.question == from_model.question
    assert from_selector.options == from_model.options
    assert from_selector.code == SELECTION_POPULATION_UNDECLARED


def test_the_population_refusal_folds_through_the_existing_branch(monkeypatch):
    from featuregen.analysis.plan import AnalysisPlanV1, Measure

    plan = AnalysisPlanV1(question="q", entity="customer", entity_ref=_COL,
                          base_table_ref=_TABLE, measure=Measure(op="count", logical_ref=""))
    folded = apply_answer(plan, SELECTION_POPULATION_UNDECLARED, (_COL,), CANDIDATES)
    assert folded.population_table_ref == "bank::public.customer_master"
    assert folded.population_key_ref == _COL


@pytest.mark.parametrize("code",
                         sorted(SELECTION_REFUSAL_CODES - {SELECTION_POPULATION_UNDECLARED}))
def test_the_other_refusals_are_applied_by_the_selector_not_by_editing_the_plan(code):
    """The plan contract has no per-need source slot; inventing one here would be a second,
    ungoverned place where a source gets chosen."""
    from featuregen.analysis.plan import AnalysisPlanV1, Measure

    plan = AnalysisPlanV1(question="q", entity="customer", entity_ref=_COL,
                          base_table_ref=_TABLE, measure=Measure(op="count", logical_ref=""))
    with pytest.raises(ClarificationError, match="selects its sources and rows"):
        apply_answer(plan, code, (_COL,), CANDIDATES)


# ── the order the questions are asked in is a DECISION, not an accident ─────────────────────────

def test_the_question_order_is_a_total_order_no_two_codes_can_swap():
    """`clarifications_for` sorts a SET, and a set of strings iterates in an order PYTHONHASHSEED
    decides. Any two codes sharing a rank therefore swap places between processes — the same
    question order is not even reproducible for one user twice. The rank alone is not enough to
    prevent it (three codes sat at rank 0), so the key carries the code NAME as a tiebreak and this
    test pins that the resulting key is INJECTIVE over the whole vocabulary."""
    from featuregen.analysis.clarify import _ORDER, _clarification_rank

    codes = sorted(UNRESOLVED_CODES | set(_ORDER))
    keys = [_clarification_rank(code) for code in codes]
    assert len(set(keys)) == len(keys)


def test_the_population_outranks_every_question_it_decides_the_meaning_of():
    """Which table is the population decides what every later answer is ABOUT (`spine.py`: a wrong
    population silently shrinks the answer). Only the refusals that settle WHICH dataset and WHICH
    rows serve the need may precede it — and the selector's own population refusal is first of all.
    Nothing about the shape of the answer, and neither ROW question, may come before it."""
    from featuregen.analysis.clarify import _ORDER, _clarification_rank

    ahead = {code for code in UNRESOLVED_CODES
             if _clarification_rank(code) < _clarification_rank("population")}
    assert ahead == {SELECTION_POPULATION_UNDECLARED, SELECTION_SOURCE_AMBIGUOUS,
                     SELECTION_AUTHORITY_INSUFFICIENT, SELECTION_BINDING_MISSING,
                     TEMPORAL_MODEL_UNKNOWN, TEMPORAL_HISTORICAL_CURRENT_ONLY}
    assert min(_clarification_rank(c) for c in UNRESOLVED_CODES) == _clarification_rank(
        SELECTION_POPULATION_UNDECLARED)
    # The two that used to TIE with `population` are strictly after it BY RANK — not merely broken
    # apart by the name tiebreak, which would leave the real ordering to alphabetical luck.
    for code in (TEMPORAL_SNAPSHOT_TIE, TEMPORAL_SCD_OVERLAP):
        assert _ORDER[code] > _ORDER["population"], code


def test_the_asked_order_is_byte_stable_across_shuffled_input():
    """Three runs, the raised codes shuffled each time: the questions come back identical. The
    caller's enumeration order is an implementation detail and must not reach the user."""
    import random

    from featuregen.analysis.clarify import clarifications_for
    from featuregen.analysis.intent import IntentExtraction
    from featuregen.analysis.plan import AnalysisPlanV1, Measure

    raised = ["population", "entity", "dimensions", "measure", TEMPORAL_SNAPSHOT_TIE,
              TEMPORAL_SCD_OVERLAP, SELECTION_POPULATION_UNDECLARED, "windows", "comparison"]
    plan = AnalysisPlanV1(question="q", entity="customer", entity_ref=_COL, base_table_ref=_TABLE,
                          measure=Measure(op="count", logical_ref=""))
    seen = set()
    for seed in (1, 2, 3):
        shuffled = list(raised)
        random.Random(seed).shuffle(shuffled)
        asked = tuple(c.code for c in clarifications_for(
            IntentExtraction(plan=plan, unresolved=tuple(shuffled)), CANDIDATES))
        seen.add(asked)
    assert len(seen) == 1, seen
    (asked,) = seen
    assert asked[0] == SELECTION_POPULATION_UNDECLARED   # the selector's population, then the rest
    assert asked.index("population") < asked.index(TEMPORAL_SNAPSHOT_TIE)
    assert asked.index("population") < asked.index(TEMPORAL_SCD_OVERLAP)
    assert set(asked) == set(raised)


# ── learning: seven gaps, one deliberate non-gap ────────────────────────────────────────────────

def test_seven_of_eight_refusals_map_to_an_actionable_gap():
    mapped = {code for code in SELECTION_REFUSAL_CODES if code in REFUSAL_TO_GAP}
    assert mapped == SELECTION_REFUSAL_CODES - {TEMPORAL_SCD_OVERLAP}
    for code in mapped:
        gap_code, action = REFUSAL_TO_GAP[code]
        assert gap_code in GAP_CODES, code
        assert isinstance(action, RequiredAction), code


def test_scd_overlap_records_nothing_and_says_so_by_returning_none():
    """The existing ruling for ATTRIBUTION_OVERLAPPING_RECORDS, honoured rather than re-litigated:
    a data defect is not an ontology gap. `record_refusal` returns None BEFORE touching a
    connection, which is why `None` can be passed here."""
    assert TEMPORAL_SCD_OVERLAP not in REFUSAL_TO_GAP
    assert record_refusal(None, analysis_request_id="areq-1", refusal_code=TEMPORAL_SCD_OVERLAP,
                          subject_refs=(_TABLE,), dependency_snapshot_id="dep-1",
                          now=None) is None


def test_the_two_new_required_actions_name_the_two_surfaces_that_now_exist():
    """An action a reviewer cannot locate is not actionable — both point at routes this release
    ships (`/catalog/dataset-policies/serving|temporal`)."""
    assert RequiredAction.DECLARE_SERVING_POLICY.value == "declare_serving_policy"
    assert RequiredAction.DECLARE_TEMPORAL_POLICY.value == "declare_temporal_policy"
    assert len({a.value for a in RequiredAction}) == len(list(RequiredAction))


def test_the_temporal_model_choices_are_the_enum_minus_the_absence_being_recorded():
    """`unknown` is the state the gap RECORDS; offering it as an ANSWER would let a reviewer
    'resolve' a gap by restating it."""
    choices = CHOICE_VOCABULARIES["TEMPORAL_MODEL_UNRESOLVED"]
    assert set(choices) == {m.value for m in TemporalStorageModel
                            if m is not TemporalStorageModel.UNKNOWN}
    assert "unknown" not in choices
