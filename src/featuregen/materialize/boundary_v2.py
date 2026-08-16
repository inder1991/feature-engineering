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
from dataclasses import dataclass
from typing import Any

from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.schema import ZeroDenominator
from featuregen.formula.schema_v2 import FinalOperationV2
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
    "FeatureGroupPlanV2",
    "FormulaExecutionIRV2",
    "MaterializationContractV2",
    "PlannedFormulaExecutionIRV2",
    "SelectedRowsV2",
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
    read_set: tuple[str, ...]

    def __post_init__(self) -> None:
        derived = physical_read_set_of([(self.ir.feature_name, self.ir.expressions)], self.ir.spine)
        if tuple(self.read_set) != derived:
            missing = sorted(set(derived) - set(self.read_set))
            extra = sorted(set(self.read_set) - set(derived))
            raise ValueError(
                f"{self.ir.feature_name}'s planned read set does not match the one its IR derives"
                + (f"; MISSING {missing}" if missing else "")
                + (f"; UNREAD {extra}" if extra else "")
                + ". A planned read set is not an assertion a caller makes about an IR, it is a "
                  "fact the IR determines")

    @classmethod
    def plan(cls, ir: FormulaExecutionIRV2) -> PlannedFormulaExecutionIRV2:
        """Derive the read set and pair it with ``ir`` — the ONLY sane way to build one."""
        return cls(ir=ir, read_set=physical_read_set_of(
            [(ir.feature_name, ir.expressions)], ir.spine))


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
        required = physical_read_set_of(
            [(planned.ir.feature_name, planned.ir.expressions) for planned in self.planned],
            self.spine)
        unauthorized = sorted(set(required) - set(self.authorized_refs))
        if unauthorized:
            raise ValueError(
                f"the group reads {unauthorized} which the token does not authorize: a token that "
                f"names fewer refs than the compilation reads is the exact shape of an "
                f"authorization bypass, so it is refused at construction rather than trusted")

    def ordered_planned(self) -> tuple[PlannedFormulaExecutionIRV2, ...]:
        """The planned IRs by FEATURE NAME — never by the order they were compiled in."""
        return tuple(sorted(self.planned, key=lambda planned: planned.ir.feature_name))

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
