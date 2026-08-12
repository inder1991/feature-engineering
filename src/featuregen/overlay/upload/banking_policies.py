"""BR-10 — the governed banking policy-kind registry: what a policy reference must BE.

BR-6 taught formulas to CARRY policy references (``AuthorityRefsV2``, ``allocation_policy_ref``)
and BR-7 to BLOCK on unresolved ones — but "policy" was an open string. This module closes it:
a small registry of the governed policy KINDS the platform recognizes, what each kind REQUIRES
a governed record to declare, and which existing mechanism is its resolution home. It creates NO
second store: the eligible-status and reversal kinds resolve in ``data_agent.eligibility`` (whose
IR already declares per-source status vocabularies and refuses unsupported reversal shapes), the
direction/sign kind in the BR-5 formula-authority envelope, the model-output kind against BR-7A's
``ModelFeatureSpecV1`` — this registry only names the kinds and their declaration schemas, so a
recipe never embeds a source's status literals and a ref like ``eligible_status:posted-only`` is
checkable BEFORE any store lookup.

The BR-10 acceptance sentence this mechanizes: *a source must declare or resolve its
lifecycle/status policy before filtered formulas are materialization-ready* —
:func:`required_policy_kinds` reads a formula's shape and says which kinds it needs, and
:func:`policy_blockers` turns the undeclared ones into the machine-readable blocker codes BR-7's
``governed_policy_blockers`` input consumes.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


class PolicyKindError(ValueError):
    """An unknown kind or an incomplete declaration — refused, never approximated."""


@dataclass(frozen=True, slots=True)
class GovernedPolicyKindV1:
    """One governed policy KIND: what any record of this kind must declare to mean anything.

    ``required_declarations`` are the fields a governed policy record must carry (a threshold
    without a jurisdiction, an effective period and a currency is a number, not a policy);
    ``resolution_home`` names the EXISTING mechanism that resolves references of this kind —
    documentation of the one store, not a hook for a second one."""

    kind: str
    description: str
    required_declarations: tuple[str, ...]
    resolution_home: str


#: The closed vocabulary — the plan's eleven governed policy types, verbatim.
BANKING_POLICY_KINDS: tuple[GovernedPolicyKindV1, ...] = (
    GovernedPolicyKindV1(
        "eligible_status",
        "Which transaction/booking statuses COUNT. One source's 'POSTED' is vocabulary, not "
        "truth — the meaning is declared per source, never hardcoded in a recipe.",
        ("source", "status_field_meaning", "eligible_statuses_meaning"),
        "data_agent.eligibility (ObservationPlan status declaration)"),
    GovernedPolicyKindV1(
        "direction_sign",
        "How debit/credit direction and physical sign are read — signed amounts vs positive "
        "magnitudes with an indicator. Inflow-vs-outflow is unanswerable without it.",
        ("direction_authority", "sign_convention"),
        "recipe_operand_policy.build_formula_authority_envelope (direction requirement)"),
    GovernedPolicyKindV1(
        "reversal_correction",
        "How reversals/corrections neutralize originals. Only declared shapes are supported; "
        "an unsupported shape is REFUSED, never approximated as a flag.",
        ("reversal_mode", "as_of_semantics"),
        "data_agent.eligibility (ReversalMode; REVERSAL_AS_OF_UNRESOLVED)"),
    GovernedPolicyKindV1(
        "active_state",
        "Which account/product/facility lifecycle states count as ACTIVE for population "
        "definitions (account_status open/dormant/closed is data; active-ness is policy).",
        ("state_field_meaning", "active_states_meaning"),
        "governed operational facts (read_operational_value)"),
    GovernedPolicyKindV1(
        "currency_conversion",
        "Which governed rate converts per-row currencies, AS OF when (booking vs settlement). "
        "The BR-6 refusal CURRENCY_CONVERSION_UNDECLARED points at a missing ref of this kind.",
        ("rate_source", "rate_timing"),
        "formula.output_authority_v2 (currency_conversion_ref resolution)"),
    GovernedPolicyKindV1(
        "business_calendar",
        "Business-day convention and timezone — what 'end of day' and 'T+n' MEAN for a source.",
        ("calendar", "timezone"),
        "governed operational facts (read_operational_value)"),
    GovernedPolicyKindV1(
        "allocation",
        "How joint-account / facility-to-obligor / relationship rollups allocate amounts. The "
        "BR-6 allocation_policy_ref names a record of this kind.",
        ("allocation_rule", "rollup_grain"),
        "formula schema v2 (allocation_policy_ref resolution)"),
    GovernedPolicyKindV1(
        "threshold",
        "A governed cutoff. A number is not a policy: a threshold binds only with its "
        "jurisdiction, its effective period and its currency.",
        ("jurisdiction", "effective_period", "currency"),
        "recipe_variants (governed threshold policy labels)"),
    GovernedPolicyKindV1(
        "risk_corridor",
        "Risk-rating / corridor classifications with effective dating — a corridor list "
        "without an effective period silently reclassifies history.",
        ("rating_source", "effective_dating"),
        "governed operational facts (read_operational_value)"),
    GovernedPolicyKindV1(
        "model_output",
        "Consumption terms for a governed model output: which registered model/version, over "
        "what horizon, with what confidence basis. BR-7A's ModelFeatureSpec is the record.",
        ("model_ref", "model_version", "horizon", "confidence_basis"),
        "model_feature_registry (ModelFeatureSpecV1)"),
    GovernedPolicyKindV1(
        "privacy_purpose",
        "Privacy, suitability, consent and permitted-purpose constraints on using a value — "
        "the policy answer to PII-laden computable data.",
        ("purposes", "consent_basis"),
        "field_policies (sensitivity evidence requirements)"),
)

POLICY_KIND_REGISTRY: Mapping[str, GovernedPolicyKindV1] = {
    k.kind: k for k in BANKING_POLICY_KINDS}

#: The BR-6 carriage, mapped: which policy KIND each AuthorityRefsV2 field (and the proposal's
#: allocation ref) names. Total over the schema's fields BY TEST — a new ref field cannot ship
#: without declaring its kind here.
AUTHORITY_REF_KINDS: Mapping[str, str] = {
    "status_policy_ref": "eligible_status",
    "direction_policy_ref": "direction_sign",
    "reversal_policy_ref": "reversal_correction",
    "currency_conversion_ref": "currency_conversion",
    "allocation_policy_ref": "allocation",
}


def parse_policy_ref(ref: str) -> tuple[str, str]:
    """``"<kind>:<name>"`` → (kind, name), refusing unknown kinds and empty names. The check a
    carrier runs BEFORE any store lookup — a ref that cannot name its kind resolves nowhere."""
    kind, sep, name = ref.partition(":")
    if not sep or not name.strip():
        raise PolicyKindError(f"policy ref {ref!r} is not '<kind>:<name>'")
    if kind not in POLICY_KIND_REGISTRY:
        raise PolicyKindError(
            f"unknown policy kind {kind!r}; this platform governs "
            f"{sorted(POLICY_KIND_REGISTRY)}")
    return kind, name.strip()


def validate_policy_declaration(kind: str, declarations: Mapping[str, str]) -> None:
    """A governed record of ``kind`` must declare every required field, non-vacuously."""
    spec = POLICY_KIND_REGISTRY.get(kind)
    if spec is None:
        raise PolicyKindError(f"unknown policy kind {kind!r}")
    missing = [f for f in spec.required_declarations if not declarations.get(f, "").strip()]
    if missing:
        raise PolicyKindError(
            f"a {kind} policy must declare {missing} — without them it is "
            f"not a policy: {spec.description.split('.')[0]}")


def required_policy_kinds(*, filtered: bool = False, monetary: bool = False,
                          per_row_currency: bool = False, cross_grain_rollup: bool = False,
                          ) -> tuple[str, ...]:
    """Which policy kinds a formula's SHAPE requires. A filtered formula needs the status and
    reversal policies (which rows count is functional correctness, not decoration); a monetary
    one needs direction/sign; per-row currency needs conversion; a rollup needs allocation."""
    kinds: list[str] = []
    if filtered:
        kinds += ["eligible_status", "reversal_correction"]
    if monetary:
        kinds.append("direction_sign")
    if per_row_currency:
        kinds.append("currency_conversion")
    if cross_grain_rollup:
        kinds.append("allocation")
    return tuple(kinds)


def policy_blockers(required: Iterable[str], declared: Iterable[str]) -> tuple[str, ...]:
    """The undeclared kinds as machine-readable blocker codes — BR-7's
    ``governed_policy_blockers`` input, so a filtered formula whose source never declared its
    status policy folds to FORMULA_BLOCKED with the missing kind NAMED."""
    declared_set = set(declared)
    return tuple(f"policy_undeclared:{kind}" for kind in required
                 if kind not in declared_set)


def _validate_registry() -> None:
    if len({k.kind for k in BANKING_POLICY_KINDS}) != len(BANKING_POLICY_KINDS):
        raise ValueError("duplicate policy kind")
    for k in BANKING_POLICY_KINDS:
        if not k.required_declarations:
            raise ValueError(f"policy kind {k.kind!r} requires no declarations — "
                             "a kind nothing must declare governs nothing")
        if not k.resolution_home.strip():
            raise ValueError(f"policy kind {k.kind!r} names no resolution home")
    # AUTHORITY_REF_KINDS maps real schema fields to real kinds — both directions checked here,
    # totality over AuthorityRefsV2's fields is pinned by test (fields() introspection).
    for field_name, kind in AUTHORITY_REF_KINDS.items():
        if kind not in POLICY_KIND_REGISTRY:
            raise ValueError(f"authority ref {field_name!r} names unknown kind {kind!r}")


_validate_registry()

__all__ = [
    "AUTHORITY_REF_KINDS", "BANKING_POLICY_KINDS", "GovernedPolicyKindV1", "POLICY_KIND_REGISTRY",
    "PolicyKindError", "parse_policy_ref", "policy_blockers", "required_policy_kinds",
    "validate_policy_declaration",
]
