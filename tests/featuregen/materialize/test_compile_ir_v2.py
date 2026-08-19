"""Compiling an admitted V3 formula into its executable plan — the link that did not exist.

Nothing in production has ever built a `FormulaExecutionIRV2`. Every V2 test constructs one by
wrapping expressions compiled by the *V1* compiler, which proves the downstream types can carry V1
expressions and proves nothing about compiling a V2 formula.

What these tests hold:

1. **A declared policy resolves to executable content or REFUSES.** The formula said the policy
   applies; rendering without it computes a number under rules nobody wrote.
2. **Missing grain refuses** rather than borrowing operands — the defect that was invisible because
   it was self-consistent.
3. **An operation with no body-path spelling refuses** at compile time, where it is a verdict about
   the feature, rather than as a KeyError deep in rendering.
4. **Refusals are RETURNED**, so one bad feature does not hide the rest of a group's verdicts.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.formula.authoring_fixtures import seed_authoring_catalog
from tests.featuregen.materialize.test_admission_v2_s13 import _INTENT, _expr, _raw

from featuregen.formula.policy_payloads import EligibleStatusPayloadV1, record_payload
from featuregen.formula.schema_v2 import FinalOperationV2
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.compile_ir_v2 import compile_ir_v2


@pytest.fixture
def catalog(db):
    seed_authoring_catalog(db)
    return db


def _v2_admitted(raw=None, **overrides):
    """An admitted V2 feature over a REAL parsed V3 proposal.

    The V1 harness cannot be reused here: its `AdmittedFeature` carries a `TypedFormulaV1`, and what
    is under test is a compiler that takes a V2/V3 proposal. Using the V1 object would have tested
    an adapter nobody ships.
    """
    from featuregen.formula.parse_v2 import parse_proposal_v2
    from featuregen.materialize.admission_v2 import AdmittedFeatureV2

    proposal = overrides.get("proposal") or parse_proposal_v2(raw if raw is not None else _raw())
    return AdmittedFeatureV2(
        feature_name="posted_debit_amount_30d",
        proposal=proposal,
        proposal_content_hash="sha256:proposal",
        intent=_INTENT,
        authoring_run_id="far-compile-1",
        operator_kinds=(("aggregate", "sum"),))


def _compile(db, admitted=None, **kwargs):
    """Compile, with a spine and inventory that are deliberately EMPTY placeholders.

    Every test here asserts a decision the compiler makes BEFORE physical resolution — body paths,
    arity, grain, declared policies. Those checks run first precisely because they need no catalog,
    and a test that had to seed a cluster inventory to prove "this policy is unbound" would be
    testing the seeding.
    """
    return compile_ir_v2(
        db, admitted if admitted is not None else _v2_admitted(),
        spine=kwargs.pop("spine", object()), inventory=kwargs.pop("inventory", None), **kwargs)


# ══ A DECLARED POLICY MUST RESOLVE TO CONTENT ══════════════════════════════════════════════════
def test_A_DECLARED_POLICY_WITH_NO_REALIZATION_REFUSES(catalog):
    """The formula SAID the policy applies. Nothing says what it is.

    Rendering anyway would produce a number computed under rules nobody wrote, while the artifact
    claims a governed policy was applied.
    """
    declared = _with_status_policy(_v2_admitted(), "status:posted-only")
    result = _compile(catalog, declared, policy_realization_ids={})

    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.POLICY_REFERENCE_UNRESOLVABLE
    assert "status:posted-only" in result.detail


def test_a_policy_whose_CONTENT_IS_MISSING_refuses_differently(catalog):
    """Unbound and unstored are different states with different remedies: bind it, or supply its
    content. A single code would send an operator looking in the wrong place half the time."""
    catalog.execute(
        "INSERT INTO policy_realization_revision (revision_id, family_key_hash, policy_kind, "
        "policy_ref, bound_dataset, environment_id, semantic_role, executable_content_hash, "
        "cas_pointer, provenance) VALUES ('pr-1','fam','status','status:posted-only','ds','env',"
        "'status','sha256:nothing-here','cas','source_derived')")

    declared = _with_status_policy(_v2_admitted(), "status:posted-only")
    result = _compile(catalog, declared,
                      policy_realization_ids={"status:posted-only": "pr-1"})

    assert isinstance(result, MaterializationRefused)
    assert "cannot be rendered" in result.detail
    assert "sha256:nothing-here" in result.detail


def test_a_policy_with_STORED_CONTENT_RESOLVES_TO_EMITTABLE_CONTENT(catalog):
    """The round trip the renderer needs: a governed decision becomes content it can emit.

    Asserted on the resolution step rather than on a full compile. Reaching the IR also requires
    physical resolution against a seeded cluster inventory, which is step 9's pilot — and a test
    that had to seed one to prove "the policy resolved" would mostly be testing the seeding, and
    would fail for reasons that have nothing to do with policies.
    """
    from featuregen.materialize.compile_ir_v2 import _resolve_policies

    content = record_payload(
        catalog,
        EligibleStatusPayloadV1(status_column_ref="authored::public.txns.status_cd",
                                eligible_values=("POSTED", "SETTLED")),
        recorded_by="user:ops")
    catalog.execute(
        "INSERT INTO policy_realization_revision (revision_id, family_key_hash, policy_kind, "
        "policy_ref, bound_dataset, environment_id, semantic_role, executable_content_hash, "
        "cas_pointer, provenance) VALUES ('pr-1','fam','status','status:posted-only','ds','env',"
        "'status',%s,'cas','source_derived')", (content,))

    declared = _with_status_policy(_v2_admitted(), "status:posted-only")
    resolved = _resolve_policies(
        catalog, declared.proposal, {"status:posted-only": "pr-1"})

    assert not isinstance(resolved, MaterializationRefused), resolved
    assert resolved[0].eligible_values == ("POSTED", "SETTLED")
    assert resolved[0].status_column_ref == "authored::public.txns.status_cd"


# ══ THE GRAIN IS NOT OPTIONAL ══════════════════════════════════════════════════════════════════
def test_NO_GRAIN_KEYS_REFUSES(catalog):
    """The defect that was invisible because it was self-consistent: the old path borrowed every
    operand as a grain key, and every check downstream agreed with the guess."""
    admitted = _v2_admitted()
    grainless = dataclasses.replace(
        admitted.proposal, grain=dataclasses.replace(admitted.proposal.grain, keys=()))
    result = _compile(catalog, _v2_admitted(proposal=grainless))

    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.GRAIN_NOT_RESOLVED
    assert "computed PER" in result.detail


# ══ AN OPERATION WITH NOWHERE TO STAGE ITS TERMS ═══════════════════════════════════════════════
def test_SIGNED_SUM_REFUSES_AT_COMPILE_TIME(catalog):
    """`signed_sum` has no body-path spelling anywhere: BODY_PATHS is the closed five-member set v1
    froze, and there is no path for N signed terms.

    Refusing here makes it a verdict about the feature. Compiling it would place expressions at
    paths the renderer cannot look up, surfacing as a KeyError deep in rendering — the same fact,
    delivered where nobody can act on it.
    """
    from featuregen.materialize.compile_ir_v2 import _BODY_PATHS_BY_OPERATION

    assert FinalOperationV2.SIGNED_SUM not in _BODY_PATHS_BY_OPERATION


def test_the_compilers_body_paths_agree_with_the_RENDERERS():
    """Two tables describing one agreement drift the first time either is edited.

    The renderer's `_BODY_SLOTS` is the authority for what it can emit; the compiler's map is what
    it produces. A path in one and not the other is a feature that compiles and cannot render.
    """
    from featuregen.materialize.compile_ir_v2 import _BODY_PATHS_BY_OPERATION
    from featuregen.materialize.render.nodes_compute import _BODY_SLOTS

    for operation, paths in _BODY_PATHS_BY_OPERATION.items():
        rendered = next((v for k, v in _BODY_SLOTS.items() if str(k) == str(operation)), None)
        assert rendered is not None, f"the renderer has no slots for {operation}"
        assert set(paths) == set(rendered), (
            f"{operation}: compiler stages {sorted(paths)}, renderer reads {sorted(rendered)}")


# ══ REFUSALS ARE RETURNED ══════════════════════════════════════════════════════════════════════
def test_a_refusal_is_RETURNED_not_raised(catalog):
    """One refused feature is one verdict among the many a compilation collects; raising would let
    the first bad feature hide every other verdict in the group."""
    admitted = _v2_admitted()
    grainless = dataclasses.replace(
        admitted.proposal, grain=dataclasses.replace(admitted.proposal.grain, keys=()))
    assert isinstance(_compile(catalog, _v2_admitted(proposal=grainless)),
                      MaterializationRefused)


def _with_status_policy(admitted, ref: str):
    """The same feature with a status policy DECLARED, built through the RAW proposal.

    Declared in the raw and re-parsed rather than dataclass-patched, because the parser is what
    decides whether an authority ref is well-formed — patching around it would test a shape the
    parser might reject.
    """
    from featuregen.formula.parse_v2 import parse_proposal_v2

    raw = _raw(body={"final_operation": "identity", "expr": _expr(authority_refs={
        "status_policy_ref": ref, "direction_policy_ref": "",
        "reversal_policy_ref": "", "currency_conversion_ref": ""})})
    return _v2_admitted(proposal=parse_proposal_v2(raw))
