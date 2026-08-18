"""S8 — the development gold proof, capability proofs and ``evaluate_generate`` (1079).

*"Every case and mutation behaves; changed bytes invalidate a proof without a version bump;
capability is ``renderer-dispatchable ∧ execution-proved``."*

The middle clause is the one worth being careful about: it is satisfied by a MECHANISM, not by a
check. A proof is looked up BY the bytes it was taken over, so different bytes simply do not find
one — there is no staleness flag to set and no version to increment, because both would be claims
someone had to remember to make.
"""
from __future__ import annotations

import psycopg
import pytest
from tests.featuregen.materialize.test_subgraph_requirements_v2 import _fixed_aed_pilot, _fx_chain

from featuregen.formula.policy_occurrences import PolicyOccurrenceSetV1, PolicyOccurrenceV1
from featuregen.formula.policy_realization import (
    PolicyRealizationRevisionV1,
    RealizationProvenanceV1,
    family_key_for,
)
from featuregen.formula.policy_store import publish_policy_realization
from featuregen.materialize.evaluate_generate import (
    evaluate_generate,
    undispatchable_kinds,
    unresolvable_references,
)
from featuregen.materialize.execution_proof_store import (
    PILOT_MUTATIONS,
    SOLE_VARIANT,
    MutationOutcomeV1,
    ProofIncomplete,
    advertised_operators,
    capability_of,
    proof_for_bytes,
    record_execution_proof,
    record_renderer_dispatch,
    set_execution_proof,
)
from featuregen.materialize.operator_graph_v2 import OperatorKindV2
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.publication_revisions import OperatorExecutionProofV1

ENGINE = "kedro-pyspark"
RUNTIMES = (("hive", "3.1.2"), ("spark", "3.3.0"), ("metastore", "3.1.2"),
            ("python", "3.11.14"), ("java", "11.0.20"), ("pyspark", "3.3.0"),
            ("kedro", "0.19.3"), ("kedro_datasets", "2.1.0"))


def _proof(*, project_hash: str = "sha256:gold-project",
           mutation_set_version: int = 1) -> OperatorExecutionProofV1:
    return OperatorExecutionProofV1(
        signature="pilot-operator-graph", signature_version=1,
        compiler_version="formula-compiler@1", renderer_version="kedro-renderer@1",
        physical_type_policy="formula-v2/physical-types@1", topology_version=1,
        gold_corpus_hash="sha256:gold-corpus", generated_project_hash=project_hash,
        mutation_set_version=mutation_set_version, engine_versions=RUNTIMES)


def _all_caught(names=PILOT_MUTATIONS):
    return tuple(MutationOutcomeV1(mutation_name=name, detected=True,
                                   detail=f"gold corpus rejected {name}")
                 for name in names)


# ══ ACCEPTANCE 1 — every case and MUTATION behaves ══════════════════════════════════════════════
def test_A_COMPLETE_MUTATION_SET_records(db):
    proof_hash = record_execution_proof(db, _proof(), _all_caught())
    assert proof_hash == _proof().content_hash
    rows = db.execute(
        "SELECT mutation_name, detected FROM operator_proof_mutation WHERE proof_hash = %s "
        "ORDER BY mutation_name", (proof_hash,)).fetchall()
    assert [name for name, _ in rows] == sorted(PILOT_MUTATIONS)
    assert all(detected for _, detected in rows)


@pytest.mark.parametrize("dropped", PILOT_MUTATIONS)
def test_A_MUTATION_SET_WITH_A_HOLE_IS_REFUSED(db, dropped):
    """A hole proves nothing about the behaviour it covers, and the proof would claim a corpus that
    catches wrong numbers it was never shown. Parametrized over all seven, so the check is a rule
    rather than a guard on whichever one someone thought of."""
    partial = _all_caught(tuple(name for name in PILOT_MUTATIONS if name != dropped))
    with pytest.raises(ProofIncomplete, match="does not run"):
        record_execution_proof(db, _proof(), partial)
    assert db.execute("SELECT count(*) FROM operator_execution_proof").fetchone()[0] == 0


@pytest.mark.parametrize("undetected", PILOT_MUTATIONS)
def test_A_MUTATION_THE_CORPUS_MISSED_REFUSES_THE_PROOF(db, undetected):
    """Each of the seven is a wrong NUMBER rather than an error, so a corpus that misses one would
    let that exact defect ship. The outcome is real; the corpus is what needs fixing."""
    outcomes = tuple(
        MutationOutcomeV1(mutation_name=name, detected=name != undetected,
                          detail="" if name != undetected else "the corpus produced the same total")
        for name in PILOT_MUTATIONS)
    with pytest.raises(ProofIncomplete, match="did NOT catch"):
        record_execution_proof(db, _proof(), outcomes)


def test_a_mutation_OUTSIDE_the_pinned_set_is_refused(db):
    """A mutation outside the pinned set is one nobody can reproduce from the version alone."""
    with pytest.raises(ProofIncomplete, match="not in the mutation set version"):
        record_execution_proof(
            db, _proof(), (*_all_caught(), MutationOutcomeV1("some_new_mutation", True)))


def test_a_mutation_reported_TWICE_is_refused(db):
    with pytest.raises(ProofIncomplete, match="list-order accident"):
        record_execution_proof(
            db, _proof(),
            (*_all_caught(), MutationOutcomeV1(PILOT_MUTATIONS[0], False)))


def test_the_seven_mutations_are_the_ones_the_plan_names():
    """Frozen as a value the code checks completeness against, rather than a list in a document."""
    assert set(PILOT_MUTATIONS) == {
        "wrong_debit_mapping", "missing_status_filter", "reversal_neutralization_removed",
        "post_cutoff_fx_accepted", "quote_inversion_reversed", "conversion_after_aggregation",
        "duplicate_rate_gate_deleted"}


# ══ ACCEPTANCE 2 — CHANGED BYTES invalidate a proof, with no version bump ══════════════════════
def test_CHANGED_BYTES_FIND_NO_PROOF(db):
    """The mechanism, not a check: a proof is looked up BY the bytes it was taken over."""
    record_execution_proof(db, _proof(project_hash="sha256:gold-project"), _all_caught())

    assert proof_for_bytes(db, "sha256:gold-project") is not None
    assert proof_for_bytes(db, "sha256:one-byte-different") is None


def test_NO_VERSION_BUMP_IS_NEEDED_or_possible(db):
    """The same everything, one byte of project different — two DIFFERENT proofs, because the bytes
    are inside the proof's own content hash. Nothing had to be incremented, and there is nothing an
    author could have forgotten to increment."""
    first = _proof(project_hash="sha256:gold-project")
    second = _proof(project_hash="sha256:one-byte-different")

    assert first.mutation_set_version == second.mutation_set_version
    assert first.signature_version == second.signature_version
    assert first.content_hash != second.content_hash

    record_execution_proof(db, first, _all_caught())
    record_execution_proof(db, second, _all_caught())
    assert db.execute("SELECT count(*) FROM operator_execution_proof").fetchone()[0] == 2


def test_the_proof_carries_NO_S9_CHECK_SET_FIELD():
    """Sandbox verification does not exist at S8 and is a separate concept — a nullable field for it
    would get filled in eventually, by someone who assumed it meant the same thing."""
    fields = set(OperatorExecutionProofV1.__dataclass_fields__)
    assert not any("check_set" in name or "verification" in name for name in fields), fields


def test_a_proof_is_APPEND_ONLY(db):
    record_execution_proof(db, _proof(), _all_caught())
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(
            "UPDATE operator_execution_proof SET generated_project_hash = %s WHERE proof_hash = %s",
            ("sha256:rewritten", _proof().content_hash))


# ══ ACCEPTANCE 3 — capability is renderer-dispatchable ∧ execution-proved ══════════════════════
def _dispatch_all(db, *, dispatchable: bool = True, except_kind: str | None = None):
    record_renderer_dispatch(db, engine_id=ENGINE, dispatchable={
        (kind.value, SOLE_VARIANT): (dispatchable and kind.value != except_kind)
        for kind in OperatorKindV2})


def test_THE_TWO_FACTS_ARE_RECORDED_SEPARATELY(db):
    """Invariant 11. Collapsing them into one flag loses which half is missing, and the halves have
    completely different remedies — write a renderer branch, or run the proof."""
    _dispatch_all(db)
    capability = capability_of(db, engine_id=ENGINE, operator_kind="aggregate")
    assert capability is not None
    assert capability.renderer_dispatchable is True
    assert capability.execution_proved is False        # no proof yet — NOT "it failed"
    assert capability.supported is False

    proof_hash = record_execution_proof(db, _proof(), _all_caught())
    set_execution_proof(db, engine_id=ENGINE, operator_kind="aggregate", proof_hash=proof_hash)

    proved = capability_of(db, engine_id=ENGINE, operator_kind="aggregate")
    assert proved is not None
    assert (proved.renderer_dispatchable, proved.execution_proved) == (True, True)
    assert proved.supported is True


def test_DISPATCHABLE_WITHOUT_A_PROOF_IS_NOT_SUPPORTED(db):
    _dispatch_all(db)
    capability = capability_of(db, engine_id=ENGINE, operator_kind="as_of_fx_join")
    assert capability is not None and capability.supported is False


def test_PROVED_BUT_NOT_DISPATCHABLE_IS_NOT_SUPPORTED(db):
    """The other half, so the conjunction is a rule rather than an inability to tell them apart."""
    _dispatch_all(db, except_kind="quote_inversion")
    proof_hash = record_execution_proof(db, _proof(), _all_caught())
    set_execution_proof(db, engine_id=ENGINE, operator_kind="quote_inversion",
                        proof_hash=proof_hash)

    capability = capability_of(db, engine_id=ENGINE, operator_kind="quote_inversion")
    assert capability is not None
    assert (capability.renderer_dispatchable, capability.execution_proved) == (False, True)
    assert capability.supported is False


def test_an_UNRECORDED_operator_is_None_never_a_default(db):
    """"Never recorded" and "recorded as unsupported" must not look the same."""
    assert capability_of(db, engine_id=ENGINE, operator_kind="aggregate") is None
    assert capability_of(db, engine_id="an-engine-nobody-built", operator_kind="aggregate") is None


def test_a_dispatch_record_must_cover_the_WHOLE_vocabulary(db):
    """The renderer either has a branch or it does not, so an omitted kind is not a third answer —
    it is a claim nobody made that a caller will read as one."""
    with pytest.raises(ValueError, match="omits"):
        record_renderer_dispatch(db, engine_id=ENGINE,
                                 dispatchable={("aggregate", SOLE_VARIANT): True})


def test_a_dispatch_record_naming_a_NON_OPERATOR_is_refused(db):
    with pytest.raises(ValueError, match="not operator kinds"):
        record_renderer_dispatch(db, engine_id=ENGINE, dispatchable={
            **{(kind.value, SOLE_VARIANT): True for kind in OperatorKindV2},
            ("teleportation", SOLE_VARIANT): True})


def test_a_proof_cannot_be_attached_where_there_is_NO_DISPATCH_RECORD(db):
    """An operator with a proof and no dispatch record would be "proved" for a renderer that may not
    be able to emit it at all."""
    proof_hash = record_execution_proof(db, _proof(), _all_caught())
    with pytest.raises(ValueError, match="no capability row"):
        set_execution_proof(db, engine_id=ENGINE, operator_kind="aggregate",
                            proof_hash=proof_hash)


def test_a_capability_can_only_point_at_a_proof_that_EXISTS(db):
    _dispatch_all(db)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "UPDATE engine_operator_capability SET execution_proof_hash = %s "
            "WHERE engine_id = %s AND operator_kind = %s",
            ("sha256:no-such-proof", ENGINE, "aggregate"))


# ══ evaluate_generate ══════════════════════════════════════════════════════════════════════════
DIR_REF = "direction_sign:foundation-signed-by-indicator"


def _occurrence(dataset: str = "public.transactions") -> PolicyOccurrenceV1:
    return PolicyOccurrenceV1(
        expr_path="body.expr", policy_ref_field="direction_policy_ref",
        policy_kind="direction_sign", policy_ref=DIR_REF, semantic_role="direction",
        bound_dataset=dataset, bound_column="txn_amt", environment_id="hdfc-local")


def _publish(db, occurrence):
    publish_policy_realization(
        db,
        [PolicyRealizationRevisionV1(
            revision_id="rev-1", family_key=family_key_for(occurrence),
            executable_content_hash="sha256:debit-is-D", cas_pointer="cas://x",
            provenance=RealizationProvenanceV1.SOURCE_DERIVED,
            realizes_occurrences=(occurrence.occurrence_hash,))],
        expected_pointer_version=0, declared_by="ops@bank")


def _generate(db, **overrides):
    occurrence = _occurrence()
    kwargs = dict(generation_authorization_revision_id="gar-1", activation_blockers=(),
                  occurrences=PolicyOccurrenceSetV1((occurrence,)),
                  graph=_fixed_aed_pilot(), engine_id=ENGINE)
    kwargs.update(overrides)
    return evaluate_generate(db, **kwargs)


def test_GENERATE_IS_ALLOWED_when_every_requirement_is_met(db):
    _dispatch_all(db)
    _publish(db, _occurrence())
    verdict = _generate(db)
    assert verdict.allowed is True
    assert verdict.blockers == ()


def test_an_UNRESOLVABLE_POLICY_refuses_generation(db):
    """S4's resolver is what decides what "current" means; a second answer here would let a feature
    generate against a realization the resolver would not have served."""
    _dispatch_all(db)
    verdict = _generate(db)       # nothing published
    assert verdict.allowed is False
    assert R.POLICY_REFERENCE_UNRESOLVABLE in verdict.blockers


def test_an_UNDISPATCHABLE_OPERATOR_refuses_generation(db):
    """Asked PER OPERATOR: an engine with a branch for twelve of thirteen supports twelve, and a
    feature using the thirteenth must refuse."""
    _publish(db, _occurrence())
    _dispatch_all(db, except_kind="as_of_fx_join")
    verdict = _generate(db, graph=_fx_chain())
    assert verdict.allowed is False
    assert R.RENDERER_CANNOT_DISPATCH in verdict.blockers

    # The same graph with that operator dispatchable generates — so the refusal is about the
    # operator, not about FX graphs being refused wholesale.
    _dispatch_all(db)
    assert _generate(db, graph=_fx_chain()).allowed is True


def test_an_UNRECORDED_operator_counts_as_undispatchable(db):
    """Treating an unrecorded operator as renderable is how a project that cannot run gets
    generated."""
    _publish(db, _occurrence())
    assert undispatchable_kinds(db, _fixed_aed_pilot(), engine_id=ENGINE)
    assert _generate(db).allowed is False


def test_ACTIVATION_BLOCKERS_ARE_CARRIED_not_recomputed(db):
    """Passed in, so this evaluator cannot disagree with the readiness the UI shows."""
    _dispatch_all(db)
    _publish(db, _occurrence())
    verdict = _generate(db, activation_blockers=(R.PERSONAL_DATA_POLICY_REQUIRED,))
    assert verdict.allowed is False
    assert verdict.blockers == (R.PERSONAL_DATA_POLICY_REQUIRED,)


def test_a_DROPPED_activation_blocker_does_not_refuse_generation(db):
    """"Dropped" means this evaluator is not the gate for it — a governed V2 formula stands on a
    reviewed recipe blueprint rather than on a person confirming each column's meaning."""
    _dispatch_all(db)
    _publish(db, _occurrence())
    assert _generate(db, activation_blockers=(R.PROPOSED_METADATA_ONLY,)).allowed is True


def test_an_UNKNOWN_activation_code_STOPS_the_evaluation(db):
    """Silently ignoring one is the defect the disposition table exists to prevent, and shrinking
    the blocker list is how a gate stops gating."""
    _dispatch_all(db)
    _publish(db, _occurrence())
    with pytest.raises(KeyError):
        _generate(db, activation_blockers=("A_CODE_NOBODY_DECIDED_ABOUT",))


def test_generation_must_name_the_revision_it_is_FOR(db):
    """Invariant 17: a generation is authorized for a target."""
    with pytest.raises(ValueError, match="authorized for nothing"):
        _generate(db, generation_authorization_revision_id="  ")


def test_BOTH_execution_chain_blockers_can_appear_together(db):
    """A verdict lists everything standing in the way, not the first thing it found — an operator
    told one blocker fixes it and meets the next."""
    verdict = _generate(db, graph=_fx_chain())
    assert verdict.allowed is False
    assert set(verdict.blockers) == {R.POLICY_REFERENCE_UNRESOLVABLE, R.RENDERER_CANNOT_DISPATCH}


def test_generation_does_NOT_require_an_execution_proof(db):
    """Requiring one here would make a never-executed operator ungenerable — which is how it would
    never get proved. The proof is what S9's verification and S10's publication rest on."""
    _dispatch_all(db)
    _publish(db, _occurrence())
    assert capability_of(db, engine_id=ENGINE, operator_kind="aggregate").execution_proved is False
    assert _generate(db).allowed is True


def test_the_unresolved_reference_report_NAMES_the_family(db):
    """"Unresolvable" without which policy, in what role, over which dataset cannot be acted on."""
    _dispatch_all(db)
    reported = unresolvable_references(db, PolicyOccurrenceSetV1((_occurrence(),)))
    assert len(reported) == 1
    for part in (DIR_REF, "direction", "public.transactions"):
        assert part in reported[0], part


# ══ S13's second clause — the ADVERTISED SET is the INTERSECTION ════════════════════════════════
def test_THE_ADVERTISED_SET_IS_DISPATCHABLE_INTERSECT_PROVED(db):
    """Not the union, and not the dispatchable set. A renderer branch with no proof is a branch
    nobody has run against reviewed gold; a proof for an operator this build cannot emit is a proof
    about a different build. Advertising either would be advertising half a claim."""
    _dispatch_all(db, except_kind="quote_inversion")
    proof_hash = record_execution_proof(db, _proof(), _all_caught())

    # proved AND dispatchable
    set_execution_proof(db, engine_id=ENGINE, operator_kind="aggregate", proof_hash=proof_hash)
    # proved but NOT dispatchable
    set_execution_proof(db, engine_id=ENGINE, operator_kind="quote_inversion",
                        proof_hash=proof_hash)
    # dispatchable but NOT proved: every other kind, left alone

    assert advertised_operators(db, engine_id=ENGINE) == (("aggregate", SOLE_VARIANT),)


def test_an_UNRECORDED_operator_is_not_advertised(db):
    """Absent, never assumed: this build has established neither fact about it."""
    assert advertised_operators(db, engine_id=ENGINE) == ()
    assert advertised_operators(db, engine_id="an-engine-nobody-built") == ()


def test_the_advertised_set_is_computed_in_ONE_place():
    """Three surfaces computing the intersection three ways is how one of them starts advertising
    an operator on half the evidence."""
    import inspect

    from featuregen.materialize import execution_proof_store

    source = inspect.getsource(execution_proof_store)
    assert source.count("renderer_dispatchable AND execution_proof_hash IS NOT NULL") == 1
