"""S13 — a free-form V2 run reaches admission through the v2 tool seam.

*"Acceptance: a free-form V2 run reaches admission through the **v2** tool seam; the advertised set
is ``renderer-dispatchable ∩ execution-proved``."*

The first clause is an END-TO-END claim and is tested that way: a real ``run_authoring_v2_replay``
against a scripted provider, through ``recipe_tool_runner_v2`` — the v2 tool seam — folding to a real
terminal trace event on the lane admission reads, and then admitted from that trace. Nothing is hand-built; the point is that the pieces
connect, and a fixture assembled to look like a run would prove only that the checks accept the shape
the test happens to write.

⟨LLM⟩ The provider is a scripted ``FakeLLM``, as every authoring suite in this tree is: what is under
test is the WIRING, and a live provider would make the test a billing question rather than a
correctness one.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import make_actor
from tests.featuregen.formula.authoring_fixtures import (
    REF_AMT,
    REF_CIF,
    REF_DT,
    TABLE_REF,
    seed_authoring_catalog,
)

from featuregen.formula.author import AUTHOR_TASK
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.recipe_authoring import recipe_tool_runner_v2
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.formula.turns import AuthoringIntent
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.materialize.admission import admit_artifacts
from featuregen.materialize.admission_v2 import (
    AdmittedFeatureV2,
    ResolvedFeatureInputV2,
    admit_artifacts_v2,
    implied_operator_signatures,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.execution_proof_store import (
    PILOT_MUTATIONS,
    SOLE_VARIANT,
    MutationOutcomeV1,
    record_execution_proof,
    record_renderer_dispatch,
    set_execution_proof,
    unqualified_operators,
)
from featuregen.materialize.operator_graph_v2 import OperatorKindV2
from featuregen.overlay.upload.publication_revisions import OperatorExecutionProofV1

ENGINE = "kedro-pyspark"
#: What the pilot formulas actually need, as SIGNATURES. `sum` specifically — advertising the bare
#: AGGREGATE kind would make the tests pass for a median too, which is exactly the confusion the
#: typed signature removes.
_PILOT_SIGNATURES = frozenset({
    ("aggregate", "sum"), ("aggregate", "count_rows"), ("aggregate", "count_non_null"),
    ("aggregate", "count_distinct"),
    ("final_combine", "identity"), ("final_combine", "ratio"), ("final_combine", "difference"),
})
_ACTOR = make_actor()
_INTENT = AuthoringIntent(
    "posted_debit_amount_30d",
    "accounts whose recent debit volume falls are more likely to attrite",
    "account",
    target_grain_keys=(REF_CIF,),
)
RUNTIMES = (("hive", "3.1.2"), ("spark", "3.3.0"), ("metastore", "3.1.2"),
            ("python", "3.11.14"), ("java", "11.0.20"), ("pyspark", "3.3.0"),
            ("kedro", "0.19.3"), ("kedro_datasets", "2.1.0"))


# ── the proposal a free-form run authors ────────────────────────────────────────────────────────
def _window(**overrides) -> dict:
    return {"event_time_ref": REF_DT, "basis": "trailing", "length": 90, "unit": "day",
            "start_inclusive": "inclusive", "end_inclusive": "exclusive",
            "timezone": "Asia/Dubai", "empty_window": "null", "null_input": "ignore",
            "offset_periods": 0, **overrides}


def _expr(aggregation: str = "sum", operand: str | None = REF_AMT, **overrides) -> dict:
    return {"aggregation": aggregation, "operand": operand,
            "source_relation": {"table_ref": TABLE_REF}, "filter": None, "window": _window(),
            "aggregation_argument": None, "second_operand": None, "authority_refs": None,
            **overrides}


def _raw(body: dict | None = None, **overrides) -> dict:
    return {"formula_schema_version": 2, "operation_grammar_version": 1,
            "canonicalization_version": 1,
            "grain": {"entity": "account", "keys": [REF_CIF]},
            "body": body if body is not None else {
                "final_operation": "identity", "expr": _expr()},
            "parameters": [], "expected_output": None, "allocation_policy_ref": "",
            "decimal": {"precision": 38, "scale": 6, "rounding": "half_even",
                        "overflow": "error"},
            **overrides}


def _client(raw: dict | None = None, findings=None) -> FakeLLM:
    return FakeLLM(script={
        AUTHOR_TASK: FakeResponse(
            output=None if raw is None else {
                "turn_type": "final_proposal", "final_proposal": raw}),
        CRITIC_TASK: FakeResponse(output={"findings": list(findings or [])}),
    })


def _monetary_facts(_proposal):
    """A governed, ref-keyed bundle — the shape `FrozenRecipeReadContext.formula_facts_v2` returns
    and `resolve_output_v2` reads."""
    return {REF_AMT: OperandFactsV2(
        logical_type="decimal", unit="monetary", currency="fixed:AED")}, ()


_RUN_IDS = iter(f"far_s13_{n:03d}" for n in range(1, 500))


def _free_form_run(db, raw: dict | None = None, *, intent: AuthoringIntent = _INTENT,
                   run_id: str | None = None):
    """ONE free-form v2 authoring run, through the V2 TOOL SEAM, on the lane admission reads.

    `run_authoring_v2_replay` is the orchestrator whose trace lands in
    `formula_authoring_trace_event` — the 1022 lane `materialize.authoring_trace` reads. The older
    `run_authoring_v2` writes the 1020 lane instead, so a run driven through it could never be
    admitted no matter what the checks said: a fact worth stating, because "the run completed" and
    "the run is admissible" would otherwise look like the same claim.

    `recipe_tool_runner_v2` is the seam the acceptance names: the same closed tool set as v1's, but
    answering `list_supported_operations` out of the v2 vocabulary and validating drafts with the v2
    parser. Under v1 tools the first names a grammar the model is not authoring in, and the second
    calls a valid v2 draft invalid.
    """
    client = _client(raw if raw is not None else _raw())
    return run_authoring_v2_replay(
        db, intent, client, client, actor=None,
        authoring_run_id=run_id or next(_RUN_IDS),
        facts_reader=_monetary_facts,
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
        tool_runner=recipe_tool_runner_v2(
            frozenset({TABLE_REF, REF_AMT, REF_DT, REF_CIF})))


def _dispatch_only(db):
    """Renderer support recorded and NO proofs attached — the honest state of this platform today."""
    record_renderer_dispatch(db, engine_id=ENGINE, dispatchable={
        **{(kind.value, SOLE_VARIANT): True for kind in OperatorKindV2},
        **{sig: True for sig in _PILOT_SIGNATURES}})


def _proof_hash_for(db) -> str:
    """One recorded proof, for tests that need something to attach rather than a whole advertisement."""
    return record_execution_proof(
        db,
        OperatorExecutionProofV1(
            signature="pilot-operator-graph", signature_version=1,
            compiler_version="formula-compiler@1", renderer_version="kedro-renderer@1",
            physical_type_policy="formula-v2/physical-types@1", topology_version=1,
            gold_corpus_hash="sha256:gold", generated_project_hash="sha256:project",
            mutation_set_version=1, engine_versions=RUNTIMES),
        tuple(MutationOutcomeV1(mutation_name=name, detected=True) for name in PILOT_MUTATIONS))


def _advertise(db, *, kinds: set[str] | None = None, signatures=None):
    """Advertise operators: dispatchable AND execution-proved, which is what advertised MEANS.

    Signatures now, not bare kinds — `("aggregate", "sum")` is advertised while
    `("aggregate", "avg")` is not, and a fixture that advertised the KIND would let a median through
    a check whose whole purpose is to stop one.
    """
    wanted = signatures if signatures is not None else _PILOT_SIGNATURES
    record_renderer_dispatch(db, engine_id=ENGINE, dispatchable={
        **{(kind.value, SOLE_VARIANT): True for kind in OperatorKindV2},
        **{sig: True for sig in wanted}})
    proof_hash = record_execution_proof(
        db,
        OperatorExecutionProofV1(
            signature="pilot-operator-graph", signature_version=1,
            compiler_version="formula-compiler@1", renderer_version="kedro-renderer@1",
            physical_type_policy="formula-v2/physical-types@1", topology_version=1,
            gold_corpus_hash="sha256:gold", generated_project_hash="sha256:project",
            mutation_set_version=1, engine_versions=RUNTIMES),
        tuple(MutationOutcomeV1(mutation_name=name, detected=True) for name in PILOT_MUTATIONS))
    if kinds is not None:
        proved = {(k, SOLE_VARIANT) for k in kinds}
    else:
        proved = {(k.value, SOLE_VARIANT) for k in OperatorKindV2} | set(wanted)
    for kind, variant in sorted(proved):
        set_execution_proof(db, engine_id=ENGINE, operator_kind=kind,
                            operator_variant=variant, proof_hash=proof_hash)


@pytest.fixture
def catalog(db):
    seed_authoring_catalog(db)
    return db


# ══ THE ACCEPTANCE — a free-form V2 run reaches admission ══════════════════════════════════════
def test_A_FREE_FORM_V2_RUN_REACHES_ADMISSION(catalog):
    """End to end, with nothing hand-built: a real orchestrator run through the v2 tool seam, a real
    terminal trace event, and an admission read back from that trace."""
    result = _free_form_run(catalog)
    assert result.authoring_disposition == "RESOLVED", result
    assert result.candidate_proposal is not None
    assert result.candidate_proposal.formula_schema_version == 2

    _advertise(catalog)
    admitted = admit_artifacts_v2(
        catalog, [ResolvedFeatureInputV2(_INTENT, result)], engine_id=ENGINE)

    assert len(admitted) == 1
    only = admitted[0]
    assert isinstance(only, AdmittedFeatureV2)
    assert only.feature_name == "posted_debit_amount_30d"
    assert only.authoring_run_id == result.authoring_run_id
    # The hash is RECOMPUTED by admission, never read off the result — and it agrees.
    assert only.proposal_content_hash == result.candidate_proposal_hash


def test_THE_SAME_RUN_IS_REFUSED_BY_V1_ADMISSION(catalog):
    """Why this module had to exist. V1's check 4b refuses any formula that is not version 1, and
    its docstring says the v2 path arrives with an engine that ADVERTISES it — which is what S13
    built. Without `admit_artifacts_v2` a free-form v2 run has nowhere to go."""
    from featuregen.materialize.admission import ResolvedFeatureInput

    result = _free_form_run(catalog)
    with pytest.raises((MaterializationRefused, AttributeError, TypeError)):
        # V1 admission cannot even read a v2 result: it wants `candidate_formula`, and its version
        # gate refuses anything that is not 1.
        admit_artifacts(catalog, [ResolvedFeatureInput(_INTENT, result)])  # type: ignore[arg-type]


def test_THE_TOOL_SEAM_IS_THE_V2_ONE(catalog):
    """`run_authoring_v2` REFUSES a missing runner rather than falling back to v1's, so 'through the
    v2 tool seam' is a property of the call rather than a claim about it."""
    from featuregen.formula.replay_authoring_v2 import ToolRunnerRequired

    client = _client(_raw())
    with pytest.raises(ToolRunnerRequired, match="wrong catalog surface"):
        run_authoring_v2_replay(
            catalog, _INTENT, client, client, actor=None, authoring_run_id="far_s13_notools",
            facts_reader=_monetary_facts, tool_runner=None)


# ══ the ADVERTISED-set gate (S13's two clauses meeting) ════════════════════════════════════════
def test_AN_UNADVERTISED_OPERATOR_REFUSES_ADMISSION(catalog):
    """The renderer has no branch for it, so there is nothing to compile and nothing to show."""
    result = _free_form_run(catalog)
    with pytest.raises(MaterializationRefused) as raised:
        admit_artifacts_v2(catalog, [ResolvedFeatureInputV2(_INTENT, result)], engine_id=ENGINE)
    assert raised.value.code is CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED
    assert "cannot emit in this build" in raised.value.detail


def test_DISPATCHABLE_WITHOUT_A_PROOF_IS_ENOUGH_TO_GENERATE_CODE(catalog):
    """The policy this step deliberately changed, and why.

    This test previously asserted the opposite: dispatch without a gold proof refused admission.
    That rule made admission unreachable in practice — nothing populates proofs until a gold harness
    exists (step 6), so EVERY formula refused, and the shortest route to a working pipeline became
    writing a proof record for a proof nobody ran. The rule intended to guarantee honesty was
    manufacturing the exact dishonesty it existed to prevent.

    The three claims are now separate. Renderer-support gates CODE GENERATION: the formula compiles
    and a person may read what was produced. Execution-qualification is reported beside it, not
    required. Artifact-verification gates PUBLICATION, and nothing here loosens that.

    Showing someone unverified code while saying it is unverified is honest. Refusing to show it
    until somebody forges a proof is not.
    """
    _dispatch_only(catalog)
    result = _free_form_run(catalog)

    admitted = admit_artifacts_v2(
        catalog, [ResolvedFeatureInputV2(_INTENT, result)], engine_id=ENGINE)
    assert admitted, "a compilable formula was refused for the platform's own missing evidence"

    # And the gap is NAMED, so "unverified" is something a user and an operator can both act on.
    unqualified = unqualified_operators(
        catalog, engine_id=ENGINE,
        signatures=implied_operator_signatures(result.candidate_proposal))
    assert ("aggregate", "sum") in unqualified
    assert set(unqualified) == set(implied_operator_signatures(result.candidate_proposal)), (
        "nothing has been proved, so every implied operator should be reported unqualified")


def test_a_PROVED_operator_stops_being_reported_as_unqualified(catalog):
    """The other direction: qualification is a real state, not a permanent disclaimer."""
    _advertise(catalog)
    result = _free_form_run(catalog)

    assert unqualified_operators(
        catalog, engine_id=ENGINE,
        signatures=implied_operator_signatures(result.candidate_proposal)) == ()


def test_ONE_MISSING_OPERATOR_IS_ENOUGH_TO_REFUSE(catalog):
    """Per SIGNATURE, not per engine and not per kind.

    Sharpened by the typed capability model: the engine advertises the AGGREGATE kind and every
    other operator, and is missing only `("aggregate", "sum")`. Under the old kind-level model this
    feature would have been admitted — the kind was advertised — and rendered against a renderer
    that cannot sum. That is the whole reason the variant exists.
    """
    _advertise(catalog, signatures=_PILOT_SIGNATURES - {("aggregate", "sum")})
    result = _free_form_run(catalog)
    with pytest.raises(MaterializationRefused) as raised:
        admit_artifacts_v2(catalog, [ResolvedFeatureInputV2(_INTENT, result)], engine_id=ENGINE)
    assert "aggregate" in raised.value.detail


def test_admission_requires_a_NAMED_engine(catalog):
    """The advertised set is per engine; admitting against an unnamed one checks the operators
    against nothing."""
    result = _free_form_run(catalog)
    with pytest.raises(ValueError, match="requires an engine_id"):
        admit_artifacts_v2(catalog, [ResolvedFeatureInputV2(_INTENT, result)], engine_id="  ")


# ══ the implied operators are DERIVED, never asserted ═════════════════════════════════════════
def test_THE_OPERATORS_ARE_DERIVED_FROM_THE_PROPOSAL(catalog):
    """An operator list a caller supplied is one that gets forgotten on the feature it mattered
    for — C-C10's rule, applied here."""
    result = _free_form_run(catalog)
    signatures = implied_operator_signatures(result.candidate_proposal)
    assert set(signatures) == {
        ("governed_scan", SOLE_VARIANT), ("group_assembly", SOLE_VARIANT),
        ("aggregate", "sum"), ("final_combine", "identity")}


def test_a_DECLARED_CURRENCY_CONVERSION_implies_the_FX_operators_and_its_gates(catalog):
    """An as-of join amplifies silently when the rate side offers two rows and drops rows when it
    offers none, so declaring a conversion implies the gates — the feature cannot be admitted
    against an engine that advertises the join but not them."""
    raw = _raw(body={"final_operation": "identity", "expr": _expr(authority_refs={
        "status_policy_ref": "", "direction_policy_ref": "", "reversal_policy_ref": "",
        "currency_conversion_ref": "currency_conversion:foundation-base-currency"})})
    result = _free_form_run(catalog, raw)
    assert result.candidate_proposal is not None

    kinds = {kind for kind, _variant in implied_operator_signatures(result.candidate_proposal)}
    assert {"as_of_fx_join", "duplicate_rate_gate", "missing_rate_gate",
            "decimal_multiplication"} <= kinds


def test_the_admitted_artifact_CARRIES_the_kinds_it_was_checked_against(catalog):
    """So a later stage does not re-derive the topology from a different reading than the one the
    advertised-set check was made against."""
    _advertise(catalog)
    result = _free_form_run(catalog)
    admitted = admit_artifacts_v2(
        catalog, [ResolvedFeatureInputV2(_INTENT, result)], engine_id=ENGINE)
    assert admitted[0].operator_kinds == implied_operator_signatures(result.candidate_proposal)


# ══ the checks that make admission a PROOF rather than a formality ════════════════════════════
def test_A_FORGED_PROPOSAL_IS_REFUSED(catalog):
    """The digest is recomputed from the supplied proposal; the result's own hash field is never
    trusted, because a forger sets it to whatever makes the pair look consistent."""
    import dataclasses

    _advertise(catalog)
    result = _free_form_run(catalog)
    other = _free_form_run(catalog, _raw(body={
        "final_operation": "identity", "expr": _expr("count", None)}))
    # A result citing the FIRST run, carrying the SECOND run's proposal.
    forged = dataclasses.replace(result, candidate_proposal=other.candidate_proposal)

    with pytest.raises(MaterializationRefused) as raised:
        admit_artifacts_v2(catalog, [ResolvedFeatureInputV2(_INTENT, forged)], engine_id=ENGINE)
    assert raised.value.code is CompilationRefusalCode.FORMULA_HASH_MISMATCH


def test_A_RESULT_WITH_NO_PROPOSAL_IS_REFUSED(catalog):
    import dataclasses

    _advertise(catalog)
    result = _free_form_run(catalog)
    with pytest.raises(MaterializationRefused, match="carries no candidate proposal"):
        admit_artifacts_v2(
            catalog,
            [ResolvedFeatureInputV2(_INTENT, dataclasses.replace(
                result, candidate_proposal=None))],
            engine_id=ENGINE)


def test_A_DIFFERENT_INTENT_IS_REFUSED(catalog):
    """The manifest is written before any provider call and is write-once, so it is the immutable
    record of what was asked."""
    _advertise(catalog)
    result = _free_form_run(catalog)
    other = AuthoringIntent(
        name="posted_debit_amount_30d", hypothesis="a different hypothesis entirely",
        target_entity="customer", target_grain_keys=(REF_CIF,))
    # The hasher admission uses, not v2's four-field one — see `_verify_intent_hash_v2`.
    from featuregen.materialize.authoring_trace import authoring_intent_hash

    assert authoring_intent_hash(other) != authoring_intent_hash(_INTENT)

    with pytest.raises(MaterializationRefused) as raised:
        admit_artifacts_v2(catalog, [ResolvedFeatureInputV2(other, result)], engine_id=ENGINE)
    assert raised.value.code is CompilationRefusalCode.INTENT_HASH_MISMATCH


def test_MISMATCHED_AXES_ARE_REFUSED(catalog):
    import dataclasses

    _advertise(catalog)
    result = _free_form_run(catalog)
    with pytest.raises(MaterializationRefused) as raised:
        admit_artifacts_v2(
            catalog,
            [ResolvedFeatureInputV2(_INTENT, dataclasses.replace(
                result, capability_status="unsupported_operation"))],
            engine_id=ENGINE)
    assert raised.value.code is CompilationRefusalCode.AXES_MISMATCH


def test_a_V1_FORMULA_REACHING_THIS_CHAIN_IS_REFUSED(catalog):
    """The mirror image of v1's check 4b: a v1 formula read under v2 semantics would be read under
    operations v1 never defined."""
    from featuregen.materialize.admission_v2 import _verify_language_version

    class _V1Shaped:
        formula_schema_version = 1

    with pytest.raises(MaterializationRefused) as raised:
        _verify_language_version(_V1Shaped(), "run-1")  # type: ignore[arg-type]
    assert raised.value.code is CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED
    assert "not the Formula-V2 language" in raised.value.detail


def test_a_version_NOBODY_DEFINED_is_refused_too(catalog):
    """A membership test rather than a floor: an inequality that happened to be open-ended would
    admit a grammar that does not exist."""
    from featuregen.materialize.admission_v2 import _verify_language_version

    class _Future:
        formula_schema_version = 4

    with pytest.raises(MaterializationRefused, match="not the Formula-V2 language"):
        _verify_language_version(_Future(), "run-1")  # type: ignore[arg-type]


def test_admission_is_ALL_OR_NOTHING(catalog):
    """A caller that admitted the survivors of a refused batch would compile a group whose
    membership nobody decided."""
    import dataclasses

    _advertise(catalog)
    good = _free_form_run(catalog)
    bad = dataclasses.replace(good, capability_status="unsupported_operation")
    with pytest.raises(MaterializationRefused):
        admit_artifacts_v2(
            catalog,
            [ResolvedFeatureInputV2(_INTENT, good), ResolvedFeatureInputV2(_INTENT, bad)],
            engine_id=ENGINE)


def test_the_shared_checks_are_IMPORTED_not_reimplemented():
    """Checks 1, 2 and 3 read one trace row and carry no grammar. Two readings of one record
    eventually disagree about what a tampered payload looks like."""
    import inspect

    from featuregen.materialize import admission, admission_v2

    source = inspect.getsource(admission_v2)
    for shared in ("_terminal_event", "_verify_payload_hash", "_require_resolved"):
        assert "from featuregen.materialize.admission import" in source or shared in source
        assert getattr(admission_v2, shared) is getattr(admission, shared)
