"""C-D9 — the three evaluator INTERFACES, and every activation blocker accounted for.

**Interfaces only.** ``generate``, ``verify`` and ``publish_sandbox`` are typed contracts here; the
implementations ship at S8/S9/S10. A stub that returned a verdict would be an evaluator nobody
wrote, and the point of freezing the interface early is that the shape stops moving before three
stages depend on it.

**Every one of the sixteen activation blockers is accounted for, code by code.** Revision 17 listed
requirements in prose and silently lost ``PERSONAL_DATA_POLICY_REQUIRED`` — a use-licence gate, not
a review preference — so a V2 feature built from unlicensed personal data would have generated. A
prose list is how that happens; a table over the actual codes, proved exhaustive against
``semantic_eligibility_reasons`` by test, is how it stops.

**The rule these dispositions apply**, stated by the plan and applied here code by code: *human
SEMANTIC CONFIRMATION is what these evaluators drop; data-use licence, read scope and binding
completeness are carried*. Dropped never means "ignored" — it means this evaluator is not the gate
for it, because a governed V2 formula stands on a reviewed recipe blueprint rather than on a person
having confirmed each column's meaning. Anything that decides whether a computation is LEGAL or
POSSIBLE stays.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from featuregen.overlay.upload import semantic_eligibility_reasons as R

__all__ = [
    "ACTIVATION_BLOCKER_DISPOSITIONS",
    "EVALUATOR_ONLY_BLOCKERS",
    "BlockerDisposition",
    "EvaluatorAction",
    "EvaluatorVerdictV1",
    "GenerateEvaluator",
    "PublishSandboxEvaluator",
    "VerifyEvaluator",
    "carried_blockers",
]


class EvaluatorAction(StrEnum):
    """The three actions §0.3 names. Five activation actions exist; these are the three an
    execution chain gates, and naming only three is deliberate."""

    GENERATE = "generate"
    VERIFY = "verify"
    PUBLISH_SANDBOX = "publish_sandbox"


class BlockerDisposition(StrEnum):
    CARRIED = "carried"
    DROPPED = "dropped"


#: Every activation blocker code these evaluators can encounter, with its disposition and the reason.
#:
#: Proved exhaustive by test against the codes the shipped activation policy actually emits, so a
#: seventeenth cannot appear without a decision — which is exactly the failure mode revision 17 hit.
ACTIVATION_BLOCKER_DISPOSITIONS: Mapping[str, tuple[BlockerDisposition, str]] = {
    # ── CARRIED: legality ────────────────────────────────────────────────────────────────────────
    R.PERSONAL_DATA_POLICY_REQUIRED: (
        BlockerDisposition.CARRIED,
        "A data-USE LICENCE, not a review preference. Revision 17 lost this one, and losing it "
        "means a feature built from unlicensed personal data generates. No amount of formula "
        "review makes the use lawful"),
    R.EXECUTION_AUTHORITY_UNMET: (
        BlockerDisposition.CARRIED,
        "Read scope. The caller may not read what the feature reads, and a governed formula does "
        "not grant permission it did not have"),
    # ── CARRIED: the two EXECUTION-CHAIN facts (S4/S8) ───────────────────────────────────────────
    # These two are gated by `evaluate_generate` and are NOT emitted by the activation policy —
    # see `EVALUATOR_ONLY_BLOCKERS` below for why that is a property of what each layer can know,
    # rather than a gap in either.
    R.POLICY_REFERENCE_UNRESOLVABLE: (
        BlockerDisposition.CARRIED,
        "A governed policy the formula declares has no current realization, so the computation "
        "cannot be EXECUTED at all: a policy is applied by reading its columns under a realization, "
        "and there is none. Not a review preference — nothing about confirming a column's meaning "
        "produces the missing realization"),
    R.RENDERER_CANNOT_DISPATCH: (
        BlockerDisposition.CARRIED,
        "This build's renderer has no branch for an operator the graph contains, so generating "
        "would emit a project that cannot run. Distinct from FORMULA_SCHEMA_UNSUPPORTED, which is "
        "about the wire version rather than about what the renderer can emit"),
    R.ARTIFACT_NOT_SERVABLE: (
        BlockerDisposition.CARRIED,
        "The sealed artifact's subgraph check REFUSED it — a missing FX duplicate-rate gate, say — "
        "so executing it would run a graph the check declined. Verifying it anyway would produce a "
        "passing verification for a computation nobody was willing to seal"),
    R.ENVIRONMENT_INCOMPATIBLE: (
        BlockerDisposition.CARRIED,
        "The artifact was sealed for one environment and is being run against another's inventory. "
        "Environment is deployment placement, so this is the wrong ARTIFACT rather than an "
        "under-configured run, and its physical targets belong to a cluster nobody rendered for"),
    R.VERIFICATION_NOT_CURRENT: (
        BlockerDisposition.CARRIED,
        "Publication rests on a CURRENT passing verification (§0.3). A stale one vouches for an "
        "artifact whose meaning moved, and an unverifiable one never vouched for anything on "
        "content — neither is a basis for making something visible"),
    R.PUBLICATION_CAPABILITY_MISSING: (
        BlockerDisposition.CARRIED,
        "Publication REQUIRES a capability attestation and verification must not (§0.3). It is the "
        "single thing that separates the two actions, so a publish without one is an action nobody "
        "was entitled to take"),
    R.EXECUTION_AUTHORITY_UNEVALUATED: (
        BlockerDisposition.CARRIED,
        "Read scope was never checked. Unevaluated is not permitted — an unasked question has no "
        "affirmative answer"),

    # ── CARRIED: possibility ─────────────────────────────────────────────────────────────────────
    R.BINDING_NOT_BOUND: (
        BlockerDisposition.CARRIED,
        "Binding completeness, named by the plan as carried. An unbound ref resolves to no column, "
        "so there is nothing to compute"),
    R.PHYSICAL_PLAN_MISSING: (
        BlockerDisposition.CARRIED,
        "No physical plan means no compilation; there is nothing to generate"),
    R.CONCEPTUAL_PATTERN_NOT_AUTHORABLE: (
        BlockerDisposition.CARRIED,
        "Nothing can author this pattern — a structural limit of the language, not an opinion "
        "about the feature"),
    R.FORMULA_SCHEMA_UNSUPPORTED: (
        BlockerDisposition.CARRIED,
        "Renderer-dispatchable: the renderer has no branch for this operator, so generated code "
        "would not run"),
    R.UOA_MISMATCH: (
        BlockerDisposition.CARRIED,
        "The unit of analysis disagrees with the declared grain; the numbers would be computed per "
        "one thing and published per another"),
    R.SNAPSHOT_STALE_REGENERATE: (
        BlockerDisposition.CARRIED,
        "The sealed metadata snapshot no longer matches the catalog, so the facts the formula was "
        "authored against are not the current ones (C-A3c makes this fire for measure items)"),
    R.ACTIVATION_STATE_DRIFTED: (
        BlockerDisposition.CARRIED,
        "The activation state moved after the decision was read; proceeding would act on a world "
        "that no longer exists"),
    R.RECIPE_REVIEW_NOT_CURRENT: (
        BlockerDisposition.CARRIED,
        "The reviewed blueprint is what a governed V2 formula STANDS ON — dropping this would "
        "remove the very thing that justifies dropping the semantic-confirmation gates below. "
        "C-A9's re-pin gate exists for the same reason"),

    # ── DROPPED: human semantic confirmation ─────────────────────────────────────────────────────
    R.READINESS_NOT_MATERIALIZATION_READY: (
        BlockerDisposition.DROPPED,
        "§6 (four-stage ruling 2026-08-22): recipe readiness is a discovery/maturity PROJECTION "
        "and stopped authorizing executable actions. Every enforcing fact the ladder folded into "
        "it gates by its own name — review by RECIPE_REVIEW_NOT_CURRENT, the engine by "
        "FORMULA_SCHEMA_UNSUPPORTED, validation by EXTERNAL_VALIDATION_OUTSTANDING's own action — "
        "so dropping the summary loses nothing that refuses"),
    R.PROPOSED_METADATA_ONLY: (
        BlockerDisposition.DROPPED,
        "Human semantic confirmation of column meaning. A governed V2 formula stands on a reviewed "
        "recipe blueprint, which is a stronger and more reusable statement than per-column "
        "confirmation — this is the gate the reviewed blueprint replaces"),
    R.SEMANTIC_AUTHORITY_INSUFFICIENT: (
        BlockerDisposition.DROPPED,
        "The same gate at the authority tier: nobody confirmed the semantics to the required "
        "level. Replaced by blueprint review, for the same reason"),
    R.FORMULA_NOT_REVIEWED: (
        BlockerDisposition.DROPPED,
        "Per-FORMULA human review. C-A5's deterministic path authors from an already-reviewed "
        "blueprint and records `REVIEW_BYPASSED` rather than claiming a critic ran — the review "
        "moved upstream to the recipe, it did not disappear"),
    R.FORMULA_REVIEW_UNMEASURED: (
        BlockerDisposition.DROPPED,
        "§6: the honest half of what FORMULA_SCHEMA_UNSUPPORTED falsely claimed — engine support "
        "unmeasured BECAUSE unreviewed. Review is not this chain's gate (see FORMULA_NOT_REVIEWED "
        "directly above), and the governance fact rides that code, not this one"),
    R.ENGINE_CAPABILITY_UNMEASURED: (
        BlockerDisposition.DROPPED,
        "§6: a serving-time engine projection that did not run. The chain measures the engine "
        "ITSELF at generate (RENDERER_CANNOT_DISPATCH is evaluator-only and CARRIED); the "
        "fail-closed weight of an unmeasured projection sits on the production acts in "
        "action_dispositions, which this chain never performs"),
    R.EXTERNAL_VALIDATION_OUTSTANDING: (
        BlockerDisposition.DROPPED,
        "Partner-facing external attestation, which gates a DIFFERENT action (Delivery I) and is "
        "not a requirement of generate, verify or publish-sandbox. Dropped from THESE three "
        "evaluators, not from the system"),
}


@dataclass(frozen=True, slots=True)
class EvaluatorVerdictV1:
    """Whether an action may proceed, and every blocker standing in its way."""

    action: EvaluatorAction
    allowed: bool
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.allowed and self.blockers:
            raise ValueError(
                f"{self.action.value} is allowed while carrying {len(self.blockers)} blocker(s): a "
                f"caller reading only `allowed` would proceed past a gate that refused")
        if not self.allowed and not self.blockers:
            raise ValueError(
                f"{self.action.value} is refused with no blocker: the caller cannot tell a user "
                f"what to fix, and cannot tell a refusal from a crash")
        unknown = sorted(set(self.blockers) - set(ACTIVATION_BLOCKER_DISPOSITIONS))
        if unknown:
            raise ValueError(
                f"blocker code(s) {unknown} have no disposition in this build. A code with no "
                f"CARRIED/DROPPED decision is exactly what revision 17 shipped when it lost "
                f"PERSONAL_DATA_POLICY_REQUIRED")


#: The blockers `evaluate_generate` gates on that the ACTIVATION POLICY does not emit.
#:
#: Not a gap in either layer — a property of what each can know. The activation policy answers
#: "is this CANDIDATE ready", from the catalog and the review state; whether a governed policy
#: reference resolves and whether this build's renderer can emit an operator are facts about the
#: EXECUTION CHAIN, discovered while compiling one. Asking the activation policy for them would
#: mean compiling every candidate to answer a list query.
#:
#: Named as a constant so the exhaustiveness test can state the real rule — the table covers
#: everything the activation policy emits PLUS exactly these — rather than being loosened to a
#: superset check that would let a genuinely lost code back in.
EVALUATOR_ONLY_BLOCKERS: frozenset[str] = frozenset({
    R.POLICY_REFERENCE_UNRESOLVABLE,
    R.RENDERER_CANNOT_DISPATCH,
    R.ARTIFACT_NOT_SERVABLE,
    R.ENVIRONMENT_INCOMPATIBLE,
    R.VERIFICATION_NOT_CURRENT,
    R.PUBLICATION_CAPABILITY_MISSING,
})


def carried_blockers(codes: Mapping[str, Any] | tuple[str, ...]) -> tuple[str, ...]:
    """Those of ``codes`` these evaluators actually gate on, sorted.

    Raises:
        KeyError: a code with no disposition — never silently ignored, because silently ignoring
            one is the defect this table exists to prevent.
    """
    return tuple(sorted(
        code for code in codes
        if ACTIVATION_BLOCKER_DISPOSITIONS[code][0] is BlockerDisposition.CARRIED))


class GenerateEvaluator(Protocol):
    """§0.3's Generate: selected revision · complete binding · valid formula · resolvable policies ·
    renderer-dispatchable. Implementation ships at S8."""

    def evaluate_generate(
        self, *, generation_authorization_revision_id: str,
    ) -> EvaluatorVerdictV1: ...


class VerifyEvaluator(Protocol):
    """§0.3's Verify: the exact sealed artifact · execution permission · environment compatibility.
    Implementation ships at S9."""

    def evaluate_verify(
        self, *, sealed_artifact_hash: str, inventory_observation_id: str,
    ) -> EvaluatorVerdictV1: ...


class PublishSandboxEvaluator(Protocol):
    """§0.3's Publish sandbox: a current passing verification · the exact staging output ·
    publication permission and capability. Implementation ships at S10."""

    def evaluate_publish_sandbox(
        self, *, verified_output_revision_id: str, staging_path: str,
    ) -> EvaluatorVerdictV1: ...
