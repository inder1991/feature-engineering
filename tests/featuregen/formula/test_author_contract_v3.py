"""The V3 author-turn contract, and the version agreement it makes enforceable.

▲ **WHAT WAS TRUE BEFORE THIS FILE.** The production formula-draft worker declares
``formula_schema: 3`` on every run it opens, and drove the provider under
``AUTHOR_TURN_CONTRACT_V2`` — whose instruction says, in as many words, *"The proposal MUST declare
formula_schema_version 2."* The provider duly returned a v2 proposal, the orchestrator accepted
either type without comparing it to what it had asked for, and the draft reached READY. Every "v3"
run this platform had ever authored was a v2 run wearing a v3 label, and the semantic row selections
v3 exists FOR were never requested from anyone.

The agreement is now enforced at three independent places, and each covers what the others cannot:

    the CONTRACT      a v3 run is driven under a v3 instruction and a v3 response schema, so a
                      live provider's v2 answer is rejected on the wire
    AUTHORING         the parsed proposal is compared to the schema the run requested, which
                      catches replayed, deterministic and imported material that no response
                      schema ever validated
    ADMISSION         the manifest, the proposal's declared version and its runtime TYPE must all
                      agree, which catches traces written before any of this existed
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.materialize.test_admission_v2_s13 import _raw, _raw_v3

from featuregen.formula.author import (
    AUTHOR_CONTRACT_BY_FORMULA_SCHEMA,
    AUTHOR_TURN_CONTRACT_V2,
    AUTHOR_TURN_CONTRACT_V3,
    author_contract_for,
)

_SETTINGS = {"model": "m", "max_tokens": 1, "temperature": 0, "top_p": 1,
             "stop_sequences": (), "seed": None, "provider": "p"}


# ══ THE CONTRACT IS SELECTED, NOT FIXED ════════════════════════════════════════════════════════
def test_a_V3_RUN_IS_DRIVEN_UNDER_A_V3_INSTRUCTION():
    """The defect in one assertion: the instruction a v3 run holds its provider to must ask for
    version 3. It asked for 2."""
    assert "formula_schema_version 3" in author_contract_for(3).instruction
    assert "formula_schema_version 2" in author_contract_for(2).instruction


def test_the_V3_CONTRACT_HAS_ITS_OWN_AUDITED_IDENTITY():
    """The audited seam records `output_schema_id`/`version` and `frozen_configuration` hashes them,
    so a v3 run requested under the v2 identity is indistinguishable in the audit from a v2 run."""
    v2, v3 = author_contract_for(2), author_contract_for(3)

    assert v3.schema_id == "formula_author_turn_v3"
    assert v3.prompt_id == "formula_author_turn_v3"
    assert (v3.schema_id, v3.prompt_id) != (v2.schema_id, v2.prompt_id)


def test_the_V3_INSTRUCTION_DESCRIBES_WHAT_V3_IS_FOR():
    """A version bump that did not mention row selections would ask for v3 and describe v2 — and
    the model would have no reason to emit the one field the grammar was added for."""
    assert "row_selections" in author_contract_for(3).instruction


def test_the_MAPPING_IS_CLOSED_and_an_unknown_schema_fails_early():
    """Before a run is opened and before any provider call: a schema with no contract has no
    instruction to hold a model to and no schema to validate its answer against."""
    assert AUTHOR_CONTRACT_BY_FORMULA_SCHEMA == {2: AUTHOR_TURN_CONTRACT_V2, 3: AUTHOR_TURN_CONTRACT_V3}

    with pytest.raises(ValueError, match="no author turn contract"):
        author_contract_for(4)
    with pytest.raises(ValueError, match="no author turn contract"):
        author_contract_for(1)


def test_a_V2_ANSWER_DOES_NOT_VALIDATE_AGAINST_THE_V3_TURN_SCHEMA():
    """The wire-level half. A live provider's v2 proposal is rejected by the response schema before
    it ever reaches the orchestrator — which is why the authoring comparison below is described as
    protecting replayed and imported material rather than as the primary gate."""
    import jsonschema

    from featuregen.formula.turns_v3 import AUTHOR_TURN_V3_SCHEMA

    jsonschema.validate({"turn_type": "final_proposal", "final_proposal": _raw_v3()},
                        AUTHOR_TURN_V3_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"turn_type": "final_proposal", "final_proposal": _raw()},
                            AUTHOR_TURN_V3_SCHEMA)


# ══ THE FROZEN CONFIGURATION CANNOT MISDESCRIBE ITSELF ═════════════════════════════════════════
def test_a_SCHEMA_3_FREEZE_CARRIES_THE_V3_AUTHOR_IDENTITY():
    """`formula_schema=3` beside `formula_author_turn_v2` should be unconstructible: the whole
    author identity — prompt, instruction and turn schema — is selected from the requested schema,
    not just the canonicalization axis."""
    import json

    from featuregen.formula.frozen_configuration import freeze_current_configuration_v2

    v3 = freeze_current_configuration_v2(generation_settings=_SETTINGS, formula_schema_version=3)
    v2 = freeze_current_configuration_v2(generation_settings=_SETTINGS, formula_schema_version=2)

    assert json.loads(v3.version_vector_json)["formula_schema"] == 3
    assert v3.author.prompt_id == "formula_author_turn_v3"
    assert v2.author.prompt_id == "formula_author_turn_v2"
    assert v3.configuration_hash != v2.configuration_hash


def test_a_SEAL_THAT_DECLARES_NO_SCHEMA_IS_REFUSED_rather_than_guessed():
    """`verify_frozen_configuration_v2` re-freezes to compare. Re-freezing under a guess would
    answer "has this drifted?" with "it depends what I assumed it was"."""
    import dataclasses
    import json

    from featuregen.formula.frozen_configuration import (
        ConfigurationDrifted,
        freeze_current_configuration_v2,
        verify_frozen_configuration_v2,
    )

    sealed = freeze_current_configuration_v2(
        generation_settings=_SETTINGS, formula_schema_version=3)
    verify_frozen_configuration_v2(sealed, generation_settings=_SETTINGS)   # the control

    vector = json.loads(sealed.version_vector_json)
    del vector["formula_schema"]
    stripped = dataclasses.replace(sealed, version_vector_json=json.dumps(vector))

    with pytest.raises(ConfigurationDrifted, match="no formula_schema"):
        verify_frozen_configuration_v2(stripped, generation_settings=_SETTINGS)


# ══ THE MISMATCH IS REFUSED DURING AUTHORING ═══════════════════════════════════════════════════
def _run(db, *, run_id: str, raw: dict, requested: int):
    """One real authoring run whose SCRIPTED answer and REQUESTED schema can disagree."""
    from tests.featuregen.materialize.test_admission_v2_s13 import (
        _INTENT,
        REF_AMT,
        REF_CIF,
        REF_DT,
        TABLE_REF,
        _client,
        _monetary_facts,
    )

    from featuregen.formula.recipe_authoring import recipe_tool_runner_v2
    from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay

    client = _client(raw)
    return run_authoring_v2_replay(
        db, _INTENT, client, client, actor=None, authoring_run_id=run_id,
        facts_reader=_monetary_facts,
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
        tool_runner=recipe_tool_runner_v2(frozenset({TABLE_REF, REF_AMT, REF_DT, REF_CIF})),
        formula_schema_version=requested)


def test_REQUESTING_V3_AND_PRODUCING_V2_IS_REFUSED_ON_THE_WIRE(db):
    """The FIRST of the three guards, and the one a live provider meets.

    A scripted v2 answer to a v3 run never reaches the orchestrator's comparison at all: the v3
    response schema rejects it, the repair loop exhausts, and the run ends TECHNICAL_FAILURE
    carrying no artifact. Before the v3 contract existed this same fixture produced a READY draft.
    """
    result = _run(db, run_id="far-mismatch-32", raw=_raw(), requested=3)

    assert result.authoring_disposition == "TECHNICAL_FAILURE"
    assert result.candidate_proposal is None, "a protocol failure carries no artifact"

    # The terminal is `failed` and carries the technical disposition. (The provider-stage reason
    # rides the audited seam's own log rather than the terminal payload's `reason` slot, which
    # `_technical_failure` fills only for faults it raises itself.)
    assert db.execute(
        "SELECT count(*) FROM formula_authoring_trace_event "
        "WHERE authoring_run_id = %s AND kind = 'failed'",
        ("far-mismatch-32",)).fetchone()[0] == 1


def test_the_ORCHESTRATOR_COMPARISON_is_the_guard_for_UNVALIDATED_material():
    """The SECOND guard, asked directly.

    It exists because the wire schema does not run on every path: replayed material, the
    deterministic producer and imports all reach the orchestrator with a proposal nothing
    validated. This asserts the comparison is present and reads the run's REQUESTED schema rather
    than accepting whatever arrived — the property that was missing, and that no amount of response
    schema can supply for those paths.
    """
    import inspect

    from featuregen.formula import replay_authoring_v2

    source = inspect.getsource(replay_authoring_v2)
    assert "FORMULA_SCHEMA_CONTRACT_MISMATCH" in source
    assert "proposal.formula_schema_version != formula_schema_version" in source


def test_a_MISMATCHED_RUN_LEAVES_NOTHING_ADMISSIBLE(db):
    """The THIRD guard's precondition: a run refused during authoring writes no terminal
    `completed` event, so `_terminal_event` refuses before any artifact is inspected — the run
    cannot be admitted because there is nothing to admit."""
    _run(db, run_id="far-mismatch-adm", raw=_raw(), requested=3)

    assert db.execute(
        "SELECT count(*) FROM formula_authoring_trace_event "
        "WHERE authoring_run_id = %s AND kind = 'completed'",
        ("far-mismatch-adm",)).fetchone()[0] == 0

    # The terminal that DOES exist records TECHNICAL_FAILURE, which is neither RESOLVED nor the V3
    # deferral — so `_require_admissible_disposition` refuses it, and no artifact accompanies it to
    # be admitted in the first place.
    disposition = db.execute(
        "SELECT payload->'result'->>'authoring_disposition' FROM formula_authoring_trace_event "
        "WHERE authoring_run_id = %s AND kind = 'failed'",
        ("far-mismatch-adm",)).fetchone()[0]
    assert disposition == "TECHNICAL_FAILURE"


# ══ THE THIRD GUARD, DRIVEN THROUGH REAL ADMISSION ═════════════════════════════════════════════
def _relabelled(db, *, source_run: str, run_id: str, declared_schema: int | None) -> str:
    """A run whose MANIFEST says one language and whose TRACE holds another.

    Authoring refuses to produce this now, which is the point: the admission guard exists for
    material that did NOT come through today's authoring path — traces written before the contract
    fix, and imports. So it is built the way an import would arrive: a run row opened with the
    declared manifest, and the terminal bytes of a genuine run of the OTHER language copied under
    it. The payload digest covers the payload alone, so the copied event verifies exactly as the
    original did — which is what makes this a test of the version guard rather than of tampering.
    """
    from featuregen.formula.replay_trace import open_authoring_run

    manifest = db.execute(
        "SELECT intent_hash, versions FROM formula_authoring_run WHERE authoring_run_id = %s",
        (source_run,)).fetchone()
    versions = dict(manifest[1])
    # `None` opens the run with NO declared schema — an import whose manifest is incomplete. It is
    # constructed this way rather than stripped afterwards because `formula_authoring_run` is
    # write-once by trigger, which is itself the reason this guard cannot rely on repair.
    if declared_schema is None:
        versions.pop("formula_schema", None)
    else:
        versions["formula_schema"] = declared_schema
    open_authoring_run(db, intent_hash=manifest[0], versions=versions, actor=None,
                       authoring_run_id=run_id)
    db.execute(
        "INSERT INTO formula_authoring_trace_event (authoring_run_id, seq, kind, stage, payload, "
        "payload_hash, canonical_output_hash, idempotency_key, logical_turn_index, llm_call_ref) "
        "SELECT %s, seq, kind, stage, payload, payload_hash, canonical_output_hash, "
        "       %s || ':' || idempotency_key, logical_turn_index, llm_call_ref "
        "  FROM formula_authoring_trace_event WHERE authoring_run_id = %s",
        (run_id, run_id, source_run))
    return run_id


def _admit(db, run_id: str, result):
    from tests.featuregen.materialize.test_admission_v2_s13 import _INTENT, _advertise

    from featuregen.materialize.admission_v2 import ResolvedFeatureInputV2, admit_artifacts_v2

    _advertise(db)
    swapped = dataclasses.replace(result, authoring_run_id=run_id)
    return admit_artifacts_v2(
        db, [ResolvedFeatureInputV2(_INTENT, swapped)], engine_id="kedro-pyspark")


def test_a_MANIFEST_3_RUN_CARRYING_A_V2_PROPOSAL_IS_REFUSED(db):
    """The pre-fix shape, through REAL `admit_artifacts_v2`: manifest says 3, the trace holds a
    genuine v2 proposal. Exactly what every "v3" draft authored before the contract fix looks
    like."""
    from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

    real = _run(db, run_id="far-real-v2", raw=_raw(), requested=2)
    assert real.authoring_disposition == "RESOLVED", "the control must be admissible as v2"
    _relabelled(db, source_run="far-real-v2", run_id="far-mislabelled-3", declared_schema=3)

    with pytest.raises(MaterializationRefused) as refusal:
        _admit(db, "far-mislabelled-3", real)

    assert refusal.value.code is CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED
    assert "formula_schema=3" in str(refusal.value)


def test_a_MANIFEST_2_RUN_CARRYING_A_V3_PROPOSAL_IS_REFUSED(db):
    """The other direction, also through real admission. The guard is an AGREEMENT, not a floor
    that only stops downgrades."""
    from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

    real = _run(db, run_id="far-real-v3", raw=_raw_v3(), requested=3)
    assert real.authoring_disposition == "READY_FOR_OUTPUT_BINDING"
    _relabelled(db, source_run="far-real-v3", run_id="far-mislabelled-2", declared_schema=2)

    with pytest.raises(MaterializationRefused) as refusal:
        _admit(db, "far-mislabelled-2", real)

    assert refusal.value.code is CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED


def test_a_RUN_WHOSE_MANIFEST_DECLARES_NO_SCHEMA_is_INCOMPLETE(db):
    """A run that states nothing about what decided it cannot be shown to agree with anything — so
    it is refused as INCOMPLETE rather than waved through."""
    from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

    real = _run(db, run_id="far-real-noschema", raw=_raw_v3(), requested=3)
    _relabelled(db, source_run="far-real-noschema", run_id="far-noschema", declared_schema=None)

    with pytest.raises(MaterializationRefused) as refusal:
        _admit(db, "far-noschema", real)

    assert refusal.value.code is CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE


def test_a_MATCHING_V3_RUN_IS_STILL_ADMITTED(db):
    """The positive control. Without it every refusal above could be passing because admission
    refuses everything."""
    real = _run(db, run_id="far-good-v3", raw=_raw_v3(), requested=3)

    admitted = _admit(db, "far-good-v3", real)

    assert len(admitted) == 1
    assert admitted[0].proposal.formula_schema_version == 3


def test_a_FORGED_V2_OBJECT_CLAIMING_VERSION_3_IS_CAUGHT_BY_ITS_TYPE():
    """The declared field and the runtime type are SEPARATELY forgeable. `parse_versioned`
    dispatches on the field, so an object whose field says 3 while its class is
    `TypedFormulaProposalV2` satisfies an integer comparison and is then read as v2 by every stage
    below — which is why the guard compares the type as well.

    Asked of the check directly: constructing this pair is the whole point, and no writer in the
    platform can produce it.
    """
    from featuregen.formula.parse_v2 import parse_proposal_v2
    from featuregen.materialize.admission_v2 import _verify_manifest_declares_the_language
    from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

    forged = dataclasses.replace(parse_proposal_v2(_raw()), formula_schema_version=3)
    assert forged.formula_schema_version == 3, "the integer comparison alone would pass"

    class _Conn:
        def execute(self, *_a, **_k):
            return type("R", (), {"fetchone": lambda self: ({"formula_schema": 3},)})()

    with pytest.raises(MaterializationRefused) as refusal:
        _verify_manifest_declares_the_language(_Conn(), forged, "far-forged")

    assert refusal.value.code is CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED
    assert "separately forgeable" in str(refusal.value)


# ══ NO PRE-FIX "V3" EVIDENCE CAN QUALIFY ═══════════════════════════════════════════════════════
def test_a_PRE_FIX_V3_LABELLED_RUN_DOES_NOT_QUALIFY_as_v3_evidence():
    """The evaluator must exclude the old runs by IDENTITY, never by commit date.

    A pre-fix run declares `formula_schema: 3` — it is indistinguishable from real V3 evidence on
    that axis alone, which is exactly how it came to be mislabelled. What it cannot fake is the
    rest of the tuple: the orchestrator and disposition versions moved when the V3 state was added.
    """
    from featuregen.formula.authoring_versions import qualifies_as_v3_evidence

    pre_fix = {"formula_schema": 3, "orchestrator": 2, "disposition": 2,
               "canonicalization": 1, "output_policy": 1}

    ok, disagreeing = qualifies_as_v3_evidence(pre_fix)
    assert ok is False
    # It NAMES the axes, because "not V3 evidence" and "not V3 evidence because its orchestrator is
    # 2" send an operator to different places.
    assert any("orchestrator" in axis for axis in disagreeing)
    assert any("disposition" in axis for axis in disagreeing)


def test_a_GENUINE_V3_RUN_QUALIFIES(db):
    """Asserted against a REAL manifest this build wrote, not a hand-written dict — otherwise the
    predicate could agree with a tuple nothing produces."""
    from featuregen.formula.authoring_versions import qualifies_as_v3_evidence

    _run(db, run_id="far-qualifies", raw=_raw_v3(), requested=3)
    stored = db.execute(
        "SELECT versions FROM formula_authoring_run WHERE authoring_run_id = %s",
        ("far-qualifies",)).fetchone()[0]

    assert qualifies_as_v3_evidence(stored) == (True, ())


def test_a_V2_RUN_DOES_NOT_QUALIFY(db):
    from featuregen.formula.authoring_versions import qualifies_as_v3_evidence

    _run(db, run_id="far-v2-nonqual", raw=_raw(), requested=2)
    stored = db.execute(
        "SELECT versions FROM formula_authoring_run WHERE authoring_run_id = %s",
        ("far-v2-nonqual",)).fetchone()[0]

    ok, disagreeing = qualifies_as_v3_evidence(stored)
    assert ok is False
    assert any("formula_schema" in axis for axis in disagreeing)


def test_a_MISLABELLED_RUN_IS_FINDABLE_by_the_cleanup_query(db):
    """The development-data cleanup needs to IDENTIFY the mislabelled drafts before anything is
    reset. This is that query, run against a mislabelled row built the way an import arrives.

    It reads the manifest and the stored proposal SEPARATELY and compares them, because that is the
    disagreement — a query keyed on the manifest alone would return every V3 draft, and one keyed on
    the proposal alone would return every V2 draft.
    """
    real = _run(db, run_id="far-clean-src", raw=_raw(), requested=2)
    assert real.authoring_disposition == "RESOLVED"
    _relabelled(db, source_run="far-clean-src", run_id="far-clean-bad", declared_schema=3)

    mismatched = db.execute(
        "SELECT r.authoring_run_id, r.versions->>'formula_schema' AS declared, "
        "       e.payload->'result'->'candidate_proposal'->>'formula_schema_version' AS produced "
        "  FROM formula_authoring_run r "
        "  JOIN formula_authoring_trace_event e ON e.authoring_run_id = r.authoring_run_id "
        " WHERE e.kind = 'completed' "
        "   AND e.payload->'result'->'candidate_proposal' IS NOT NULL "
        "   AND r.versions->>'formula_schema' IS DISTINCT FROM "
        "       (e.payload->'result'->'candidate_proposal'->>'formula_schema_version')"
    ).fetchall()

    found = {row[0] for row in mismatched}
    assert "far-clean-bad" in found, "the mislabelled run must be findable"
    assert "far-clean-src" not in found, "a consistent run must not be swept up"
