"""C-C1/C-C2 — the V2 EXECUTION BOUNDARY: what §2's chain requires, over Formula-V2 features.

**Why a boundary at all.** Nothing in ``materialize/`` consumes a V2 formula. ``authorize_compilation``
takes ``Sequence[FormulaExecutionIRV1]`` and mints the token that is *"the only way into §2's
downstream chain"*, so a V2 feature has no door: it can be authored, disposed and replayed, and
still cannot reach a number. This module is that door, built additively — **no V1 type, signature
or canonical byte changes** (C-C1's gate), because V1 features are live and their hashes are
history.

**What is REUSED rather than re-declared.** :class:`~featuregen.materialize.expression_ir.
ExpressionExecutionIR`, :class:`~featuregen.materialize.spine.SpineSpec` and the contract's own
sub-declarations name no formula version — they describe physical reads, joins, point-in-time
semantics and publication, which V2 did not change. Re-declaring them would create two answers to
questions like "what does this expression read". The genuinely V2-shaped parts are exactly three:
the output policy (:class:`FormulaOutputPolicyV2`, whose ``currency`` carries
``"converted:<ref>"``), the final operation (:class:`FinalOperationV2`), and the semantic row
selection C-A3b put on the recipe.

**The read set is not derived here.** :func:`~featuregen.materialize.ir.physical_read_set_of` is the
one walk, shared with V1 — it folds join endpoints BY KIND, join predicates and availability
columns, and getting any of that subtly different in a second implementation would hand the group a
narrower read set than the one it actually reads.

**The ordering is expressed in types (C-C2).** ``read set → Gate 2 → authorization`` is not a
convention here: :class:`AuthorizedCompilationV2` holds
:class:`PlannedFormulaExecutionIRV2`, which cannot exist without a read set that matches its IR, so
an authorization naming IRs that were never planned is unconstructible rather than merely wrong.
The authorization then names *those exact planned IRs*, and the renderer consumes the same objects
— nothing is rebuilt after the gate, which is where a group could otherwise be authorized for one
read set and rendered from another.

**Physical types are a POLICY ID, not an ordinal.** V1 carries ``physical_type_policy_version: int``
— a counter that says nothing about which rules produced a type. V2 carries a named, versioned
policy id (``formula-v2/physical-types@1``); its CONTENT is C-C6's and is not defined yet, so this
module refuses a blank or ordinal-shaped id rather than inventing one.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.schema_leaves import ZeroDenominator
from featuregen.formula.schema_v2 import AuthorityRefsV2, FinalOperationV2
from featuregen.formula.schema_v3 import SemanticRowSelectionV1
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    BackfillBoundary,
    CadenceDecl,
    LandingPitSemantics,
    PublicationPolicy,
)
from featuregen.materialize.expression_ir import ExpressionExecutionIR
from featuregen.materialize.group_plan import PlannedFeature
from featuregen.materialize.ir import physical_read_set_of
from featuregen.materialize.spine import SpineSourceDeclarationV1, SpineSpec

__all__ = [
    "AuthorizedCompilationV2",
    "CompilationIdentityV2",
    "DeclaredPoliciesV2",
    "FeatureGroupPlanV2",
    "FormulaExecutionIRV2",
    "KnowledgeTimeBasisV2",
    "MaterializationContractV2",
    "PlannedFormulaExecutionIRV2",
    "PolicyReadV2",
    "SelectedRowsV2",
    "TemporalReadV2",
    "contract_hash_v2",
    "group_plan_hash_v2",
    "ir_hash_v2",
]

#: A physical-type policy id: ``<family>/<name>@<version>``. The shape is checked, never the
#: content — C-C6 defines what ``formula-v2/physical-types@1`` MEANS. Checking the shape here is
#: what stops V1's ordinal (``1``, or ``"1"``) from being smuggled into a field that is supposed to
#: name a rule set: an ordinal identifies a counter's position, not the rules a type was decided
#: under, and the two are indistinguishable once persisted as a string.
_POLICY_ID = re.compile(r"^[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)+@\d+$")


def _checked_policy_id(value: str, field: str) -> str:
    if not _POLICY_ID.fullmatch(value or ""):
        raise ValueError(
            f"{field}={value!r} is not a physical-type policy id of the form "
            f"'<family>/<name>@<version>' (for example 'formula-v2/physical-types@1'). V1's "
            f"ordinal version says which counter value was current, not which rules decided a "
            f"type; V2 names the rule set so a persisted artifact states what it was typed under")
    return value


# ── the feature IR ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SelectedRowsV2:
    """One expression's SEMANTIC row selection (C-A3b), carried from the recipe into execution.

    ``semantic_value`` is semantic on purpose — ``"debit"``, not the pilot ledger's ``"D"``.
    Resolving it to a physical value is a POLICY realization (C-C8) and deliberately does not happen
    here: a boundary that quietly resolved ``"debit"`` to a literal would bake one ledger's encoding
    into every feature's identity, and the pilot ledger's own encoding (``D``/``C``) already differs
    from the frozen V2 fixture's (``"debit"``).

    Order is PRESERVED, never sorted. The V3 canonicalizer treats ``row_selections`` as an ordered
    tuple, so two orderings are two formulas with two different ``formula_content_hash`` values;
    sorting here would give those two formulas one IR hash and silently merge them.
    """

    expr_path: str
    selections: tuple[SemanticRowSelectionV1, ...]

    def __post_init__(self) -> None:
        if not self.expr_path.strip():
            raise ValueError("a row selection must name the expression it selects rows for")
        if not self.selections:
            raise ValueError(
                f"{self.expr_path} carries an EMPTY selection tuple: an expression that selects no "
                f"rows structurally has no entry at all, and an empty one is indistinguishable "
                f"from 'the selection was dropped somewhere between the recipe and here'")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "expr_path": self.expr_path,
            "selections": [
                {"kind": str(selection.kind), "role": selection.role,
                 "semantic_value": selection.semantic_value}
                for selection in self.selections
            ],
        }


@dataclass(frozen=True, slots=True)
class DeclaredPoliciesV2:
    """The governed policies ONE expression declared — :class:`AuthorityRefsV2` carried verbatim.

    Without this the IR has no record of which policies a formula depends on, and C-C4's
    read-set union has nothing to check its coverage against: a feature could declare an FX
    conversion and plan zero policy reads, and the gate would authorize a compilation that reads a
    rate table nobody authorized. The refs are the formula's own declaration, so this is a carry,
    not a re-derivation.
    """

    expr_path: str
    refs: AuthorityRefsV2

    def __post_init__(self) -> None:
        if not self.expr_path.strip():
            raise ValueError("declared policies must name the expression that declared them")

    def declared_refs(self) -> tuple[tuple[str, str], ...]:
        """``(role, ref)`` for every NON-BLANK declared ref, in a fixed role order.

        Blank is how ``AuthorityRefsV2`` spells "this expression declares no policy of this kind",
        so a blank is not a policy to cover — and treating it as one would demand a read for a
        policy that does not exist.
        """
        return tuple(
            (role, ref) for role, ref in (
                ("status", self.refs.status_policy_ref),
                ("direction", self.refs.direction_policy_ref),
                ("reversal", self.refs.reversal_policy_ref),
                ("currency_conversion", self.refs.currency_conversion_ref),
            ) if ref.strip()
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "expr_path": self.expr_path,
            "status_policy_ref": self.refs.status_policy_ref,
            "direction_policy_ref": self.refs.direction_policy_ref,
            "reversal_policy_ref": self.refs.reversal_policy_ref,
            "currency_conversion_ref": self.refs.currency_conversion_ref,
        }


class KnowledgeTimeBasisV2(StrEnum):
    """WHEN a read's knowledge is taken — the axis C-C5 keeps separate from the T+N promise.

    ``LATEST_AVAILABLE`` is the dangerous one and is named rather than left implicit: a reversal
    flag or an FX rate read at current state tells you something that became true AFTER the cutoff,
    which the model being trained could not have known. That is leakage, and it looks identical to a
    correct read unless the basis is a fact the plan carries.
    """

    AS_OF_CUTOFF = "as_of_cutoff"
    EVENT_TIME = "event_time"
    LATEST_AVAILABLE = "latest_available"


@dataclass(frozen=True, slots=True)
class TemporalReadV2:
    """One read's temporal semantics, and — SEPARATELY — the promise declared over it (C-C5).

    Two fields, deliberately, because they answer different questions and conflating them hides
    leakage. ``basis`` is what the read actually SEES. ``declared_promise`` is the T+N availability
    the contract PROMISES for that data. A post-cutoff read under a generous promise still reads
    post-cutoff data; a plan that stored only the promise would call it fine.
    """

    basis: KnowledgeTimeBasisV2
    declared_promise: AvailabilityPromiseV1 | None

    def identity_payload(self) -> dict[str, Any]:
        return {
            "basis": self.basis.value,
            "declared_promise": (None if self.declared_promise is None
                                 else self.declared_promise.identity_payload()),
        }


@dataclass(frozen=True, slots=True)
class PolicyReadV2:
    """One physical column a POLICY reads, attributed to the policy that made it a read.

    The attribution is the point. Gate 2's read set is derived, so a policy read that named no
    policy would be an unexplained ref in the union — and C-C4's restatement is precisely that there
    is no separate list to add or forget: every read traces to something the formula declared.
    """

    policy_ref: str
    role: str
    logical_ref: str
    temporal: TemporalReadV2

    def __post_init__(self) -> None:
        for value, what in ((self.policy_ref, "policy_ref"), (self.role, "role"),
                            (self.logical_ref, "logical_ref")):
            if not value.strip():
                raise ValueError(
                    f"a policy read with a blank {what} cannot be attributed to anything, and an "
                    f"unattributed ref in Gate 2's union is exactly the separate list C-C4 removes")

    def identity_payload(self) -> dict[str, Any]:
        return {"policy_ref": self.policy_ref, "role": self.role,
                "logical_ref": self.logical_ref, "temporal": self.temporal.identity_payload()}


@dataclass(frozen=True, slots=True)
class FormulaExecutionIRV2:
    """One V2 feature's complete compiled plan — and nothing about a given run.

    Mirrors :class:`~featuregen.materialize.ir.FormulaExecutionIRV1` field for field, with the three
    V2-shaped differences named in the module docstring. ``authoring_run_id`` stays PROVENANCE and
    stays outside :meth:`identity_payload` for V1's reason: the same governed artifact authored
    twice is one computation, and letting the run id in would split a materialization group by who
    happened to author its members — which now matters more, not less, because C-A5's deterministic
    producer can author the same formula from the same reviewed blueprint any number of times.
    """

    feature_name: str
    formula_content_hash: str
    final_operation: FinalOperationV2
    zero_denominator: ZeroDenominator | None
    grain_entity: str
    grain_keys: tuple[str, ...]
    expressions: tuple[ExpressionExecutionIR, ...]
    row_selections: tuple[SelectedRowsV2, ...]
    policies: tuple[DeclaredPoliciesV2, ...]
    spine: SpineSpec
    output_policy: FormulaOutputPolicyV2
    authoring_run_id: str

    def __post_init__(self) -> None:
        if not self.expressions:
            raise ValueError(
                f"{self.feature_name} compiles to no expressions: a feature that computes nothing "
                f"is not a feature, and an IR for it would hash the same as every other empty one")
        paths = [expression.expr_path for expression in self.expressions]
        if len(set(paths)) != len(paths):
            raise ValueError(
                f"{self.feature_name} carries two expressions at the same body path: identity "
                f"orders expressions BY PATH, so a duplicate makes the ordering ambiguous")
        seen: set[str] = set()
        for selected in self.row_selections:
            if selected.expr_path not in set(paths):
                raise ValueError(
                    f"{self.feature_name} carries a row selection for {selected.expr_path!r}, "
                    f"which is not one of its expressions ({', '.join(sorted(paths))}): a "
                    f"selection that names no expression selects nothing, and carrying it would "
                    f"let a feature LOOK filtered while executing unfiltered")
            if selected.expr_path in seen:
                raise ValueError(
                    f"{self.feature_name} carries two row-selection entries for "
                    f"{selected.expr_path!r}: which one applies is then a tuple-order accident")
            seen.add(selected.expr_path)
        declared: set[str] = set()
        for policies in self.policies:
            if policies.expr_path not in set(paths):
                raise ValueError(
                    f"{self.feature_name} declares policies for {policies.expr_path!r}, which is "
                    f"not one of its expressions ({', '.join(sorted(paths))}): a policy attached to "
                    f"no expression is one the read-set union would demand a read for and the "
                    f"renderer would never apply")
            if policies.expr_path in declared:
                raise ValueError(
                    f"{self.feature_name} carries two policy declarations for "
                    f"{policies.expr_path!r}: which set governs is then a tuple-order accident")
            declared.add(policies.expr_path)

    def declared_policy_refs(self) -> tuple[tuple[str, str, str], ...]:
        """``(expr_path, role, ref)`` for every non-blank policy this formula declared, sorted.

        The list C-C4's coverage check is written against — derived from the formula's own
        declaration, so there is nothing separate to keep in step with it.
        """
        return tuple(sorted(
            (policies.expr_path, role, ref)
            for policies in self.policies
            for role, ref in policies.declared_refs()))

    def identity_payload(self) -> dict[str, Any]:
        """What this feature IS — no provenance, no run-time value.

        Expressions and row selections BOTH enter ordered by body path, for V1's reason: which half
        of a ratio the tuple happens to hold first is not a fact about the computation.
        """
        return {
            "feature_name": self.feature_name,
            "formula_content_hash": self.formula_content_hash,
            "formula_schema_version": 3,
            "final_operation": self.final_operation.value,
            "zero_denominator": (None if self.zero_denominator is None
                                 else self.zero_denominator.value),
            "grain_entity": self.grain_entity,
            "grain_keys": list(self.grain_keys),
            "expressions": [expression.identity_payload()
                            for expression in sorted(self.expressions,
                                                     key=lambda e: e.expr_path)],
            "row_selections": [selected.identity_payload()
                               for selected in sorted(self.row_selections,
                                                      key=lambda s: s.expr_path)],
            "policies": [policies.identity_payload()
                         for policies in sorted(self.policies, key=lambda p: p.expr_path)],
            "spine": self.spine.identity_payload(),
            "output_policy": {
                "output_type": self.output_policy.output_type,
                "unit": self.output_policy.unit,
                # V2's currency is `"" | "fixed:<CCY>" | "converted:<policy ref>"`, so the CONVERSION
                # a feature depends on is part of what the feature IS. V1's field could only ever
                # say which currency, never that one was converted at all.
                "currency": self.output_policy.currency,
                "output_additivity": self.output_policy.output_additivity.value,
                "external_type_required": self.output_policy.external_type_required,
                "output_policy_version": self.output_policy.output_policy_version,
            },
        }


def ir_hash_v2(ir: FormulaExecutionIRV2) -> str:
    """The V2 feature's content identity — ``materialize_hash`` is the one hasher (§14)."""
    return materialize_hash(ir.identity_payload())


# ── planning, then authorization (C-C2's ordering, in types) ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PlannedFormulaExecutionIRV2:
    """A V2 IR together with the read set it was PLANNED against — C-C2's first step.

    Existing at all is the claim: this IR's complete physical read set has been derived, once, by
    the shared walk. ``__post_init__`` re-derives and compares rather than trusting the caller, so
    a hand-built instance carrying a narrower read set than its IR actually reads cannot be
    constructed — which is the only thing standing between "Gate 2 authorized these refs" and "the
    run read those refs".
    """

    ir: FormulaExecutionIRV2
    policy_reads: tuple[PolicyReadV2, ...]
    read_set: tuple[str, ...]

    def __post_init__(self) -> None:
        derived = self.derive_read_set(self.ir, self.policy_reads)
        if tuple(self.read_set) != derived:
            missing = sorted(set(derived) - set(self.read_set))
            extra = sorted(set(self.read_set) - set(derived))
            raise ValueError(
                f"{self.ir.feature_name}'s planned read set does not match the one its IR derives"
                + (f"; MISSING {missing}" if missing else "")
                + (f"; UNREAD {extra}" if extra else "")
                + ". A planned read set is not an assertion a caller makes about an IR, it is a "
                  "fact the IR determines")
        self._check_policy_coverage()

    def _check_policy_coverage(self) -> None:
        """C-C4's restatement, enforced: every DECLARED policy has at least one read.

        The gate's read set is derived from what the formula declares, so there is no separate list
        to keep in step — but a declaration with no read is the same hole by another route: the
        compilation would apply a governed policy whose columns nobody authorized. The converse is
        checked too, because a policy read attributed to a policy the formula never declared is a
        read with no reason to exist.
        """
        declared = {ref for _, _, ref in self.ir.declared_policy_refs()}
        read_for = {read.policy_ref for read in self.policy_reads}
        uncovered = sorted(declared - read_for)
        if uncovered:
            raise ValueError(
                f"{self.ir.feature_name} declares {uncovered} and plans no read for them: a "
                f"governed policy is applied by reading its columns, so a declared policy with no "
                f"policy read would be applied on data Gate 2 never authorized")
        undeclared = sorted(read_for - declared)
        if undeclared:
            raise ValueError(
                f"{self.ir.feature_name} plans policy reads for {undeclared}, which it never "
                f"declared: a read attributed to a policy the formula does not use has no reason to "
                f"be in the union, and widening a read set is exactly what the union prevents")

    @property
    def structural_read_set(self) -> tuple[str, ...]:
        """The expression and spine half of the union, WITHOUT the policy reads.

        Not a second derivation — the same shared walk, asked a narrower question. The leakage gate
        needs it because a ref can be read BOTH as an operand and as a policy column, and reporting
        only one of the two would send an author to fix one path while the other still leaks.
        """
        return physical_read_set_of([(self.ir.feature_name, self.ir.expressions)], self.ir.spine)

    @staticmethod
    def derive_read_set(
        ir: FormulaExecutionIRV2, policy_reads: Sequence[PolicyReadV2],
    ) -> tuple[str, ...]:
        """The COMPLETE V2 read set: expression and spine reads, UNIONED with policy reads (C-C4).

        The expression and spine half goes through V1's shared walk unchanged. Policy reads are the
        V2 addition, and they are unioned here rather than kept beside the read set because §5.2
        classifies sensitivity over "what this feature reads" — a policy column outside that union
        would be read at run time and absent from the classification.
        """
        structural = physical_read_set_of([(ir.feature_name, ir.expressions)], ir.spine)
        return tuple(sorted({*structural, *(read.logical_ref for read in policy_reads)}))

    @classmethod
    def plan(
        cls, ir: FormulaExecutionIRV2, *, policy_reads: Sequence[PolicyReadV2],
    ) -> PlannedFormulaExecutionIRV2:
        """Derive the read set and pair it with ``ir`` — the ONLY sane way to build one.

        ``policy_reads`` is REQUIRED, with no default. A default of ``()`` would mean "this feature
        reads no policy columns", which is a claim, and the wrong one for every feature that
        declares a status, direction, reversal or conversion policy. Making the caller state it is
        what turns C-C4 from a list somebody maintains into a fact the type demands.
        """
        reads = tuple(policy_reads)
        return cls(ir=ir, policy_reads=reads, read_set=cls.derive_read_set(ir, reads))


@dataclass(frozen=True, slots=True)
class AuthorizedCompilationV2:
    """The TOKEN Gate 2 issues for a V2 group, and the only way into §2's downstream chain.

    Holds :class:`PlannedFormulaExecutionIRV2`, never bare IRs: that is C-C2's ordering expressed in
    the type system rather than in a comment, because an authorization built from unplanned IRs is
    then not merely incorrect but unconstructible. The renderer consumes THESE objects — nothing is
    re-planned after the gate, which is where a group could otherwise be authorized for one read set
    and rendered from another.

    As in V1 there is no partial variant and no per-feature field: the group is authorized as one
    thing or refused as one thing.
    """

    planned: tuple[PlannedFormulaExecutionIRV2, ...]
    spine: SpineSpec
    authorized_refs: tuple[str, ...]
    roles_used: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.planned:
            raise ValueError(
                "an authorized compilation names no feature: an empty group compiles nothing, and "
                "a token for it would authorize every empty group equally")
        # The group's union, INCLUDING policy reads (C-C4). Built from the planned members' own
        # read sets rather than re-walked, so the gate authorizes exactly what planning derived.
        required = {ref for planned in self.planned for ref in planned.read_set}
        required |= set(physical_read_set_of([], self.spine))
        unauthorized = sorted(required - set(self.authorized_refs))
        if unauthorized:
            raise ValueError(
                f"the group reads {unauthorized} which the token does not authorize: a token that "
                f"names fewer refs than the compilation reads is the exact shape of an "
                f"authorization bypass, so it is refused at construction rather than trusted")

    def ordered_planned(self) -> tuple[PlannedFormulaExecutionIRV2, ...]:
        """The planned IRs by FEATURE NAME — never by the order they were compiled in."""
        return tuple(sorted(self.planned, key=lambda planned: planned.ir.feature_name))

    @property
    def irs(self) -> tuple[FormulaExecutionIRV2, ...]:
        """The compiled IRs this token covers, ordered by feature name.

        Named to match V1's attribute deliberately: the project renderer asks a token for the IRs it
        authorized, and that question has one answer in both languages. A differently-spelled
        accessor would force the renderer to know which token it holds in order to ask.
        """
        return tuple(planned.ir for planned in self.ordered_planned())

    @property
    def ir_hashes(self) -> tuple[str, ...]:
        """The V2 IR hashes this token covers, ordered by feature name."""
        return tuple(ir_hash_v2(planned.ir) for planned in self.ordered_planned())


# ── contract, group plan, identity ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MaterializationContractV2:
    """One V2 feature's materialization contract — the group key of §5.

    Field-for-field V1's, except ``physical_type_policy``. It carries NO feature name, no IR hash
    and no physical type, for V1's reason: those are per-feature facts, and a contract holding any
    of them could never be shared, which would make the group of §10 a group of one.
    """

    entity: str
    ordered_keys: tuple[str, ...]
    pit_semantics: LandingPitSemantics
    sensitivity_class: str
    access_requirements: tuple[str, ...]
    retention_class: str
    retention_policy_version: int
    availability_promise: AvailabilityPromiseV1
    cadence: CadenceDecl
    publication_policy: PublicationPolicy
    backfill_boundary: BackfillBoundary
    spine: SpineSourceDeclarationV1
    classification_policy_version: int
    physical_type_policy: str

    def __post_init__(self) -> None:
        _checked_policy_id(self.physical_type_policy, "physical_type_policy")

    def identity_payload(self) -> dict[str, Any]:
        """§5.5's INCLUDE list — no run id, no watermark, no wall-clock, no provenance.

        Deliberately TOTAL: no parsing, no catalog read, nothing that can raise. It is called while
        building a hash, where an exception would be indistinguishable from a governed refusal.
        """
        return {
            "entity": self.entity,
            "ordered_keys": list(self.ordered_keys),
            "pit_semantics": self.pit_semantics.identity_payload(),
            "sensitivity_class": self.sensitivity_class,
            "access_requirements": list(self.access_requirements),
            "retention_class": self.retention_class,
            "retention_policy_version": self.retention_policy_version,
            "availability_promise": self.availability_promise.identity_payload(),
            "cadence": self.cadence.identity_payload(),
            "publication_policy": self.publication_policy.value,
            "backfill_boundary": self.backfill_boundary.value,
            "spine": self.spine.identity_payload(),
            "classification_policy_version": self.classification_policy_version,
            "physical_type_policy": self.physical_type_policy,
        }


def contract_hash_v2(contract: MaterializationContractV2) -> str:
    """The V2 contract's group key."""
    return materialize_hash(contract.identity_payload())


@dataclass(frozen=True, slots=True)
class FeatureGroupPlanV2:
    """The packing list: which columns a V2 group publishes, and under which contract.

    ``features`` is ordered by column name, so the plan does not depend on the order its features
    happened to be compiled in. ``physical_type_policy`` is STORED rather than read from a module at
    hash time, so a persisted plan states the rule set its types were decided under — which is the
    whole reason it is a named policy here and not an ordinal.
    """

    logical_group_name: str
    materialization_contract_hash: str
    entity_key_columns: tuple[str, ...]
    business_dt_column: str
    features: tuple[PlannedFeature, ...]
    physical_type_policy: str

    def __post_init__(self) -> None:
        _checked_policy_id(self.physical_type_policy, "physical_type_policy")

    def identity_payload(self) -> dict[str, Any]:
        """What the group PUBLISHES — no run id, no generation, no business date (§3.3)."""
        return {
            "logical_group_name": self.logical_group_name,
            "materialization_contract_hash": self.materialization_contract_hash,
            "entity_key_columns": list(self.entity_key_columns),
            "business_dt_column": self.business_dt_column,
            "features": [feature.identity_payload()
                         for feature in sorted(self.features, key=lambda f: f.column_name)],
            "physical_type_policy": self.physical_type_policy,
        }


def group_plan_hash_v2(plan: FeatureGroupPlanV2) -> str:
    """The V2 group plan's identity."""
    return materialize_hash(plan.identity_payload())


@dataclass(frozen=True, slots=True)
class CompilationIdentityV2:
    """What a V2 compilation IS — the identity the rendered artifact is produced from.

    As in V1 there is deliberately no ``generated_project_hash`` field: the rendered files embed
    THIS object, so a project hash on it would be a value the bytes contain and are hashed to
    produce.

    ``bridge_realization_dependencies`` and ``crosswalk_execution_pins`` mean "this group executes
    none", not "not carried yet" — the pilot is single-source and genuinely has none. C-C8's
    ``realizes_occurrences`` is a different fact and is NOT represented here, because inventing an
    empty field for it would say a group realizes no policies when what is true today is that
    policy realization does not exist.
    """

    formula_content_hashes: tuple[str, ...]
    ir_hashes: tuple[str, ...]
    materialization_contract_hash: str
    group_plan_hash: str
    bridge_realization_dependencies: tuple[tuple[str, str], ...] = ()
    crosswalk_execution_pins: tuple[tuple[str, str, str, str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.ir_hashes:
            raise ValueError(
                "a compilation identity names no feature: an empty group compiles nothing, and an "
                "identity for it would be the same identity for every empty group")
        if len(self.formula_content_hashes) != len(self.ir_hashes):
            raise ValueError(
                f"{len(self.formula_content_hashes)} formula hashes and {len(self.ir_hashes)} IR "
                f"hashes: they are paired one per feature, so unequal lengths mean one of the two "
                f"lists is not the group's members")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "formula_content_hashes": list(self.formula_content_hashes),
            "ir_hashes": list(self.ir_hashes),
            "materialization_contract_hash": self.materialization_contract_hash,
            "group_plan_hash": self.group_plan_hash,
            "bridge_realization_dependencies": [
                list(pair) for pair in self.bridge_realization_dependencies],
            "crosswalk_execution_pins": [
                list(pins) for pins in self.crosswalk_execution_pins],
        }


def build_compilation_identity_v2(
    authorized: AuthorizedCompilationV2,
    contract_hash: str,
    plan: FeatureGroupPlanV2,
) -> CompilationIdentityV2:
    """The identity of the compilation ``authorized`` authorizes.

    Takes the TOKEN, not a list of IRs: the identity names what Gate 2 admitted, so a caller cannot
    build an identity over features the gate never saw. Both hash lists are read from the token's
    own ordering, so they are paired one per feature by construction.
    """
    ordered = authorized.ordered_planned()
    return CompilationIdentityV2(
        formula_content_hashes=tuple(planned.ir.formula_content_hash for planned in ordered),
        ir_hashes=tuple(ir_hash_v2(planned.ir) for planned in ordered),
        materialization_contract_hash=contract_hash,
        group_plan_hash=group_plan_hash_v2(plan),
    )
