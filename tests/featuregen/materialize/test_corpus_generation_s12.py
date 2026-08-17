"""S12 — corpus generation, generation only.

*"A coverage table with every refusal named; the batch triggers no execution."*

The second clause is a safety property, so it is checked STRUCTURALLY rather than by observing one
batch: a runtime assertion can only prove that a particular run executed nothing, and the claim is
that no run can. The module imports neither the verification store nor the publication store, and a
test reads that off its source.
"""
from __future__ import annotations

import inspect

import pytest
from tests.featuregen.materialize.test_subgraph_requirements_v2 import _fixed_aed_pilot, _fx_chain

from featuregen.formula.policy_occurrences import PolicyOccurrenceSetV1, PolicyOccurrenceV1
from featuregen.formula.policy_realization import (
    PolicyRealizationRevisionV1,
    RealizationProvenanceV1,
    family_key_for,
)
from featuregen.formula.policy_store import publish_policy_realization
from featuregen.materialize import corpus_generation
from featuregen.materialize.corpus_generation import (
    CorpusCandidateV1,
    CorpusRowV1,
    generate_corpus,
    named_refusals,
)
from featuregen.materialize.execution_proof_store import record_renderer_dispatch
from featuregen.materialize.operator_graph_v2 import OperatorKindV2
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.selection_revisions import BuildDeclarationV1, TargetModeV1

ENGINE = "kedro-pyspark"
ENV = "hdfc-local"
DIR_REF = "direction_sign:foundation-signed-by-indicator"


def _declaration(environment_id: str = ENV) -> BuildDeclarationV1:
    return BuildDeclarationV1(
        entity="customer", grain_keys=("hdfc::public.customers.cif_id",),
        purpose="churn propensity", base_name="customer_txn_features", cadence="daily",
        availability_promise_days=1, spine_source_ref="hdfc::public.customers",
        environment_id=environment_id)


def _occurrence(dataset: str = "public.transactions") -> PolicyOccurrenceV1:
    return PolicyOccurrenceV1(
        expr_path="body.expr", policy_ref_field="direction_policy_ref",
        policy_kind="direction_sign", policy_ref=DIR_REF, semantic_role="direction",
        bound_dataset=dataset, bound_column="txn_amt", environment_id=ENV)


def _candidate(
    candidate_id: str = "cand-1", *, mode: TargetModeV1 = TargetModeV1.PREDICTION,
    blockers: tuple[str, ...] = (), dataset: str = "public.transactions",
    graph=None, environment_id: str = ENV,
) -> CorpusCandidateV1:
    return CorpusCandidateV1(
        candidate_id=candidate_id, declaration=_declaration(environment_id), target_mode=mode,
        generation_authorization_revision_id=f"gar-{candidate_id}",
        activation_blockers=blockers,
        occurrences=PolicyOccurrenceSetV1((_occurrence(dataset),)),
        graph=graph or _fixed_aed_pilot())


def _dispatch_all(db, *, except_kind: str | None = None):
    record_renderer_dispatch(db, engine_id=ENGINE, dispatchable={
        kind.value: kind.value != except_kind for kind in OperatorKindV2})


def _publish(db, dataset: str = "public.transactions"):
    occurrence = _occurrence(dataset)
    publish_policy_realization(
        db,
        [PolicyRealizationRevisionV1(
            revision_id=f"rev-{dataset}", family_key=family_key_for(occurrence),
            executable_content_hash="sha256:debit-is-D", cas_pointer="cas://x",
            provenance=RealizationProvenanceV1.SOURCE_DERIVED,
            realizes_occurrences=(occurrence.occurrence_hash,))],
        expected_pointer_version=0, declared_by="ops@bank")


# ══ ACCEPTANCE 1 — THE BATCH TRIGGERS NO EXECUTION ═════════════════════════════════════════════
def test_THE_MODULE_CANNOT_REACH_VERIFICATION_OR_PUBLICATION():
    """Structural, because a runtime check can only prove that ONE batch executed nothing. There is
    no flag defaulting to off and no parameter nobody passes — the paths are absent."""
    source = inspect.getsource(corpus_generation)
    for forbidden in ("verification_store", "publication_attempt_store", "seal_v2",
                      "record_verification_attempt", "record_publication_attempt"):
        assert forbidden not in source, forbidden


def test_A_WHOLE_BATCH_WRITES_NOTHING_TO_THE_EXECUTION_TABLES(db):
    """The behavioural half beside the structural one: a batch over a mixed corpus leaves both
    execution tables empty."""
    _dispatch_all(db)
    _publish(db)
    generate_corpus(db, [_candidate("cand-1"),
                         _candidate("cand-2", blockers=(R.PERSONAL_DATA_POLICY_REQUIRED,)),
                         _candidate("cand-3", dataset="public.unrealized")],
                    engine_id=ENGINE)

    for table in ("verification_attempt", "verified_output_revision", "publication_attempt",
                  "sealed_artifact_v2"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_the_batch_takes_no_capability_and_no_staging_root():
    """Two arguments an executing batch would need. Their absence is what makes pointing this at a
    whole catalog safe."""
    parameters = set(inspect.signature(generate_corpus).parameters)
    assert parameters == {"conn", "candidates", "engine_id"}


# ══ ACCEPTANCE 2 — A COVERAGE TABLE WITH EVERY REFUSAL NAMED ═══════════════════════════════════
def test_EVERY_REFUSAL_IS_NAMED_not_counted(db):
    """The output is not "2 of 3 generated". Each candidate that could not generate carries the
    codes that stopped it, so the table answers what stands between this corpus and coverage."""
    _dispatch_all(db)
    _publish(db)
    coverage = generate_corpus(
        db,
        [_candidate("ok"),
         _candidate("licence", blockers=(R.PERSONAL_DATA_POLICY_REQUIRED,)),
         _candidate("no-policy", dataset="public.unrealized")],
        engine_id=ENGINE)

    assert coverage.generated == ("ok",)
    assert set(coverage.refused) == {"licence", "no-policy"}

    rows = {row.candidate_id: row for row in coverage.rows}
    assert rows["licence"].blockers == (R.PERSONAL_DATA_POLICY_REQUIRED,)
    assert rows["no-policy"].blockers == (R.POLICY_REFERENCE_UNRESOLVABLE,)


def test_a_ROW_CANNOT_BE_REFUSED_WITH_NOTHING_NAMED():
    """The one thing a coverage table exists to say."""
    with pytest.raises(ValueError, match="the one thing a coverage table exists to say"):
        CorpusRowV1(candidate_id="x", target_mode=TargetModeV1.PREDICTION, environment_id=ENV,
                    generated=False, blockers=())


def test_a_ROW_CANNOT_BE_GENERATED_AND_BLOCKED():
    """A table whose two columns disagree is read by whichever one the reader trusts."""
    with pytest.raises(ValueError, match="disagree"):
        CorpusRowV1(candidate_id="x", target_mode=TargetModeV1.PREDICTION, environment_id=ENV,
                    generated=True, blockers=(R.BINDING_NOT_BOUND,))


def test_THE_TOP_REASON_HAS_ONE_ANSWER(db):
    """`by_blocker` is built here rather than by a reader, so "why is coverage incomplete" is not
    computed three ways on three surfaces."""
    _dispatch_all(db)
    coverage = generate_corpus(
        db,
        [_candidate("a", dataset="public.unrealized"),
         _candidate("b", dataset="public.also-unrealized"),
         _candidate("c", blockers=(R.PERSONAL_DATA_POLICY_REQUIRED,),
                    dataset="public.unrealized")],
        engine_id=ENGINE)

    assert list(coverage.by_blocker) == [R.POLICY_REFERENCE_UNRESOLVABLE,
                                         R.PERSONAL_DATA_POLICY_REQUIRED]
    assert coverage.by_blocker[R.POLICY_REFERENCE_UNRESOLVABLE] == 3


def test_EVERY_NAMED_REFUSAL_CARRIES_ITS_REASON(db):
    """From the evaluator's disposition table, so a corpus report and the product surface explain
    the same code the same way."""
    _dispatch_all(db)
    coverage = generate_corpus(db, [_candidate("a", dataset="public.unrealized")],
                               engine_id=ENGINE)

    named = named_refusals(coverage)
    assert len(named) == 1
    code, reason, count = named[0]
    assert code == R.POLICY_REFERENCE_UNRESOLVABLE
    assert count == 1
    assert "no current realization" in reason


def test_an_UNDISPATCHABLE_operator_appears_in_the_table_too(db):
    """The other execution-chain blocker, so the table covers both things generation can refuse
    for that the activation policy never sees."""
    _publish(db)
    _dispatch_all(db, except_kind="as_of_fx_join")
    coverage = generate_corpus(db, [_candidate("fx", graph=_fx_chain())], engine_id=ENGINE)
    assert coverage.rows[0].blockers == (R.RENDERER_CANNOT_DISPATCH,)


def test_a_candidate_with_an_UNKNOWN_code_STOPS_the_batch(db):
    """Deliberate: swallowing it into a row would turn "a code nobody decided about" into "this
    candidate was refused for reasons", which is the silent shrinking the vocabulary prevents."""
    _dispatch_all(db)
    _publish(db)
    with pytest.raises(KeyError):
        generate_corpus(db, [_candidate("bad", blockers=("A_CODE_NOBODY_DECIDED_ABOUT",))],
                        engine_id=ENGINE)


def test_DUPLICATE_candidate_ids_are_refused(db):
    """A table with a duplicated row cannot say which of the two a blocker belongs to."""
    _dispatch_all(db)
    with pytest.raises(ValueError, match="share an id"):
        generate_corpus(db, [_candidate("same"), _candidate("same")], engine_id=ENGINE)


# ══ the TARGET MODE axis ═══════════════════════════════════════════════════════════════════════
def test_BOTH_MODES_are_recorded_and_separable(db):
    """A coverage number averaged across the two describes neither population: an exploration build
    has no target to leak, so its refusals are a different set of questions."""
    _dispatch_all(db)
    _publish(db)
    coverage = generate_corpus(
        db,
        [_candidate("pred", mode=TargetModeV1.PREDICTION),
         _candidate("expl", mode=TargetModeV1.EXPLORATION)],
        engine_id=ENGINE)

    assert [row.candidate_id for row in coverage.for_mode(TargetModeV1.PREDICTION)] == ["pred"]
    assert [row.candidate_id for row in coverage.for_mode(TargetModeV1.EXPLORATION)] == ["expl"]


def test_the_ENVIRONMENT_rides_on_every_row(db):
    """From the DECLARATION, so a row cannot claim an environment its build was not declared for."""
    _dispatch_all(db)
    _publish(db)
    coverage = generate_corpus(
        db, [_candidate("local", environment_id="hdfc-local"),
             _candidate("uat", environment_id="hdfc-uat")],
        engine_id=ENGINE)
    assert {row.candidate_id: row.environment_id for row in coverage.rows} == {
        "local": "hdfc-local", "uat": "hdfc-uat"}


def test_a_candidate_must_have_an_ID():
    with pytest.raises(ValueError, match="traceable back"):
        _candidate("  ")


def test_an_EMPTY_corpus_is_an_empty_table_not_an_error(db):
    """Nothing selected is a legitimate answer — a batch pointed at a filter that matched nothing
    should say so rather than refuse."""
    coverage = generate_corpus(db, [], engine_id=ENGINE)
    assert coverage.rows == ()
    assert coverage.by_blocker == {}
    assert named_refusals(coverage) == ()
