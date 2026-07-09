"""Supporting dimension registries — the closed, non-``use_case`` vocabularies.

Applicability narrows only on the ``use_case`` tree (``use_cases.py``); the *other* dimensions in spec
§1 are flat, closed vocabularies that route legacy tags to their true home rather than being forced
into the tree. Each is a ``frozenset[str]`` of governed ids, and ``DIMENSIONS`` maps the dimension name
to its vocabulary so the Task-3 crosswalk can validate a ``(dimension, target)`` pair generically.

Authored verbatim from ``docs/superpowers/specs/2026-07-09-usecase-taxonomy-crosswalk-draft.md`` §1
(the dimensions table) and §5 (the reclassification note). Six vocabularies:

* ``modelling_context`` — regulatory framework/regime (``xva``/``lgd`` included per the owner's call).
* ``measure`` — an output *quantity*, not an objective (kept separate from modelling_context — §5 note).
* ``product_context`` — product/asset/channel.
* ``typology`` — a specific pattern within a use-case.
* ``journey_stage`` — position in a broader journey.
* ``business_outcome`` — the benefit, not the target.

(The ``metadata`` row in §1 — ownership & capability tags — is deliberately *not* a closed vocabulary
here; it is free-form ownership metadata, not a governed dimension applicability routes on.)

Behaviour-neutral: read-only registries, nothing here touches ``templates.py`` or grounding.
``_validate_dimensions()`` runs at import — the six sets must be non-empty and **pairwise disjoint**
(no id may live in two dimensions, so a crosswalk target is unambiguous).
"""
from __future__ import annotations

from itertools import combinations

# Regulatory framework / regime a model is built under (xva, lgd included per owner — §5).
MODELLING_CONTEXTS: frozenset[str] = frozenset(
    {"ifrs9", "frtb", "xva", "lcr", "nsfr", "lgd", "irrbb", "ftp"})

# An output quantity, not an objective — kept its own dimension so it never masquerades as a use-case.
MEASURES: frozenset[str] = frozenset({"tracking_error", "data_quality"})

# Product / asset / channel context (e.g. crypto is context, not a use-case — D4).
PRODUCT_CONTEXTS: frozenset[str] = frozenset(
    {"deposits", "credit_cards", "mortgages", "crypto_assets", "derivatives"})

# A specific pattern within a use-case (the "how", not the objective).
TYPOLOGIES: frozenset[str] = frozenset({
    "app_scam",
    "mule_account",
    "synthetic_id",
    "account_takeover",
    "crypto_asset_laundering",
    "trade_based_money_laundering",
})

# Position in a broader journey (unbundling, primacy_erosion route here — D6).
JOURNEY_STAGES: frozenset[str] = frozenset(
    {"disengagement", "unbundling", "primacy_erosion", "outflow", "closure"})

# The benefit, not the target (cost_efficiency is an outcome, not pricing — D5).
BUSINESS_OUTCOMES: frozenset[str] = frozenset(
    {"revenue_growth", "cost_efficiency", "loss_reduction"})

# Dimension name -> its closed vocabulary. The Task-3 crosswalk validates (dimension, target) pairs
# against this map; disjointness (enforced below) makes any target's owning dimension unambiguous.
DIMENSIONS: dict[str, frozenset[str]] = {
    "modelling_context": MODELLING_CONTEXTS,
    "measure": MEASURES,
    "product_context": PRODUCT_CONTEXTS,
    "typology": TYPOLOGIES,
    "journey_stage": JOURNEY_STAGES,
    "business_outcome": BUSINESS_OUTCOMES,
}


def _validate_dimensions() -> None:
    """Fail fast at import if a vocabulary drifts: none may be empty, and no id may appear in two
    dimensions (pairwise disjoint) — otherwise a crosswalk target would have an ambiguous home."""
    for name, members in DIMENSIONS.items():
        if not members:
            raise ValueError(f"dimension {name!r} vocabulary is empty")
    for (name_a, set_a), (name_b, set_b) in combinations(DIMENSIONS.items(), 2):
        overlap = set_a & set_b
        if overlap:
            raise ValueError(
                f"dimensions {name_a!r} and {name_b!r} share value(s) {sorted(overlap)!r}")


_validate_dimensions()


def is_known(dimension: str, value: str) -> bool:
    """True iff ``value`` is a governed member of ``dimension``'s closed vocabulary. An unknown
    dimension resolves to the empty set, so it returns False rather than raising."""
    return value in DIMENSIONS.get(dimension, frozenset())
