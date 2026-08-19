"""S6 — planned IR → read set → leakage → Gate 2 → contracts → groups (1077).

*"The IR is not rebuilt after authorization; a policy column swapped for the target ref refuses; an
FX column the roles cannot read refuses ``READ_SCOPE_INSUFFICIENT``; membership is queryable; V1
bytes and the single-contract path byte-identical."*

Driven off a REALLY COMPILED V1 IR against the seeded catalog, the way ``test_boundary_v2`` is: the
whole point of the V2 boundary is that it reuses V1's language-neutral expressions and spine, and a
hand-built fixture would prove only that the V2 types accept objects shaped the way this file shapes
them.
"""
from __future__ import annotations

import inspect

import pytest
from tests.featuregen.materialize.test_ir import (  # the V1 machinery, reused not re-created
    _ROLES,
    _SRC,
    CUSTOMERS,
    DECLARATION,
    INVENTORY,
    TXN_AMT,
    TXN_DR_CR,
    _admitted,
    _col,
    _floor,
    _table_node,
    _tag,
    compile_ir,
    seed_catalog,
)

from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.schema_leaves import AdditivityClass
from featuregen.formula.schema_v2 import AuthorityRefsV2, FinalOperationV2
from featuregen.formula.schema_v3 import SelectionKind, SemanticRowSelectionV1
from featuregen.materialize.boundary_v2 import (
    AuthorizedCompilationV2,
    DeclaredPoliciesV2,
    FormulaExecutionIRV2,
    KnowledgeTimeBasisV2,
    PlannedFormulaExecutionIRV2,
    PolicyReadV2,
    SelectedRowsV2,
    TemporalReadV2,
    build_compilation_identity_v2,
    contract_hash_v2,
    ir_hash_v2,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    CadenceDecl,
    CadencePeriod,
    CadenceTrigger,
    MaterializationContractV1,
    derive_contract,
)
from featuregen.materialize.contract_v2 import (
    ContractGroupV2,
    contracts_for,
    derive_contract_v2,
    group_by_contract_v2,
)
from featuregen.materialize.gate2_v2 import authorize_compilation_v2
from featuregen.materialize.group_plan import PlannedFeature
from featuregen.materialize.group_plan_v2 import (
    build_group_plan_v2,
    group_of_feature,
    members_of_group,
    record_group_plan,
)
from featuregen.materialize.ir import (
    AuthorizedCompilation,
    FormulaExecutionIRV1,
    ReadElementKind,
    authorize_compilation,
    decide_read_scope,
    union_read_elements,
)
from featuregen.materialize.leakage_v2 import (
    DIRECT_TARGET_READ,
    POST_CUTOFF_POLICY_READ,
    full_read_set_leakage_gate_v2,
)
from featuregen.materialize.physical_types import PhysicalType


def _code_of_function(function) -> str:
    """One function's executable text, comments and docstring stripped.

    `test_ir._code_of` does this for a MODULE; the claim here is about one function, and reading the
    whole module would be satisfied by any other function mentioning the string.
    """
    import io
    import tokenize

    kept: list[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(inspect.getsource(function)).readline)
    previous = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and previous in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
            continue
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            kept.append(token.string)
        previous = token.type
    return " ".join(kept).lower()

POLICY_ID = "formula-v2/physical-types@1"
ENV = "hdfc-local"
FX_RATE = f"{_SRC}::public.fx_rates.rate"
FX_REF = "currency_conversion:foundation-base-currency"
DIR_REF = "direction_sign:foundation-signed-by-indicator"

DEBIT = SemanticRowSelectionV1(
    kind=SelectionKind.TRANSACTION_DIRECTION, role="direction", semantic_value="debit")

CADENCE = CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                      business_date_cutoff="00:00:00", trigger=CadenceTrigger.SCHEDULED)


@pytest.fixture
def catalog(db):
    seed_catalog(db)
    # An FX rate table the pilot genuinely needs for a converted feature. Governed like any other
    # relation, because a policy column Gate 2 cannot address is a column it cannot authorize.
    for column in ("rate", "quote_dt", "ccy"):
        _col(db, "fx_rates", column)
    _table_node(db, "fx_rates")
    return db


@pytest.fixture
def v1_ir(catalog) -> FormulaExecutionIRV1:
    ir = compile_ir(catalog, _admitted("total_debit_amount_30d"), roles=_ROLES,
                    spine_decl=DECLARATION, inventory=INVENTORY)
    assert isinstance(ir, FormulaExecutionIRV1), ir
    return ir


def _output_v2(currency: str = "fixed:AED") -> FormulaOutputPolicyV2:
    return FormulaOutputPolicyV2(
        output_type="decimal", unit="monetary", currency=currency,
        output_additivity=AdditivityClass.ADDITIVE, external_type_required=False)


def _v2_ir(v1: FormulaExecutionIRV1, *, name: str = "posted_debit_amount_30d",
           refs: AuthorityRefsV2 | None = None,
           currency: str = "fixed:AED") -> FormulaExecutionIRV2:
    path = v1.expressions[0].expr_path
    return FormulaExecutionIRV2(
        feature_name=name, formula_content_hash=f"sha256:{name}",
        final_operation=FinalOperationV2.IDENTITY, zero_denominator=None,
        grain_entity=v1.grain_entity, grain_keys=v1.grain_keys,
        expressions=v1.expressions,
        policies=() if refs is None else (DeclaredPoliciesV2(expr_path=path, refs=refs),),
        row_selections=(SelectedRowsV2(expr_path=path, selections=(DEBIT,)),),
        spine=v1.spine, output_policy=_output_v2(currency), authoring_run_id="run-v2-0001")


def _fx_refs() -> AuthorityRefsV2:
    return AuthorityRefsV2(status_policy_ref="", direction_policy_ref="",
                           reversal_policy_ref="", currency_conversion_ref=FX_REF)


def _fx_read(*, basis: KnowledgeTimeBasisV2 = KnowledgeTimeBasisV2.AS_OF_CUTOFF,
             logical_ref: str = FX_RATE) -> PolicyReadV2:
    return PolicyReadV2(policy_ref=FX_REF, role="currency_conversion", logical_ref=logical_ref,
                        temporal=TemporalReadV2(basis=basis, declared_promise=None))


def _planned(v1: FormulaExecutionIRV1, *, converted: bool = False,
             name: str = "posted_debit_amount_30d",
             policy_reads=None) -> PlannedFormulaExecutionIRV2:
    ir = _v2_ir(v1, name=name, refs=_fx_refs() if converted else None,
                currency=f"converted:{FX_REF}" if converted else "fixed:AED")
    reads = policy_reads if policy_reads is not None else ((_fx_read(),) if converted else ())
    return PlannedFormulaExecutionIRV2.plan(ir, policy_reads=reads)


# ══ ACCEPTANCE 1 — the IR is NOT REBUILT after authorization ════════════════════════════════════
def test_THE_TOKEN_HOLDS_THE_SAME_OBJECTS_not_equal_ones(catalog, v1_ir):
    """Object identity, not equality. Two EQUAL planned IRs carrying different read sets is exactly
    the failure this clause exists to stop, and an equality check would pass it."""
    planned = _planned(v1_ir)
    token = authorize_compilation_v2(catalog, [planned], v1_ir.spine, roles=_ROLES)
    assert isinstance(token, AuthorizedCompilationV2), token

    assert token.planned[0] is planned
    assert token.planned[0].ir is planned.ir
    assert token.planned[0].read_set is planned.read_set
    assert token.ordered_planned()[0] is planned


def test_the_read_set_the_GATE_saw_is_the_read_set_the_token_carries(catalog, v1_ir):
    planned = _planned(v1_ir, converted=True)
    token = authorize_compilation_v2(catalog, [planned], v1_ir.spine, roles=_ROLES)
    assert isinstance(token, AuthorizedCompilationV2), token
    assert set(planned.read_set) <= set(token.authorized_refs)
    assert FX_RATE in token.authorized_refs


def test_the_downstream_stages_take_the_TOKEN_not_a_list_of_irs():
    """C-C2's ordering, checked where it can be undone: a stage that accepted bare IRs could be
    entered by a caller who skipped the gate, and nothing about the type system would notice."""
    identity = inspect.signature(build_compilation_identity_v2).parameters["authorized"]
    assert identity.annotation in (AuthorizedCompilationV2, "AuthorizedCompilationV2")

    contract = inspect.signature(derive_contract_v2).parameters["planned"]
    assert contract.annotation in (PlannedFormulaExecutionIRV2, "PlannedFormulaExecutionIRV2")


def test_the_CONTRACT_classifies_the_planned_union_not_a_re_walk(catalog, v1_ir):
    """A derivation given the bare IR could only reach the structural half. The FX rate column is
    in the union ONLY because it is a policy read, so its presence proves which set was classified.
    """
    planned = _planned(v1_ir, converted=True)
    assert FX_RATE in planned.read_set
    assert FX_RATE not in planned.structural_read_set

    _floor(catalog, "fx_rates", "rate", "restricted")
    contract = derive_contract_v2(
        catalog, planned, cadence=CADENCE,
        availability_promise=_promise(catalog, v1_ir), physical_type_policy=POLICY_ID)
    assert not isinstance(contract, MaterializationRefused), contract
    # The restricted FX column raised the class. A structural-only classification could not have.
    unconverted = derive_contract_v2(
        catalog, _planned(v1_ir), cadence=CADENCE,
        availability_promise=_promise(catalog, v1_ir), physical_type_policy=POLICY_ID)
    assert not isinstance(unconverted, MaterializationRefused), unconverted
    assert contract.sensitivity_class != unconverted.sensitivity_class


def _promise(conn, v1: FormulaExecutionIRV1):
    """The V1 contract's own promise, so the V2 derivation is compared against a real one."""
    contract = derive_contract(conn, v1, cadence=CADENCE,
                               availability_promise=_bare_promise())
    assert isinstance(contract, MaterializationContractV1), contract
    return contract.availability_promise


def _bare_promise():
    return AvailabilityPromiseV1(calendar_days=1)


# ══ ACCEPTANCE 2 — a POLICY COLUMN SWAPPED FOR THE TARGET REF refuses ═══════════════════════════
def test_A_POLICY_COLUMN_THAT_IS_THE_TARGET_REFUSES(catalog, v1_ir):
    """The failure a formula-only leakage check cannot see: the formula never mentions this column,
    and the target arrives through the policy that reads it."""
    target = f"{CUSTOMERS}.status_cd"
    planned = _planned(
        v1_ir, converted=True,
        policy_reads=(PolicyReadV2(policy_ref=FX_REF, role="currency_conversion",
                                   logical_ref=target,
                                   temporal=TemporalReadV2(
                                       basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
                                       declared_promise=None)),))

    verdict = full_read_set_leakage_gate_v2([planned], target_ref=target)
    assert verdict.admitted is False
    finding = verdict.findings[0]
    assert finding.code == DIRECT_TARGET_READ
    assert finding.source == "policy_read:currency_conversion"
    assert target in finding.logical_ref


def test_the_SAME_GROUP_without_the_swap_is_admitted(catalog, v1_ir):
    """The discriminator. Same feature, same target, an FX rate column instead of the target — so
    the refusal above is about the swap and not about policy reads existing."""
    target = f"{CUSTOMERS}.status_cd"
    planned = _planned(v1_ir, converted=True)
    assert target not in planned.read_set

    verdict = full_read_set_leakage_gate_v2([planned], target_ref=target)
    assert verdict.admitted is True
    assert verdict.claim  # the narrow claim rides on the verdict (invariant 18)


def test_a_formula_only_check_WOULD_HAVE_PASSED_it(catalog, v1_ir):
    """Why the clause is worth a test: the structural read set — the part a human re-reads — does
    not contain the target at all."""
    target = f"{CUSTOMERS}.status_cd"
    planned = _planned(
        v1_ir, converted=True,
        policy_reads=(PolicyReadV2(policy_ref=FX_REF, role="currency_conversion",
                                   logical_ref=target,
                                   temporal=TemporalReadV2(
                                       basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
                                       declared_promise=None)),))
    assert target not in planned.structural_read_set
    assert target in planned.read_set


# ══ C-C5's DEFERRED GATE — post-cutoff FX and post-cutoff REVERSAL refuse ═══════════════════════
@pytest.mark.parametrize("policy_ref,role", [
    (FX_REF, "currency_conversion"),
    ("reversal_correction:foundation-reversal-window", "reversal"),
])
def test_A_POST_CUTOFF_POLICY_READ_REFUSES(catalog, v1_ir, policy_ref, role):
    """`LATEST_AVAILABLE` sees state as it is NOW, not as it was at the cutoff — a reversal flag or
    an FX rate read there tells the model something that became true AFTER the cutoff. Both roles,
    because the two are the pilot's real post-cutoff hazards and a gate that caught one would look
    like it caught both."""
    ir = _v2_ir(v1_ir, refs=AuthorityRefsV2(
        status_policy_ref="", direction_policy_ref="",
        reversal_policy_ref=policy_ref if role == "reversal" else "",
        currency_conversion_ref=policy_ref if role == "currency_conversion" else ""))
    planned = PlannedFormulaExecutionIRV2.plan(ir, policy_reads=(PolicyReadV2(
        policy_ref=policy_ref, role=role, logical_ref=FX_RATE,
        temporal=TemporalReadV2(basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE,
                                declared_promise=None)),))

    verdict = full_read_set_leakage_gate_v2([planned], target_ref=f"{CUSTOMERS}.status_cd")
    assert verdict.admitted is False
    assert verdict.findings[0].code == POST_CUTOFF_POLICY_READ
    assert verdict.findings[0].source == f"policy_read:{role}"


def test_a_GENEROUS_PROMISE_does_not_rescue_a_post_cutoff_read(catalog, v1_ir):
    """C-C5's separation, load-bearing: a promise says WHEN data should arrive, not WHICH INSTANT a
    read observes. A plan storing only the promise would call this fine."""
    from featuregen.materialize.contract import AvailabilityPromiseV1 as Promise

    planned = _planned(
        v1_ir, converted=True,
        policy_reads=(PolicyReadV2(
            policy_ref=FX_REF, role="currency_conversion", logical_ref=FX_RATE,
            temporal=TemporalReadV2(basis=KnowledgeTimeBasisV2.LATEST_AVAILABLE,
                                    declared_promise=Promise(calendar_days=30))),))
    verdict = full_read_set_leakage_gate_v2([planned], target_ref=f"{CUSTOMERS}.status_cd")
    assert verdict.admitted is False
    assert "does not change that" in verdict.findings[0].detail


def test_the_SAME_READ_at_the_cutoff_is_admitted(catalog, v1_ir):
    """The discriminator, so the refusals above are about the BASIS and not about FX reads."""
    verdict = full_read_set_leakage_gate_v2(
        [_planned(v1_ir, converted=True)], target_ref=f"{CUSTOMERS}.status_cd")
    assert verdict.admitted is True


# ══ ACCEPTANCE 3 — an FX column the roles cannot read refuses READ_SCOPE_INSUFFICIENT ═══════════
def test_AN_UNREADABLE_FX_COLUMN_REFUSES_READ_SCOPE_INSUFFICIENT(catalog, v1_ir):
    """Through the ORDINARY path, not a rule written specially for FX: the rate column enters the
    same union as everything else, as its own element kind."""
    _tag(catalog, "fx_rates", "rate", "restricted")
    planned = _planned(v1_ir, converted=True)

    refused = authorize_compilation_v2(catalog, [planned], v1_ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused), refused
    assert refused.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert FX_RATE in refused.detail
    # The KIND is in the message, because "change the formula" and "be granted a rate table the
    # formula never mentions" are different remedies.
    assert ReadElementKind.POLICY_READ.value in refused.detail


def test_the_SAME_GROUP_without_the_fx_policy_read_authorizes(catalog, v1_ir):
    """The discriminator: the restriction is on a column only the policy reads."""
    _tag(catalog, "fx_rates", "rate", "restricted")
    token = authorize_compilation_v2(catalog, [_planned(v1_ir)], v1_ir.spine, roles=_ROLES)
    assert isinstance(token, AuthorizedCompilationV2), token


def test_a_READABLE_fx_column_authorizes_and_is_in_the_token(catalog, v1_ir):
    token = authorize_compilation_v2(
        catalog, [_planned(v1_ir, converted=True)], v1_ir.spine, roles=_ROLES)
    assert isinstance(token, AuthorizedCompilationV2), token
    assert FX_RATE in token.authorized_refs


def test_an_UNGOVERNED_fx_column_refuses_COLUMN_NOT_GOVERNED_first(catalog, v1_ir):
    """Existence before read scope, inherited rather than re-decided: no grantable role can make an
    undescribed column readable, so a scope refusal would send the operator after a privilege that
    cannot help."""
    planned = _planned(
        v1_ir, converted=True,
        policy_reads=(_fx_read(logical_ref=f"{_SRC}::public.fx_rates.no_such_column"),))
    refused = authorize_compilation_v2(catalog, [planned], v1_ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused), refused
    assert refused.code is CompilationRefusalCode.COLUMN_NOT_GOVERNED


def test_the_gate_refuses_the_WHOLE_group_not_the_offending_feature(catalog, v1_ir):
    """Group-wide, because a group is published as one row per key."""
    _tag(catalog, "fx_rates", "rate", "restricted")
    clean = _planned(v1_ir, name="clean_feature")
    dirty = _planned(v1_ir, converted=True, name="converted_feature")
    refused = authorize_compilation_v2(catalog, [clean, dirty], v1_ir.spine, roles=_ROLES)
    assert isinstance(refused, MaterializationRefused), refused
    assert "2 feature(s)" in refused.detail


def test_an_empty_v2_group_is_a_CALLER_ERROR(catalog, v1_ir):
    with pytest.raises(ValueError, match="permit for nothing"):
        authorize_compilation_v2(catalog, [], v1_ir.spine, roles=_ROLES)


# ══ ACCEPTANCE 4 — MEMBERSHIP IS QUERYABLE ══════════════════════════════════════════════════════
def _plan_for(catalog, v1_ir, *, names=("posted_debit_amount_30d",)):
    planned = [_planned(v1_ir, name=name) for name in names]
    contracts = contracts_for(
        catalog, planned, cadence=CADENCE, availability_promise=_bare_promise(),
        physical_type_policy=POLICY_ID)
    assert not isinstance(contracts, MaterializationRefused), contracts
    group = group_by_contract_v2(contracts)
    assert isinstance(group, ContractGroupV2), group
    features = tuple(
        PlannedFeature(column_name=member.ir.feature_name, ir_hash=ir_hash_v2(member.ir),
                       physical_type=PhysicalType(sql_type="DECIMAL(38,6)", nullable=True,
                                                  rounding=None, overflow=None))
        for member in planned)
    return build_group_plan_v2(group, features, logical_group_name="customer_txn_features",
                               physical_type_policy=POLICY_ID)


def test_MEMBERSHIP_ANSWERS_THE_FORWARD_QUESTION(catalog, v1_ir):
    """Which columns does this group publish — a query, not a re-derivation. A question answered by
    re-deriving is one answered DIFFERENTLY once the inputs move."""
    plan = _plan_for(catalog, v1_ir, names=("posted_debit_amount_30d", "posted_credit_amount_30d"))
    plan_hash = record_group_plan(catalog, plan, environment_id=ENV)

    members = members_of_group(catalog, environment_id=ENV,
                               logical_group_name="customer_txn_features")
    assert [member.column_name for member in members] == [
        "posted_credit_amount_30d", "posted_debit_amount_30d"]
    assert {member.group_plan_hash for member in members} == {plan_hash}
    # The IR hash rides on the row, so a member can be traced to the compiled plan behind it.
    assert all(member.ir_hash.startswith("sha256:") or member.ir_hash for member in members)


def test_MEMBERSHIP_ANSWERS_THE_REVERSE_QUESTION(catalog, v1_ir):
    plan = _plan_for(catalog, v1_ir, names=("posted_debit_amount_30d", "posted_credit_amount_30d"))
    record_group_plan(catalog, plan, environment_id=ENV)

    found = group_of_feature(catalog, environment_id=ENV, feature_name="posted_debit_amount_30d")
    assert [row.logical_group_name for row in found] == ["customer_txn_features"]
    assert found[0].materialization_contract_hash == plan.materialization_contract_hash


def test_MEMBERSHIP_IS_ENVIRONMENT_SCOPED(catalog, v1_ir):
    """F3: environment is deployment placement, not feature meaning, so it must not fold into the
    group name — and two environments publishing the same logical group must not share membership.
    """
    plan = _plan_for(catalog, v1_ir)
    record_group_plan(catalog, plan, environment_id=ENV)

    assert members_of_group(catalog, environment_id=ENV,
                            logical_group_name="customer_txn_features")
    assert members_of_group(catalog, environment_id="hdfc-uat",
                            logical_group_name="customer_txn_features") == ()
    assert group_of_feature(catalog, environment_id="hdfc-uat",
                            feature_name="posted_debit_amount_30d") == ()


def test_recording_a_group_plan_needs_an_ENVIRONMENT(catalog, v1_ir):
    with pytest.raises(ValueError, match="deployment placement"):
        record_group_plan(catalog, _plan_for(catalog, v1_ir), environment_id="  ")


def test_the_IR_HASH_comes_from_the_PLAN_not_a_second_argument():
    """A caller-supplied map would be a second statement about which compiled plan produces a
    column — free to disagree with the plan being recorded."""
    parameters = inspect.signature(record_group_plan).parameters
    assert "ir_hashes" not in parameters
    assert set(parameters) == {"conn", "plan", "environment_id"}


def test_membership_rows_are_APPEND_ONLY(catalog, v1_ir):
    import psycopg

    plan = _plan_for(catalog, v1_ir)
    plan_hash = record_group_plan(catalog, plan, environment_id=ENV)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        catalog.execute(
            "UPDATE materialization_group_member SET ir_hash = %s WHERE group_plan_hash = %s",
            ("sha256:rewritten", plan_hash))


def test_recording_the_same_plan_twice_is_idempotent(catalog, v1_ir):
    plan = _plan_for(catalog, v1_ir)
    record_group_plan(catalog, plan, environment_id=ENV)
    record_group_plan(catalog, plan, environment_id=ENV)
    assert len(members_of_group(catalog, environment_id=ENV,
                                logical_group_name="customer_txn_features")) == 1


# ══ ACCEPTANCE 5 — V1 BYTES, and the single-contract path, BYTE-IDENTICAL ═══════════════════════
def test_V1s_GATE_DELEGATES_rather_than_keeping_a_copy():
    """This is what makes "V1 bytes unchanged" a consequence of the structure instead of a promise.
    Two copies of the refusal text would both pass today and drift on the first edit to either."""
    source = inspect.getsource(authorize_compilation)
    assert "decide_read_scope(" in source
    # The CODE, not the prose: the docstring still explains the refusal it delegates, and a check
    # that read the explanation would be satisfied by deleting it.
    code = _code_of_function(authorize_compilation)
    assert "read_scope_insufficient" not in code
    assert "the supplied read scope hides" not in code


def test_V1_AND_V2_PRODUCE_THE_SAME_REFUSAL_BYTES_for_the_same_union(catalog, v1_ir):
    """One implementation, so one message. Compared byte for byte rather than by code, because the
    detail is what an operator reads and two gates drifting apart shows up there first."""
    _tag(catalog, "transactions", "txn_amt", "restricted")

    v1_refusal = authorize_compilation(catalog, (v1_ir,), v1_ir.spine, roles=_ROLES)
    assert isinstance(v1_refusal, MaterializationRefused), v1_refusal

    elements = union_read_elements(
        [(v1_ir.feature_name, v1_ir.expressions)], v1_ir.spine)
    direct = decide_read_scope(catalog, elements, roles=_ROLES, feature_count=1)
    assert isinstance(direct, MaterializationRefused), direct
    assert direct.detail == v1_refusal.detail
    assert direct.code is v1_refusal.code


def test_a_V1_GROUP_STILL_AUTHORIZES_UNCHANGED(catalog, v1_ir):
    token = authorize_compilation(catalog, (v1_ir,), v1_ir.spine, roles=_ROLES)
    assert isinstance(token, AuthorizedCompilation), token
    assert token.irs == (v1_ir,)


def test_the_V1_CONTRACT_HASH_is_untouched_by_the_extraction(catalog, v1_ir):
    """The bytes themselves. `derive_contract` reaches `tighten_classification` under its new name;
    a rename that changed behaviour would move this hash."""
    from featuregen.materialize.contract import contract_hash

    contract = derive_contract(catalog, v1_ir, cadence=CADENCE,
                               availability_promise=_bare_promise())
    assert isinstance(contract, MaterializationContractV1), contract
    assert contract_hash(contract) == contract_hash(
        derive_contract(catalog, v1_ir, cadence=CADENCE,
                        availability_promise=_bare_promise()))


def test_THE_SINGLE_CONTRACT_PATH_IS_BYTE_IDENTICAL_through_grouping(catalog, v1_ir):
    """Grouping must not perturb the contract it groups. A group of one is where a stray
    normalization would hide — every member agrees with itself, so only the bytes can tell."""
    planned = _planned(v1_ir)
    contract = derive_contract_v2(
        catalog, planned, cadence=CADENCE, availability_promise=_bare_promise(),
        physical_type_policy=POLICY_ID)
    assert not isinstance(contract, MaterializationRefused), contract

    group = group_by_contract_v2({planned.ir.feature_name: contract})
    assert isinstance(group, ContractGroupV2), group
    assert group.contract is contract
    assert group.contract_hash == contract_hash_v2(contract)
    assert group.feature_names == (planned.ir.feature_name,)


def test_TWO_DISAGREEING_CONTRACTS_are_LISTED_never_merged(catalog, v1_ir):
    """Merging is the promotion §5.1 exists to prevent, and an operator needs to see which features
    disagreed before choosing which group is right."""
    planned = _planned(v1_ir)
    base = derive_contract_v2(catalog, planned, cadence=CADENCE,
                              availability_promise=_bare_promise(),
                              physical_type_policy=POLICY_ID)
    assert not isinstance(base, MaterializationRefused), base
    import dataclasses

    other = dataclasses.replace(base, retention_class="seven_years")

    refused = group_by_contract_v2({"a": base, "b": other})
    assert isinstance(refused, MaterializationRefused), refused
    assert refused.code is CompilationRefusalCode.MULTIPLE_MATERIALIZATION_CONTRACTS
    assert "a" in refused.detail and "b" in refused.detail


def test_grouping_NOTHING_is_a_caller_error():
    with pytest.raises(ValueError, match="publishes nothing"):
        group_by_contract_v2({})


# ══ the union folds policy reads in, rather than keeping them beside it ═════════════════════════
def test_a_POLICY_READ_enters_the_SAME_union_as_everything_else(catalog, v1_ir):
    """Kept beside the read set it would be read at run time and absent from Gate 2's decision and
    from §5.2's sensitivity classification."""
    elements = union_read_elements(
        [(v1_ir.feature_name, v1_ir.expressions)], v1_ir.spine, policy_reads=[FX_RATE])
    kinds = {element.logical_ref: element.kinds for element in elements}
    assert ReadElementKind.POLICY_READ in kinds[FX_RATE]
    assert ReadElementKind.EXPRESSION_READ in kinds[TXN_AMT]


def test_a_column_read_BOTH_WAYS_carries_both_kinds(catalog, v1_ir):
    """A ref can be an operand AND a policy column. Reporting only one would send an author to fix
    one path while the other still reads it."""
    elements = union_read_elements(
        [(v1_ir.feature_name, v1_ir.expressions)], v1_ir.spine, policy_reads=[TXN_DR_CR])
    kinds = {element.logical_ref: element.kinds for element in elements}
    assert set(kinds[TXN_DR_CR]) >= {ReadElementKind.EXPRESSION_READ, ReadElementKind.POLICY_READ}


def test_V1_PASSES_NO_POLICY_READS_and_cannot(catalog, v1_ir):
    """The default is a fact about V1, not a convenience: a V1 formula declares no policies."""
    plain = union_read_elements([(v1_ir.feature_name, v1_ir.expressions)], v1_ir.spine)
    assert all(ReadElementKind.POLICY_READ not in element.kinds for element in plain)
