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

from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.policy_payloads import (
    DirectionPayloadV1,
    EligibleStatusPayloadV1,
    PolicyReadBasisV1,
    record_payload,
)
from featuregen.formula.schema_leaves import AdditivityClass
from featuregen.formula.schema_v2 import FinalOperationV2
from featuregen.materialize.boundary_v2 import KnowledgeTimeBasisV2
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
        spine=kwargs.pop("spine", object()), inventory=kwargs.pop("inventory", None),
        output_policy=kwargs.pop("output_policy", _OUTPUT), **kwargs)


#: A resolved output policy, supplied because the compiler REQUIRES one: `expected_output` is what
#: the author expected and output authority is what the operands permit, and the compiler takes the
#: second. Every test here asserts a decision made before the output step, so its content is not
#: what is under test — its presence is.
_OUTPUT = FormulaOutputPolicyV2(
    output_type="decimal", unit="monetary", currency="fixed:AED",
    output_additivity=AdditivityClass.NON_ADDITIVE, external_type_required=False)


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


def test_a_policy_with_STORED_CONTENT_BECOMES_A_PHYSICAL_READ(catalog):
    """The round trip Gate 2 needs: a governed decision becomes the COLUMN it causes to be read.

    This is why resolution belongs at compile time. A policy is applied by reading its columns, and
    which columns those are lives only in the payload — so until it is resolved the read set is
    incomplete, and Gate 2 would authorize a compilation narrower than the run performs.

    Asserted on the derivation rather than on a full compile: reaching the IR also requires physical
    resolution against a seeded cluster inventory, which is step 9's pilot, and a test that had to
    seed one to prove "the policy became a read" would mostly be testing the seeding.
    """
    _store_status_policy(catalog, "pr-1", "status:posted-only")
    reads = _reads_for(catalog, "status:posted-only", {"status:posted-only": "pr-1"})

    assert not isinstance(reads, MaterializationRefused), reads
    assert [(r.policy_ref, r.role, r.logical_ref) for r in reads] == [
        ("status:posted-only", "status", "authored::public.txns.status_cd")]


def test_the_READ_BASIS_TRAVELS_from_the_payload_to_the_read(catalog):
    """The one fact that decides whether a policy leaks, carried unchanged to the gate that reads it.

    A status column updated in place reads as it is NOW, so a feature filtering on "was POSTED"
    learns an answer its cutoff could not have known. The payload is the only thing that knows, and
    the leakage gate is the only thing that acts on it — anything lost between the two makes the
    gate decide on an assumption.
    """
    _store_status_policy(catalog, "pr-1", "status:posted-only",
                         basis=PolicyReadBasisV1.LATEST_AVAILABLE)
    reads = _reads_for(catalog, "status:posted-only", {"status:posted-only": "pr-1"})

    assert not isinstance(reads, MaterializationRefused), reads
    assert reads[0].temporal.basis is KnowledgeTimeBasisV2.LATEST_AVAILABLE


def test_the_TWO_BASIS_ENUMS_ARE_ONE_VOCABULARY():
    """`formula` must not import `materialize`, so the basis is spelled twice. Two spellings of one
    vocabulary drift the first time either side gains a member, and a basis the compiler cannot map
    would raise where a governed refusal belongs."""
    assert ({b.value for b in PolicyReadBasisV1}
            == {b.value for b in KnowledgeTimeBasisV2})


def test_a_realization_holding_THE_WRONG_SHAPE_refuses(catalog):
    """A status ref that resolves to a direction payload is not a near-miss: it would read the
    direction column and filter on values that mean something else entirely, producing a plausible
    number from a policy the formula never declared."""
    content = record_payload(
        catalog,
        DirectionPayloadV1(direction_column_ref="authored::public.txns.dr_cr",
                           debit_values=("D",), credit_values=("C",),
                           read_basis=PolicyReadBasisV1.EVENT_TIME),
        recorded_by="user:ops")
    _realization(catalog, "pr-1", "status:posted-only", content)
    reads = _reads_for(catalog, "status:posted-only", {"status:posted-only": "pr-1"})

    assert isinstance(reads, MaterializationRefused)
    assert reads.code is CompilationRefusalCode.POLICY_REFERENCE_UNRESOLVABLE
    assert "different question" in reads.detail


# ══ WHAT THE IR CARRIES ════════════════════════════════════════════════════════════════════════
def test_ROW_SELECTIONS_ARE_CARRIED_FROM_THE_EXPRESSION(catalog):
    """V3 puts row selections on the EXPRESSION. An earlier draft of the compiler read them off the
    proposal — a field that does not exist there — so it produced an empty tuple for every formula,
    and a semantically filtered feature compiled to an unfiltered one whose only symptom was its
    numbers."""
    from featuregen.formula.parse_v3 import parse_proposal_v3
    from featuregen.formula.schema_v3 import FORMULA_SCHEMA_VERSION_V3
    from featuregen.materialize.compile_ir_v2 import _row_selections

    # The policy ref is not decoration here: V3 refuses a selection that declares intent with no
    # reference to resolve it, because "wants debit rows" without a direction policy is a filter
    # nothing can apply.
    raw = _raw(body={"final_operation": "identity", "expr": _expr(
        authority_refs={"direction_policy_ref": "direction:dr-cr"},
        row_selections=[{"kind": "transaction_direction", "role": "direction",
                         "semantic_value": "debit"}])})
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    proposal = parse_proposal_v3(raw)
    carried = _row_selections(("body.expr",), tuple(_body_expressions(proposal)))

    assert [s.expr_path for s in carried] == ["body.expr"]
    assert carried[0].selections[0].semantic_value == "debit"


def test_an_expression_with_NO_SELECTIONS_gets_NO_ENTRY(catalog):
    """`SelectedRowsV2` refuses an empty tuple precisely so "selects nothing" and "the selection was
    dropped on the way here" cannot look alike — so the compiler must omit, never emit empty."""
    from featuregen.materialize.compile_ir_v2 import _row_selections

    assert _row_selections(("body.expr",), tuple(_body_expressions(_v2_admitted().proposal))) == ()


def test_THE_DECLARATION_IS_WHAT_THE_IR_CARRIES_not_the_resolved_content(catalog):
    """Identity-bearing: a formula with a reversal policy is a different formula from one without.

    The resolved CONTENT never enters the IR. Baking one environment's realization into a feature's
    identity would make the same governed formula two different features in two environments, and
    re-pointing a realization would silently re-identify every feature that used it.
    """
    from featuregen.materialize.compile_ir_v2 import _declared_policies

    declared = _declared_policies(
        ("body.expr",),
        tuple(_body_expressions(_with_status_policy(_v2_admitted(), "status:x").proposal)))

    assert [(d.expr_path, d.declared_refs()) for d in declared] == [
        ("body.expr", (("status", "status:x"),))]


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


def _body_expressions(proposal):
    from featuregen.formula.schema_v2 import body_expressions_v2

    return body_expressions_v2(proposal.body)


def _realization(conn, revision_id: str, policy_ref: str, content_hash: str) -> None:
    conn.execute(
        "INSERT INTO policy_realization_revision (revision_id, family_key_hash, policy_kind, "
        "policy_ref, bound_dataset, environment_id, semantic_role, executable_content_hash, "
        "cas_pointer, provenance) VALUES (%s,'fam','status',%s,'ds','env','status',%s,'cas',"
        "'source_derived')", (revision_id, policy_ref, content_hash))


def _store_status_policy(conn, revision_id: str, policy_ref: str,
                         basis: PolicyReadBasisV1 = PolicyReadBasisV1.EVENT_TIME) -> None:
    content = record_payload(
        conn,
        EligibleStatusPayloadV1(status_column_ref="authored::public.txns.status_cd",
                                eligible_values=("POSTED", "SETTLED"), read_basis=basis),
        recorded_by="user:ops")
    _realization(conn, revision_id, policy_ref, content)


def _reads_for(conn, ref: str, realization_ids):
    """The physical reads one declared status policy causes, through the compiler's own derivation."""
    from featuregen.materialize.compile_ir_v2 import _declared_policies, _policy_reads

    declared = _declared_policies(
        ("body.expr",),
        tuple(_body_expressions(_with_status_policy(_v2_admitted(), ref).proposal)))
    return _policy_reads(conn, declared, realization_ids)
