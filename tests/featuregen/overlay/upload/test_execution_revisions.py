"""C-B3/C-B4/C-B5 — executable feature, derived group, authorization envelope.

Three gates: *"constructible from hashes alone"*, *"membership is queryable without replaying a
request"*, and *"an artifact authorized for one target cannot be reused for another"*.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.materialize.boundary_v2 import CompilationIdentityV2
from featuregen.overlay.upload.execution_revisions import (
    DerivedGroupRevisionV2,
    ExecutableFeatureRevisionV2,
    GenerationAuthorizationRevisionV2,
)

IR_A, IR_B = "sha256:ir-a", "sha256:ir-b"


def _member(name: str = "posted_debit_amount_30d", ir_hash: str = IR_A):
    return ExecutableFeatureRevisionV2(
        revision_id=f"efr-{name}", feature_name=name, selection_revision_id="fsr-1",
        bound_formula_hash="sha256:bound", executable_output_hash="sha256:output",
        ir_hash=ir_hash)


def _identity(ir_hashes=(IR_A,)) -> CompilationIdentityV2:
    return CompilationIdentityV2(
        formula_content_hashes=tuple(f"sha256:f{i}" for i in range(len(ir_hashes))),
        ir_hashes=tuple(ir_hashes), materialization_contract_hash="sha256:contract",
        group_plan_hash="sha256:plan")


def _group(**overrides) -> DerivedGroupRevisionV2:
    kwargs = dict(revision_id="dgr-1", build_set_revision_id="bsr-1",
                  member_revision_ids=("efr-1",), materialization_contract_hash="sha256:contract",
                  group_plan_hash="sha256:plan", compilation_identity=_identity())
    kwargs.update(overrides)
    return DerivedGroupRevisionV2(**kwargs)


def _authorization(**overrides) -> GenerationAuthorizationRevisionV2:
    kwargs = dict(revision_id="gar-1", derived_group_revision_id="dgr-1",
                  member_selection_revision_ids=("fsr-1", "fsr-2"),
                  target_reading_revision_id="trr-1", leakage_policy_version=1,
                  leakage_verdict="admitted", screened_ir_hashes=(IR_A, IR_B),
                  gate2_token_ir_hashes=(IR_A, IR_B))
    kwargs.update(overrides)
    return GenerationAuthorizationRevisionV2(**kwargs)


# ══ C-B3 — constructible from HASHES alone ═══════════════════════════════════════════════════════
def test_AN_EXECUTABLE_FEATURE_IS_CONSTRUCTIBLE_FROM_STRINGS():
    """The gate. A revision that held the objects could only be constructed once S5 produced them,
    and the point of freezing the identity graph early is that the shape stops moving first."""
    assert all(f.type == "str" for f in dataclasses.fields(ExecutableFeatureRevisionV2))
    assert _member().content_hash


@pytest.mark.parametrize("blank", ["revision_id", "feature_name", "selection_revision_id",
                                   "bound_formula_hash", "executable_output_hash", "ir_hash"])
def test_every_part_is_required(blank):
    with pytest.raises(ValueError, match="unpinnable to either"):
        dataclasses.replace(_member(), **{blank: "  "})


def test_it_pins_BOTH_what_executes_and_what_was_chosen():
    payload = _member().identity_payload()
    assert payload["bound_formula_hash"] and payload["executable_output_hash"]
    assert payload["selection_revision_id"] == "fsr-1"


def test_the_revision_id_is_not_in_the_content_hash():
    """The id names the row; the hash names what it says."""
    assert "revision_id" not in _member().identity_payload()


# ══ C-B4 — the first durable group→member map ════════════════════════════════════════════════════
def test_MEMBERSHIP_IS_QUERYABLE_FROM_THE_RECORD_ALONE():
    """The gate, and the thing that does not exist today: `compile_feature_group` builds membership
    in memory and forgets it, so "which features are in this group" needs the request replayed."""
    group = _group(member_revision_ids=("efr-1", "efr-2"), compilation_identity=_identity(
        (IR_A, IR_B)))
    assert group.member_revision_ids == ("efr-1", "efr-2")
    assert group.build_set_revision_id == "bsr-1"


def test_a_group_and_its_compilation_cannot_disagree_about_MEMBER_COUNT():
    """The smaller number is the one that gets published."""
    with pytest.raises(ValueError, match="disagree about how many features"):
        _group(member_revision_ids=("efr-1", "efr-2"), compilation_identity=_identity((IR_A,)))


def test_a_duplicate_member_is_refused():
    with pytest.raises(ValueError, match="double-count in every coverage answer"):
        _group(member_revision_ids=("efr-1", "efr-1"), compilation_identity=_identity(
            (IR_A, IR_B)))


def test_a_group_with_no_members_is_refused():
    with pytest.raises(ValueError, match="publishes nothing"):
        _group(member_revision_ids=())


def test_the_group_carries_contract_plan_AND_compilation_identity():
    """C-B4's deliverable, in one record."""
    names = {f.name for f in dataclasses.fields(DerivedGroupRevisionV2)}
    assert {"member_revision_ids", "materialization_contract_hash", "group_plan_hash",
            "compilation_identity"} <= names


# ══ C-B5 — the envelope binds EVERY member ═══════════════════════════════════════════════════════
def test_the_envelope_binds_EVERY_member_selection():
    """Revision 18 named one, which would authorize a group by pointing at a single feature's
    selection — and each feature was chosen separately."""
    assert _authorization().member_selection_revision_ids == ("fsr-1", "fsr-2")
    with pytest.raises(ValueError, match="without recording what anyone chose"):
        _authorization(member_selection_revision_ids=())


def test_AN_ARTIFACT_AUTHORIZED_FOR_ONE_TARGET_CANNOT_BE_REUSED_FOR_ANOTHER():
    """C-B5's S6 gate, asked in one place rather than re-written at each call site."""
    envelope = _authorization(target_reading_revision_id="trr-1")
    assert envelope.authorizes_target("trr-1")
    assert not envelope.authorizes_target("trr-2")


def test_an_envelope_with_no_target_reading_is_refused():
    with pytest.raises(ValueError, match="could be reused for another"):
        _authorization(target_reading_revision_id="  ")


def test_SCREENED_AND_AUTHORIZED_IR_SETS_MUST_MATCH():
    """Otherwise the group ships a feature nobody screened and the envelope still reads authorized."""
    with pytest.raises(ValueError, match="UNSCREENED"):
        _authorization(screened_ir_hashes=(IR_A,), gate2_token_ir_hashes=(IR_A, IR_B))
    with pytest.raises(ValueError, match="SCREENED-BUT-UNAUTHORIZED"):
        _authorization(screened_ir_hashes=(IR_A, IR_B), gate2_token_ir_hashes=(IR_A,))


def test_the_verdict_and_the_policy_version_are_both_recorded():
    """An envelope recording that a gate ran without recording what it decided is not evidence."""
    payload = _authorization().identity_payload()
    assert payload["leakage_verdict"] == "admitted"
    assert payload["leakage_policy_version"] == 1
    with pytest.raises(ValueError, match="without recording what it decided"):
        _authorization(leakage_verdict=" ")


def test_an_envelope_screening_nothing_is_refused():
    with pytest.raises(ValueError, match="verdict over nothing"):
        _authorization(screened_ir_hashes=(), gate2_token_ir_hashes=())


def test_ir_hash_ORDER_does_not_change_the_envelopes_identity():
    """The two lists are sets of features; which order a caller assembled them in is not a fact
    about what was authorized."""
    assert _authorization(screened_ir_hashes=(IR_A, IR_B),
                          gate2_token_ir_hashes=(IR_B, IR_A)).content_hash == _authorization(
        screened_ir_hashes=(IR_B, IR_A), gate2_token_ir_hashes=(IR_A, IR_B)).content_hash


# ══ the chain composes with no forward reference ═════════════════════════════════════════════════
def test_the_whole_chain_is_expressible_today():
    """build set -> selections -> executable features -> derived group -> authorization, with every
    link a hash or an id and nothing requiring an object S5 has not produced."""
    group = _group(member_revision_ids=("efr-1", "efr-2"),
                   compilation_identity=_identity((IR_A, IR_B)))
    envelope = _authorization(derived_group_revision_id=group.revision_id)
    assert envelope.derived_group_revision_id == group.revision_id
    assert envelope.content_hash and group.content_hash and _member().content_hash
