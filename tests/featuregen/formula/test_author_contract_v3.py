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


def test_ADMISSION_INDEPENDENTLY_COMPARES_manifest_declared_and_TYPE():
    """The THIRD guard itself. It protects traces written before any of this existed, corrupted
    imports and future orchestration mistakes — none of which the first two guards ran on.

    The runtime TYPE is compared as well as the declared integer because they are separately
    forgeable: `parse_versioned` dispatches on the declared field, so an object whose field says 3
    while its class is `TypedFormulaProposalV2` would satisfy an integer comparison and then be read
    by every downstream stage as v2.
    """
    import inspect

    from featuregen.materialize import admission_v2

    source = inspect.getsource(admission_v2._verify_manifest_declares_the_language)
    assert "formula_schema" in source
    assert "isinstance(proposal, expected)" in source
    assert "_verify_manifest_declares_the_language" in inspect.getsource(
        admission_v2._admit_one_v2)
