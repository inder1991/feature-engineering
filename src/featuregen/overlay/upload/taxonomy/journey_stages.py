"""Phase-2A Task A1 — the OPTIONAL, controlled journey vocabulary.

Journey metadata is genuinely optional (Global Constraint: journey OPTIONAL). Only recipes that sit on a
**genuine funnel** — a directed sequence of escalating states a customer / exposure moves through — get
a ``(journey_model_id, journey_stage_id)``. Everything else (pricing / economics, actuarial /
underwriting, custody holdings, capital & liquidity measures, ESG scoring, market-risk measures,
payments ops, settlement, sharia compliance, baseline / context, ...) keeps BOTH null. We NEVER force a
journey onto a recipe that has none — there is deliberately no "every recipe must have a journey" rule.

Each template carries a free-form authoring ``stage`` string, but that string is (a) **ambiguous across
funnels** — ``3-arrears`` is the *insurance-lapse* arrears stage, not credit; ``2-disengagement`` is used
by churn, redemption AND lapse — and (b) not a governed id. So we resolve a controlled ``(model, stage)``
in two steps:

1. the funnel **model** is selected by the recipe's PRIMARY use-case (its funnel family), and
2. the **stage** within that model by the raw ``stage`` string, via a per-model stage map.

A recipe whose primary use-case is not a funnel family — or whose stage is not a mapped funnel position
of that family — resolves to ``(None, None)``. Use-case → model is one-to-one (validated at import), so
the selection is deterministic.

Invariant (validated at import + enforced on every :class:`JourneyMetadata`): a set ``journey_stage_id``
REQUIRES a set ``journey_model_id`` and must be a declared member of that model. A model may declare
member stages that no current recipe reaches (e.g. ``customer_attrition.primacy_loss``); the invariant is
one-directional — every emitted stage is a member, not every member is emitted.

Behaviour-neutral: read-only registries; nothing here touches grounding or the considered-set.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.templates import Template


@dataclass(frozen=True, slots=True)
class _JourneyModel:
    """One controlled funnel: its ordered member ``stages``, the primary ``use_cases`` that select it,
    and ``stage_map`` from a template's free-form ``stage`` string to a governed member stage id."""

    model_id: str
    stages: tuple[str, ...]
    use_cases: frozenset[str]
    stage_map: dict[str, str]


# The controlled funnels, authored from the ``stage`` data actually present in ``ALL_TEMPLATES`` (each
# stage_map key is a real authoring stage). Every model's member vocabulary is the plan's named stages;
# a member with no stage_map key (e.g. ``primacy_loss``) is a valid but currently-unreached stage.
_JOURNEY_MODELS: tuple[_JourneyModel, ...] = (
    _JourneyModel(
        model_id="customer_attrition",
        stages=("engagement_decline", "financial_migration", "unbundling", "primacy_loss"),
        use_cases=frozenset({"retail_churn"}),
        stage_map={
            "2-disengagement": "engagement_decline",
            "3-financial-migration": "financial_migration",
            "4-unbundling": "unbundling",
        },
    ),
    _JourneyModel(
        model_id="credit_deterioration",
        stages=("early_stress", "emerging_distress", "arrears", "delinquency", "default"),
        use_cases=frozenset({"credit_risk"}),
        stage_map={
            "1-early-stress": "early_stress",
            "2-emerging-distress": "emerging_distress",
            "3-arrears": "arrears",
            "3-delinquency": "delinquency",
            "4-default-adjacent": "default",
        },
    ),
    _JourneyModel(
        model_id="collections",
        stages=("early_dpd", "mid_dpd", "late_dpd", "recovery"),
        use_cases=frozenset({"collections", "recoveries"}),
        stage_map={
            "early-1-29-dpd": "early_dpd",
            "mid-30-89-dpd": "mid_dpd",
            "late-90-plus-dpd": "late_dpd",
            "recovery-charge-off": "recovery",
        },
    ),
    _JourneyModel(
        model_id="fraud_kill_chain",
        stages=("recon", "access", "setup", "cash_out"),
        use_cases=frozenset({"fraud"}),
        stage_map={
            "1-recon": "recon",
            "2-access-takeover": "access",
            "3-setup-staging": "setup",
            "4-cash-out": "cash_out",
        },
    ),
    _JourneyModel(
        model_id="aml_cycle",
        stages=("placement", "layering", "integration"),
        use_cases=frozenset({"aml"}),
        stage_map={
            "placement": "placement",
            "layering": "layering",
            "integration": "integration",
        },
    ),
    _JourneyModel(
        model_id="deposit_stability",
        stages=("stable_core", "rate_sensitive", "surge_hot_money", "runoff_prone"),
        use_cases=frozenset({"deposit_stability"}),
        stage_map={
            "stable-core": "stable_core",
            "rate-sensitive": "rate_sensitive",
            "surge-hot-money": "surge_hot_money",
            "runoff-prone": "runoff_prone",
        },
    ),
    _JourneyModel(
        model_id="redemption",
        stages=("invested", "redemption_risk"),
        use_cases=frozenset({"redemption_risk"}),
        stage_map={
            "1-invested": "invested",
            "3-redemption-risk": "redemption_risk",
        },
    ),
    _JourneyModel(
        model_id="insurance_lapse",
        stages=("premium_stress", "arrears"),
        use_cases=frozenset({"lapse_risk"}),
        stage_map={
            "2-disengagement": "premium_stress",
            "3-arrears": "arrears",
        },
    ),
)

# model_id -> model, for membership checks + external validation.
JOURNEY_MODELS: dict[str, _JourneyModel] = {model.model_id: model for model in _JOURNEY_MODELS}


def _validate_registry() -> None:
    """Fail fast at import if the registry drifts: every stage_map value must be a declared member, no
    model may repeat a stage, and use-case selectors must be pairwise disjoint (so a primary use-case
    selects a UNIQUE model — otherwise resolution would be order-dependent)."""
    seen_use_case: dict[str, str] = {}
    for model in _JOURNEY_MODELS:
        if len(model.stages) != len(set(model.stages)):
            raise ValueError(f"journey model {model.model_id!r} declares duplicate stages")
        members = set(model.stages)
        for raw_stage, stage_id in model.stage_map.items():
            if stage_id not in members:
                raise ValueError(
                    f"journey model {model.model_id!r} maps {raw_stage!r} -> {stage_id!r}, "
                    f"which is not a declared member stage")
        for use_case in model.use_cases:
            if use_case in seen_use_case:
                raise ValueError(
                    f"use-case {use_case!r} selects two journey models "
                    f"({seen_use_case[use_case]!r} and {model.model_id!r})")
            seen_use_case[use_case] = model.model_id


_validate_registry()


@dataclass(frozen=True, slots=True)
class JourneyMetadata:
    """A recipe's OPTIONAL position on a controlled journey. BOTH null = the recipe has no meaningful
    journey (the common case). The invariant — a set ``journey_stage_id`` requires a set,
    membership-valid ``journey_model_id`` — is enforced in ``__post_init__`` so an invalid pairing (a
    stage without a model, or a stage that is not a member of its model) cannot be constructed."""

    journey_model_id: str | None
    journey_stage_id: str | None

    def __post_init__(self) -> None:
        if self.journey_stage_id is None:
            return
        if self.journey_model_id is None:
            raise ValueError("journey_stage_id set without a journey_model_id")
        model = JOURNEY_MODELS.get(self.journey_model_id)
        if model is None or self.journey_stage_id not in model.stages:
            raise ValueError(
                f"journey_stage_id {self.journey_stage_id!r} is not a member of journey model "
                f"{self.journey_model_id!r}")


_NO_JOURNEY = JourneyMetadata(None, None)


def journey_metadata(t: Template) -> JourneyMetadata:
    """Map a template to its controlled ``(journey_model_id, journey_stage_id)`` — or ``(None, None)``.

    The funnel model is selected by the template's PRIMARY use-case (its funnel family); the stage is
    the per-model mapping of the raw ``stage`` string. A recipe whose primary use-case is not a funnel
    family, or which sits on a stage that is not a mapped funnel position of that family, gets no journey
    (we never force one).
    """
    primary_use_case = t.use_cases[0] if t.use_cases else None
    if primary_use_case is not None:
        for model in _JOURNEY_MODELS:
            if primary_use_case in model.use_cases:
                stage_id = model.stage_map.get(t.stage)
                if stage_id is not None:
                    return JourneyMetadata(model.model_id, stage_id)
                # Genuine funnel family, but this recipe sits on a non-funnel stage -> no journey.
                return _NO_JOURNEY
    return _NO_JOURNEY
