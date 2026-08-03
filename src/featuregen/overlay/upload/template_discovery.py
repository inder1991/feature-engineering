"""Per-template discovery metadata (Task 1, freeze 0F-5): the governed mapping from
``template_id`` to feature-category / business-domain / canonical-use-case / keyword /
business-value assignments, with per-value basis + evidence and an explicit aggregate
disposition.

This registry ADDS discovery metadata only. It duplicates nothing from ``Template``
(``intent``, ``family``, ``stage``, ``eligibility``, ``near_label``, needs, params stay in
``templates.py``), and ``Template.family`` remains ``recipe_family`` — never renamed or
overloaded.

Frozen identity rules (0F-5 / 0F-10 / D5):

* ``recipe_revision_id`` REUSES the existing ``template_content_hash``
  (``canonical-recipe-v1``) — a reference to an existing verified content hash, never a second
  hash scheme. A discovery edit (keyword, category, use-case mapping) can never alter it.
* ``discovery_metadata_revision_id`` is the SEPARATE hash over the entry's controlled IDs,
  text values, per-value basis and canonicalized evidence axes (0F-4 rule 3: occurrence
  provenance excluded), sorted — so display order never moves identity and replayed evidence
  changes no revision.
Citation rule (per-value, never one template-level authority): a value cites what actually
produced it. A canonical use case is read straight out of ``Template.primary_objective`` and
cites the recipe revision; a ``feature_category`` is DERIVED here by
:data:`FAMILY_FEATURE_CATEGORY_MAPPING` and cites that mapping's content hash (alongside the
recipe revision its ``family`` was read from), so a taxonomy-derived category is never
presented as a value the recipe itself attested.

Legacy migration rule: the 107 legacy ``Template.use_cases`` tags are NEVER silently converted
into canonical use-case IDs. They are consumed only through the reviewed
``LEGACY_TAG_CROSSWALK`` and recorded as migration evidence in the audit manifest
(``expected_legacy_audit_rows`` / the checked-in fixture), whose proposed-mapping column stays
explicitly ``UNRESOLVED`` until a later human/LLM pass passes the canonical validator.

Business domains: the shared controlled business-domain registry is owned by the unlanded
semantic plan (D9). No controlled domain ID can be assigned here — ``business_domains`` stays
empty and the absence is honest (disposition ``partial``/``unclassified``), never guessed and
never a local look-alike registry.

The bounded, audited LLM proposal batch seam also lives here (see the "LLM proposal batch"
section): full support path, ``basis=llm_proposed`` with ``operational_influence=hint``, and
NO live dispatch from import, tests or page reads.
"""
from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from featuregen.canonical import contract_hash_v1
from featuregen.contracts.contract_versions import register_contract_version
from featuregen.contracts.evidence_axes import (
    EvidenceAuthorityV1,
    canonical_evidence_axes,
)
from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    canonical_template,
    content_hash,
)
from featuregen.overlay.upload.suggestion_taxonomy import (
    FEATURE_CATEGORY_REGISTRY,
    TaxonomyValidationError,
    safe_registry_text,
)
from featuregen.overlay.upload.taxonomy.legacy_crosswalk import LEGACY_TAG_CROSSWALK
from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves
from featuregen.overlay.upload.templates import ALL_TEMPLATES, Template

__all__ = [
    "DISCOVERY_BASIS_VALUES",
    "DISCOVERY_DISPOSITIONS",
    "DISCOVERY_METADATA",
    "FAMILY_FEATURE_CATEGORY_MAPPING",
    "FAMILY_MAPPING_CITATION_PREFIX",
    "RECIPE_REVISION_CITATION_PREFIX",
    "family_feature_category_mapping_content_hash",
    "is_derived_from_family_mapping",
    "DISCOVERY_PROPOSAL_ABSTAIN",
    "DISCOVERY_PROPOSAL_EGRESS_FIELDS",
    "DISCOVERY_PROPOSAL_PROMPT_ID",
    "DISCOVERY_PROPOSAL_SCHEMA_ID",
    "DISCOVERY_PROPOSAL_TASK",
    "DiscoveryProposalAssignments",
    "DiscoveryProposalBatchResult",
    "DiscoveryProposalProvenanceV1",
    "DiscoveryProposalResultV1",
    "DiscoveryProposalV1",
    "discovery_proposal_items",
    "proposal_assignments",
    "run_discovery_proposal_batch",
    "templates_needing_proposal",
    "MAX_BUSINESS_VALUE_LEN",
    "MAX_KEYWORDS",
    "MAX_KEYWORD_LEN",
    "TEMPLATE_DISCOVERY_OWNER",
    "UNRESOLVED_MAPPING",
    "DiscoveryControlledAssignmentV1",
    "DiscoveryTextAssignmentV1",
    "TemplateDiscoveryMetadataV1",
    "compute_discovery_disposition",
    "discovery_metadata_revision_id",
    "discovery_registry_content_hash",
    "expected_legacy_audit_rows",
    "legacy_audit_manifest_content_hash",
    "recipe_revision_id",
    "validate_discovery_entries",
    "validate_legacy_audit_manifest",
]

TEMPLATE_DISCOVERY_OWNER = "featuregen.overlay.upload.template_discovery"

#: Frozen basis vocabulary for DISCOVERY assignments (0F-5) — narrower than the attributed-value
#: vocabulary (no ``catalog_resolved``: discovery metadata is authored/edited/proposed, never
#: resolved from catalog wording).
DISCOVERY_BASIS_VALUES = frozenset({"template_authored", "human", "llm_proposed"})

#: Coverage summary only — never a substitute for per-value basis/evidence (0F-5).
DISCOVERY_DISPOSITIONS = frozenset({"complete", "partial", "unclassified", "needs_sme"})

#: The one operational influence an LLM-proposed discovery value may carry (brief: a hint,
#: never a fabricated governed authority label). Authored/human discovery values carry ``None``
#: in v1 (0F-5: "discovery hint only in v1").
_LLM_PROPOSED_INFLUENCE = "hint"

MAX_KEYWORDS = 8
MAX_KEYWORD_LEN = 40
MAX_BUSINESS_VALUE_LEN = 400


def _check_basis_and_influence(basis: str, operational_influence: str | None) -> None:
    if basis not in DISCOVERY_BASIS_VALUES:
        raise TaxonomyValidationError(
            f"discovery basis must be one of {sorted(DISCOVERY_BASIS_VALUES)}, got {basis!r}")
    if basis == "llm_proposed":
        if operational_influence != _LLM_PROPOSED_INFLUENCE:
            raise TaxonomyValidationError(
                "an llm_proposed discovery value carries operational_influence='hint' — never "
                f"governed authority and never silently nothing; got {operational_influence!r}")
    elif operational_influence is not None:
        raise TaxonomyValidationError(
            f"a {basis} discovery value carries no operational influence in v1 (0F-5), got "
            f"{operational_influence!r}")


def _check_evidence(evidence: object, *, what: str) -> None:
    if not isinstance(evidence, tuple) or not evidence or not all(
            isinstance(e, EvidenceAuthorityV1) for e in evidence):
        raise TaxonomyValidationError(
            f"{what} requires a non-empty tuple of EvidenceAuthorityV1 evidence")


@dataclass(frozen=True, slots=True)
class DiscoveryControlledAssignmentV1:
    """One CONTROLLED-ID discovery assignment with per-value provenance (0F-5, verbatim)."""

    controlled_id: str
    basis: str                               # template_authored | human | llm_proposed
    evidence: tuple[EvidenceAuthorityV1, ...]
    operational_influence: str | None        # discovery hint only in v1

    def __post_init__(self) -> None:
        if not isinstance(self.controlled_id, str) or not self.controlled_id.strip():
            raise TaxonomyValidationError("controlled_id must be a non-empty string")
        _check_basis_and_influence(self.basis, self.operational_influence)
        _check_evidence(self.evidence, what="a controlled discovery assignment")


@dataclass(frozen=True, slots=True)
class DiscoveryTextAssignmentV1:
    """One bounded plain-TEXT discovery value (keyword / business value) with provenance.
    The value is NFC-normalized at construction; blank, control-character or HTML-bearing text
    can never be marked mapped (it fails to construct)."""

    value: str
    basis: str
    evidence: tuple[EvidenceAuthorityV1, ...]
    operational_influence: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", safe_registry_text(
            self.value, field="discovery text value", max_len=MAX_BUSINESS_VALUE_LEN))
        _check_basis_and_influence(self.basis, self.operational_influence)
        _check_evidence(self.evidence, what="a text discovery assignment")


@dataclass(frozen=True, slots=True)
class TemplateDiscoveryMetadataV1:
    """The discovery-metadata entry for one template (0F-5, verbatim). ``disposition``
    summarizes coverage only; per-value basis/evidence always travels on the assignments."""

    template_id: str
    feature_category: DiscoveryControlledAssignmentV1 | None
    business_domains: tuple[DiscoveryControlledAssignmentV1, ...]
    canonical_use_cases: tuple[DiscoveryControlledAssignmentV1, ...]
    keywords: tuple[DiscoveryTextAssignmentV1, ...]
    business_value: DiscoveryTextAssignmentV1 | None
    disposition: str                         # complete | partial | unclassified | needs_sme

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str) or not self.template_id:
            raise TaxonomyValidationError("template_id must be a non-empty string")
        if self.disposition not in DISCOVERY_DISPOSITIONS:
            raise TaxonomyValidationError(
                f"disposition must be one of {sorted(DISCOVERY_DISPOSITIONS)}, got "
                f"{self.disposition!r}")


# ──────────────────────────────────────────────────────────────────────────────────────────────
# Identity: recipe revision reuse (D5) + the separate discovery revision
# ──────────────────────────────────────────────────────────────────────────────────────────────
def recipe_revision_id(template: Template) -> str:
    """The template's computational/authored recipe identity — the EXISTING
    ``template_content_hash`` (``canonical-recipe-v1``), referenced, never re-minted (D5).
    Keyword/category/use-case mapping edits live in the discovery registry and can never
    alter this value."""
    return content_hash(canonical_template(template))


def _canonical_controlled(assignment: DiscoveryControlledAssignmentV1) -> dict:
    return {
        "controlled_id": assignment.controlled_id,
        "basis": assignment.basis,
        "evidence": canonical_evidence_axes(assignment.evidence),
    }


def _canonical_text(assignment: DiscoveryTextAssignmentV1) -> dict:
    return {
        "value": assignment.value,        # already NFC-normalized at construction
        "basis": assignment.basis,
        "evidence": canonical_evidence_axes(assignment.evidence),
    }


def _sorted_payload(items: Iterable[dict]) -> list[dict]:
    return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))


def discovery_metadata_revision_id(entry: TemplateDiscoveryMetadataV1) -> str:
    """0F-5: the entry's controlled IDs, text values, per-value basis and canonicalized
    evidence axes, sorted. Occurrence provenance (``producer_ref``/``evidence_id``),
    display order and the derived ``disposition`` summary are excluded, so reordering and
    evidence replay never move the revision."""
    payload = {
        "feature_category": (_canonical_controlled(entry.feature_category)
                             if entry.feature_category else None),
        "business_domains": _sorted_payload(
            _canonical_controlled(a) for a in entry.business_domains),
        "canonical_use_cases": _sorted_payload(
            _canonical_controlled(a) for a in entry.canonical_use_cases),
        "keywords": _sorted_payload(_canonical_text(k) for k in entry.keywords),
        "business_value": (_canonical_text(entry.business_value)
                           if entry.business_value else None),
    }
    return contract_hash_v1("template-discovery-metadata", "1", payload)


def discovery_registry_content_hash(
        entries: Iterable[TemplateDiscoveryMetadataV1] | None = None) -> str:
    """Registry-wide fingerprint over sorted ``[template_id, entry-revision]`` pairs — a
    rebuild/fencing input, never suggestion identity (0F-5)."""
    seq = list(entries) if entries is not None else list(DISCOVERY_METADATA.values())
    pairs = sorted([e.template_id, discovery_metadata_revision_id(e)] for e in seq)
    return contract_hash_v1("template-discovery-registry", "1", {"entries": pairs})


# ──────────────────────────────────────────────────────────────────────────────────────────────
# Disposition — a computed coverage summary, never invented
# ──────────────────────────────────────────────────────────────────────────────────────────────
def compute_discovery_disposition(
        *,
        feature_category: DiscoveryControlledAssignmentV1 | None,
        business_domains: tuple[DiscoveryControlledAssignmentV1, ...],
        canonical_use_cases: tuple[DiscoveryControlledAssignmentV1, ...],
        keywords: tuple[DiscoveryTextAssignmentV1, ...],
        business_value: DiscoveryTextAssignmentV1 | None) -> str:
    """``complete`` needs category + domains + use cases + business value; anything present
    short of that is ``partial``; nothing present is ``unclassified``. ``needs_sme`` is an
    authored HUMAN escalation, never computed — fabricating it would be inventing a review."""
    if feature_category and business_domains and canonical_use_cases and business_value:
        return "complete"
    if (feature_category or business_domains or canonical_use_cases or keywords
            or business_value):
        return "partial"
    return "unclassified"


# ──────────────────────────────────────────────────────────────────────────────────────────────
# The authored family → feature-category mapping (deterministic; ambiguous families stay out)
# ──────────────────────────────────────────────────────────────────────────────────────────────
# Reviewed against the real 157-template inventory (id/family/aggregation dump in the Task-1
# report). A family appears here ONLY when every template in it shares one computation shape
# deterministically derivable from its authored family/aggregation; mixed-shape families
# (e.g. balance_stock = trend + days-below + volatility) are deliberately ABSENT and their
# templates stay explicit ``unclassified`` (rule 12: coverage targets never force invented
# metadata).
#
# This table — NOT the recipe — is what produces a ``feature_category``. It is authored in this
# module by the taxonomy owner, so every category assignment cites its content hash
# (:func:`family_feature_category_mapping_content_hash`) and stays distinguishable from a value
# an SME wrote into the recipe.
FAMILY_FEATURE_CATEGORY_MAPPING: dict[str, str] = {
    # ratio & share
    "cashflow_ratio": "ratio", "charge_off": "ratio", "collateral": "ratio",
    "corridor_mix": "ratio", "cost_to_collect": "ratio", "data_quality": "ratio",
    "fee_competitiveness": "ratio", "guarantor_support": "ratio", "hot_money": "ratio",
    "lcr_liquidity": "ratio", "matching": "ratio", "nsfr_funding": "ratio",
    "peer_penetration": "ratio", "promise_to_pay": "ratio", "recovery": "ratio",
    "settlement_fail": "ratio", "settlement_quality": "ratio", "share_of_wallet": "ratio",
    "sharia_profit_sharing": "ratio", "sharia_purification": "ratio",
    "sharia_screening": "ratio", "sustainability_linked": "ratio", "taxonomy": "ratio",
    "unbundling": "ratio",
    # trend & trajectory
    "activity_trend": "trend", "clv_revenue": "trend", "cure_reage": "trend",
    "exposure_provisioning": "trend", "primacy_outflow": "trend", "redemption_flow": "trend",
    "roll_rate": "trend",
    # recency & timing
    "campaign_response": "recency", "fund_admin_nav": "recency", "recency": "recency",
    "settlement_timing": "recency",
    # frequency & velocity
    "break_behaviour": "frequency", "cross_product_stress": "frequency",
    "forbearance": "frequency", "forbearance_sicr": "frequency",
    "margin_collateral": "frequency", "recon_targeting": "frequency",
    # concentration & diversity
    "bancassurance": "concentration", "channel_engagement": "concentration",
    "concentration": "concentration", "distribution_mix": "concentration",
    "funding_concentration": "concentration", "merchant_behaviour": "concentration",
    "obligor_monitoring": "concentration", "product_holding": "concentration",
    "purpose_mix": "concentration", "rail_mix": "concentration",
    "reinsurance": "concentration", "relationship_deepening": "concentration",
    "sukuk": "concentration", "syndication": "concentration", "whitespace": "concentration",
    # volatility & stability
    "aum_stability": "volatility", "nmd_stability": "volatility",
    # utilisation & headroom
    "affordability": "utilisation", "cash_pooling": "utilisation", "covenant": "utilisation",
    "facility_utilisation": "utilisation", "liquidity_coverage": "utilisation",
    "mandate_compliance": "utilisation", "securities_lending": "utilisation",
    "tracking_error": "utilisation", "trading_limits": "utilisation",
    # duration & streak
    "collection_tenure": "duration", "fail_ageing": "duration", "payment_plan": "duration",
    "pre_settlement": "duration", "tenure": "duration", "working_capital": "duration",
    # deviation & anomaly
    "access_takeover": "deviation", "benchmark_tracking": "deviation",
    "cash_out": "deviation", "relative_performance": "deviation",
    "setup_staging": "deviation", "transition": "deviation",
    # composite score
    "claims": "composite", "claims_fraud": "composite", "corporate_actions": "composite",
    "counterparty_deterioration": "composite", "next_best_product": "composite",
    "rfm": "composite", "upsell_readiness": "composite",
    # exposure & level
    "financed_emissions": "exposure", "group_exposure": "exposure",
    "irrbb_repricing": "exposure", "market_risk_measures": "exposure",
    "notional_exposure": "exposure", "physical_risk": "exposure",
    "relationship_aggregation": "exposure", "sensitivities_greeks": "exposure",
}


def _validate_family_mapping() -> None:
    authored = {t.family for t in ALL_TEMPLATES}
    for family, category in FAMILY_FEATURE_CATEGORY_MAPPING.items():
        if family not in authored:
            raise TaxonomyValidationError(
                f"family mapping names unauthored family {family!r}")
        if category not in FEATURE_CATEGORY_REGISTRY:
            raise TaxonomyValidationError(
                f"family {family!r} maps to unknown feature category {category!r}")


def family_feature_category_mapping_content_hash(
        mapping: Mapping[str, str] = FAMILY_FEATURE_CATEGORY_MAPPING) -> str:
    """Content hash of the family→feature-category derivation table: the thing a derived
    category assignment cites, because the table (not the recipe) is what produced it.

    Order-independent (sorted pairs), so authoring order never moves the citation, while any
    real re-derivation decision — adding, removing or re-pointing a family — does."""
    payload = {"mapping": sorted([family, category] for family, category in mapping.items())}
    return contract_hash_v1("family-feature-category-mapping", "1", payload)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# Entry construction (template_authored only; nothing invented)
# ──────────────────────────────────────────────────────────────────────────────────────────────
#: Citation prefixes. A ``producer_ref`` names WHAT produced the value: the recipe the value was
#: read out of, or the taxonomy-owned mapping that derived it. Consumers (Task 2's provenance
#: badge) read these through :func:`is_derived_from_family_mapping` rather than guessing.
RECIPE_REVISION_CITATION_PREFIX = "recipe-revision:"
FAMILY_MAPPING_CITATION_PREFIX = "family-feature-category-mapping:"


def _authored_evidence(template: Template) -> EvidenceAuthorityV1:
    """Values READ OUT OF the recipe (canonical use cases, from ``Template.primary_objective``)
    cite the recipe revision (plan: "Template-authored labels cite recipe_revision_id"). The
    citation is occurrence provenance — excluded from semantic hashes — so re-citing a
    re-revisioned recipe never churns the discovery revision."""
    return EvidenceAuthorityV1(
        producer=EvidenceProducer.TAXONOMY,
        strength=AssertionStrength.ATTESTED,
        lifecycle=EvidenceLifecycle.ACTIVE,
        producer_ref=f"{RECIPE_REVISION_CITATION_PREFIX}{recipe_revision_id(template)}",
        evidence_id=None,
    )


def _derived_category_evidence(template: Template, mapping_hash: str
                               ) -> tuple[EvidenceAuthorityV1, ...]:
    """A ``feature_category`` is DERIVED by :data:`FAMILY_FEATURE_CATEGORY_MAPPING`, not authored
    in the recipe, so it cites that mapping's content hash and the family rule applied — first,
    because that is the producer — and only then the recipe revision its ``family`` was read
    from. ``basis`` stays ``template_authored`` (the derivation is deterministic and
    controller-sanctioned); the CITATION is what keeps it distinguishable from an SME-authored
    objective, so no one can read ``data_quality → ratio`` as "the recipe attests this".

    Both citations carry the same ``(producer, strength, lifecycle)`` axes, so
    :func:`canonical_evidence_axes` dedupes them and the discovery revision is unmoved."""
    return (
        EvidenceAuthorityV1(
            producer=EvidenceProducer.TAXONOMY,
            strength=AssertionStrength.ATTESTED,
            lifecycle=EvidenceLifecycle.ACTIVE,
            producer_ref=(f"{FAMILY_MAPPING_CITATION_PREFIX}{mapping_hash}"
                          f"#family={template.family}"),
            evidence_id=None,
        ),
        _authored_evidence(template),
    )


def is_derived_from_family_mapping(assignment: DiscoveryControlledAssignmentV1) -> bool:
    """True when the assignment was produced by the family→category mapping rather than read
    out of the recipe. The positive signal a provenance badge needs: a derived category and an
    SME-authored objective must never render the same."""
    return any((e.producer_ref or "").startswith(FAMILY_MAPPING_CITATION_PREFIX)
               for e in assignment.evidence)


def _authored_entry(template: Template, *, family_mapping_hash: str
                    ) -> TemplateDiscoveryMetadataV1:
    evidence = (_authored_evidence(template),)
    category_id = FAMILY_FEATURE_CATEGORY_MAPPING.get(template.family)
    feature_category = (DiscoveryControlledAssignmentV1(
        controlled_id=category_id, basis="template_authored",
        evidence=_derived_category_evidence(template, family_mapping_hash),
        operational_influence=None) if category_id else None)
    # Canonical use cases come ONLY from the template's own authored objectives — the 107
    # legacy tags are migration evidence in the audit manifest, never converted here.
    objectives: tuple[str, ...] = ()
    if template.primary_objective:
        seen: dict[str, None] = {}
        for objective in (template.primary_objective, *template.supporting_objectives):
            seen.setdefault(objective, None)
        objectives = tuple(seen)
    canonical_use_cases = tuple(
        DiscoveryControlledAssignmentV1(controlled_id=objective, basis="template_authored",
                                        evidence=evidence, operational_influence=None)
        for objective in objectives)
    business_domains: tuple[DiscoveryControlledAssignmentV1, ...] = ()  # D9: unresolvable
    keywords: tuple[DiscoveryTextAssignmentV1, ...] = ()   # none authored; none fabricated
    business_value = None                                  # none authored; none fabricated
    return TemplateDiscoveryMetadataV1(
        template_id=template.id,
        feature_category=feature_category,
        business_domains=business_domains,
        canonical_use_cases=canonical_use_cases,
        keywords=keywords,
        business_value=business_value,
        disposition=compute_discovery_disposition(
            feature_category=feature_category, business_domains=business_domains,
            canonical_use_cases=canonical_use_cases, keywords=keywords,
            business_value=business_value),
    )


def validate_discovery_entries(entries: Sequence[TemplateDiscoveryMetadataV1],
                               templates: Sequence[Template] = ALL_TEMPLATES) -> None:
    """The 0F-5 import gate. Proves: exact one-entry-per-template coverage (no orphan, no
    duplicate, no missing template); every controlled ID resolves in its OWN registry (a
    plausible legacy string dies here); canonical use cases are selectable leaves with no
    duplicates; keyword/prose bounds; basis/influence/evidence rules; a ``template_authored``
    category cites the mapping that derived it, never a bare recipe attestation; and the
    disposition is the honest computed summary (``needs_sme`` allowed only as an authored
    escalation of a non-complete entry)."""
    known = {t.id for t in templates}
    leaves = set(selectable_leaves())
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, TemplateDiscoveryMetadataV1):
            raise TaxonomyValidationError("discovery entries must be TemplateDiscoveryMetadataV1")
        tid = entry.template_id
        if tid not in known:
            raise TaxonomyValidationError(
                f"discovery entry names unknown template {tid!r} (orphan)")
        if tid in seen:
            raise TaxonomyValidationError(f"duplicate discovery entry for template {tid!r}")
        seen.add(tid)
        if entry.feature_category is not None:
            cid = entry.feature_category.controlled_id
            if cid not in FEATURE_CATEGORY_REGISTRY:
                raise TaxonomyValidationError(
                    f"template {tid!r} feature_category {cid!r} is not a registered "
                    f"feature-category ID")
            # No template authors a category: a template_authored category can only come from
            # the family→category derivation, so it must cite that mapping. A bare
            # recipe-revision citation would present a taxonomy-derived value as one the recipe
            # attested — indistinguishable from an SME-authored objective.
            if (entry.feature_category.basis == "template_authored"
                    and not is_derived_from_family_mapping(entry.feature_category)):
                raise TaxonomyValidationError(
                    f"template {tid!r} feature_category {cid!r} is template_authored but cites "
                    f"no {FAMILY_MAPPING_CITATION_PREFIX} evidence — a derived category must "
                    f"cite the mapping that produced it, never only the recipe revision")
        if entry.business_domains:
            # D9: the shared controlled business-domain registry does not exist in code; no
            # ID can validate, so none can be assigned. A local look-alike registry is
            # forbidden — the absence stays honest until the semantic plan lands.
            raise TaxonomyValidationError(
                f"template {tid!r} assigns business-domain IDs "
                f"{[a.controlled_id for a in entry.business_domains]}, but no shared "
                f"controlled business-domain registry exists yet (D9)")
        case_ids: set[str] = set()
        for assignment in entry.canonical_use_cases:
            cid = assignment.controlled_id
            if cid not in leaves:
                raise TaxonomyValidationError(
                    f"template {tid!r} canonical use case {cid!r} is not a selectable leaf "
                    f"of the governed use-case taxonomy — legacy tags and parent nodes are "
                    f"never facet IDs")
            if cid in case_ids:
                raise TaxonomyValidationError(
                    f"template {tid!r} carries duplicate canonical use case {cid!r}")
            case_ids.add(cid)
        if len(entry.keywords) > MAX_KEYWORDS:
            raise TaxonomyValidationError(
                f"template {tid!r} exceeds the {MAX_KEYWORDS}-keyword bound")
        keyword_values: set[str] = set()
        for keyword in entry.keywords:
            safe_registry_text(keyword.value, field=f"template {tid} keyword",
                               max_len=MAX_KEYWORD_LEN)
            folded = keyword.value.casefold()
            if folded in keyword_values:
                raise TaxonomyValidationError(
                    f"template {tid!r} carries duplicate keyword {keyword.value!r}")
            keyword_values.add(folded)
        if entry.business_value is not None:
            safe_registry_text(entry.business_value.value,
                               field=f"template {tid} business_value",
                               max_len=MAX_BUSINESS_VALUE_LEN)
        computed = compute_discovery_disposition(
            feature_category=entry.feature_category,
            business_domains=entry.business_domains,
            canonical_use_cases=entry.canonical_use_cases,
            keywords=entry.keywords, business_value=entry.business_value)
        if entry.disposition != computed and not (
                entry.disposition == "needs_sme" and computed != "complete"):
            raise TaxonomyValidationError(
                f"template {tid!r} disposition {entry.disposition!r} does not summarize its "
                f"contents (computed {computed!r})")
    missing = known - seen
    if missing:
        raise TaxonomyValidationError(
            f"discovery registry is missing entries for templates: {sorted(missing)} — an "
            f"unmapped template must be an explicit unclassified entry, not an absence")


# ──────────────────────────────────────────────────────────────────────────────────────────────
# The reviewed legacy use-case audit manifest (migration evidence, never facet IDs)
# ──────────────────────────────────────────────────────────────────────────────────────────────
#: The explicit "nobody has resolved this yet" marker for the proposed-mapping column.
UNRESOLVED_MAPPING = "UNRESOLVED"

_MANIFEST_ROW_KEYS = frozenset({
    "legacy_tag", "template_ids", "crosswalk_dimension", "crosswalk_target",
    "crosswalk_status", "proposed_canonical_use_case",
})


def expected_legacy_audit_rows() -> list[dict]:
    """Derive the audit-manifest rows from the authored templates + the reviewed crosswalk
    (0F-2: Task 1 CONSUMES ``LEGACY_TAG_CROSSWALK``; it never re-does the archaeology). Every
    row's proposed mapping is explicitly ``UNRESOLVED`` — it becomes a canonical ID only after
    a later human/LLM pass passes :func:`validate_legacy_audit_manifest`."""
    tags_to_templates: dict[str, list[str]] = {}
    for template in ALL_TEMPLATES:
        for tag in template.use_cases:
            tags_to_templates.setdefault(tag, []).append(template.id)
    rows = []
    for tag in sorted(tags_to_templates):
        entry = LEGACY_TAG_CROSSWALK[tag]
        rows.append({
            "legacy_tag": tag,
            "template_ids": sorted(tags_to_templates[tag]),
            "crosswalk_dimension": entry["dimension"],
            "crosswalk_target": entry["target"],
            "crosswalk_status": entry["status"],
            "proposed_canonical_use_case": UNRESOLVED_MAPPING,
        })
    return rows


def validate_legacy_audit_manifest(rows: Sequence[Mapping]) -> None:
    """The manifest gate: exactly the authored legacy tags, crosswalk columns byte-equal to the
    reviewed ``LEGACY_TAG_CROSSWALK``, template lists true to the authored templates, and a
    proposed mapping that is either the explicit ``UNRESOLVED`` marker or an already-selectable
    canonical leaf — a legacy tag string (or any other uncontrolled text) in that column dies."""
    authored_tags = {tag for t in ALL_TEMPLATES for tag in t.use_cases}
    leaves = set(selectable_leaves())
    expected_templates: dict[str, list[str]] = {}
    for template in ALL_TEMPLATES:
        for tag in template.use_cases:
            expected_templates.setdefault(tag, []).append(template.id)
    seen: set[str] = set()
    for row in rows:
        if set(row) != _MANIFEST_ROW_KEYS:
            raise TaxonomyValidationError(
                f"manifest row keys {sorted(row)} differ from the frozen shape")
        tag = row["legacy_tag"]
        if tag not in authored_tags:
            raise TaxonomyValidationError(
                f"manifest names unknown legacy tag {tag!r} — not authored by any template")
        if tag in seen:
            raise TaxonomyValidationError(f"duplicate manifest row for legacy tag {tag!r}")
        seen.add(tag)
        crosswalk = LEGACY_TAG_CROSSWALK[tag]
        if (row["crosswalk_dimension"], row["crosswalk_target"],
                row["crosswalk_status"]) != (
                crosswalk["dimension"], crosswalk["target"], crosswalk["status"]):
            raise TaxonomyValidationError(
                f"manifest row {tag!r} drifted from the reviewed crosswalk entry")
        if row["template_ids"] != sorted(expected_templates[tag]):
            raise TaxonomyValidationError(
                f"manifest row {tag!r} template list drifted from the authored templates")
        proposed = row["proposed_canonical_use_case"]
        if proposed != UNRESOLVED_MAPPING and proposed not in leaves:
            raise TaxonomyValidationError(
                f"manifest row {tag!r} proposes {proposed!r}, which is neither the explicit "
                f"{UNRESOLVED_MAPPING} marker nor a selectable canonical leaf — an "
                f"uncontrolled string never becomes a use-case mapping")
    missing = authored_tags - seen
    if missing:
        raise TaxonomyValidationError(
            f"manifest is missing legacy tags: {sorted(missing)}")


def legacy_audit_manifest_content_hash(rows: Sequence[Mapping]) -> str:
    """Order-independent fingerprint of the manifest (rows sorted by legacy tag)."""
    payload = {"rows": sorted((dict(row) for row in rows),
                              key=lambda row: row["legacy_tag"])}
    return contract_hash_v1("legacy-use-case-audit-manifest", "1", payload)


# ──────────────────────────────────────────────────────────────────────────────────────────────
# LLM proposal batch — bounded, audited, optional; SUPPORT PATH ONLY
# ──────────────────────────────────────────────────────────────────────────────────────────────
# One audited batch MAY propose category/use-case/value/keyword mappings, once per template
# revision (``templates_needing_proposal``). It is NEVER run at import, from tests, or on an
# ordinary page read — dispatching it is a separately approved/audited action, and there is no
# per-grounded-suggestion LLM call. Outputs validate EXCLUSIVELY against existing controlled
# IDs plus the explicit abstention token; ``basis=llm_proposed`` with
# ``operational_influence='hint'`` — a proposal, never a fabricated governed authority label.

DISCOVERY_PROPOSAL_TASK = "overlay.discovery.taxonomy_proposal"
DISCOVERY_PROPOSAL_PROMPT_ID = "overlay_discovery_proposal_v1"
DISCOVERY_PROPOSAL_SCHEMA_ID = "overlay_discovery_proposal_batch"
DISCOVERY_PROPOSAL_ABSTAIN = "abstain"

#: The NAMED bounded template fields allowed to egress (the enrich_llm ``_ITEM_META_ALLOWED``
#: extension carries exactly these keys). Repo-authored recipe metadata only — never catalog
#: content, sample values or uploader text. Values are NFC-normalized and length-bounded.
DISCOVERY_PROPOSAL_EGRESS_FIELDS: dict[str, int] = {
    "recipe_family": 64,
    "recipe_intent": 400,
    "recipe_aggregation": 64,
    "funnel_stage": 64,
    "legacy_use_case_tags": 64,   # bound per tag; the list itself is template-authored (≤5)
}

_PROPOSAL_FAILURE_STATUSES = frozenset({"malformed", "missing", "egress_blocked"})

_DISCOVERY_PROPOSAL_INSTRUCTION = (
    "For each recipe item, propose discovery metadata from the CLOSED vocabularies provided in "
    "the shared metadata: feature_category from feature_category_ids, use_case_ids from "
    "canonical_use_case_ids, a one-sentence business_value, and up to 8 short keywords. When "
    "unsure, abstain explicitly: feature_category='abstain', use_case_ids=[], "
    "business_value='abstain', keywords=[]. Never invent an ID outside the vocabularies.")


@dataclass(frozen=True, slots=True)
class DiscoveryProposalV1:
    """One VALIDATED proposal: every ID already resolved against its controlled registry;
    ``None``/empty means the model abstained on that axis."""

    feature_category: str | None
    canonical_use_cases: tuple[str, ...]
    business_value: str | None
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryProposalResultV1:
    """The typed per-template outcome. A blocked, malformed or abstaining item records ITS
    result and the batch continues — one bad item never becomes an unexplained zero-output
    run, and free text is never coerced into a controlled ID."""

    template_id: str
    recipe_revision_id: str
    status: str            # proposed | abstained | malformed | missing | egress_blocked
    proposal: DiscoveryProposalV1 | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DiscoveryProposalProvenanceV1:
    """Model/prompt/schema/producer provenance for one proposal batch. The immutable
    ``llm_call`` audit row is recorded by the governed batch seam; this record travels with
    the typed results so every derived ``llm_proposed`` value stays attributable."""

    task: str
    prompt_id: str
    prompt_version: int
    schema_id: str
    schema_version: int
    provider: str
    model: str
    producer: str


@dataclass(frozen=True, slots=True)
class DiscoveryProposalBatchResult:
    batch_status: str      # completed | failure_budget_exceeded
    results: tuple[DiscoveryProposalResultV1, ...]
    provenance: DiscoveryProposalProvenanceV1
    provider_calls: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class DiscoveryProposalAssignments:
    """A proposal converted to registry-shaped assignments (``basis=llm_proposed``,
    ``operational_influence='hint'``, LLM evidence). Consumers merge these into discovery
    entries; the validator relabels nothing."""

    feature_category: DiscoveryControlledAssignmentV1 | None
    canonical_use_cases: tuple[DiscoveryControlledAssignmentV1, ...]
    keywords: tuple[DiscoveryTextAssignmentV1, ...]
    business_value: DiscoveryTextAssignmentV1 | None


def discovery_proposal_items(templates: Sequence[Template]) -> list:
    """Closed input: one metadata-only item per template carrying ONLY the named bounded
    fields. Authored content outside its bound is a repo bug and dies loudly here — nothing
    is silently truncated on the way to a provider."""
    from featuregen.overlay.upload.enrich_batch import BatchItem  # lazy: heavy import chain

    def _bounded(value: str, *, field: str, bound: int) -> str:
        normalized = unicodedata.normalize("NFC", value)
        if len(normalized) > bound:
            raise TaxonomyValidationError(
                f"authored template field {field} exceeds its {bound}-char egress bound")
        return normalized

    items = []
    for template in templates:
        metadata: dict = {
            "recipe_family": _bounded(template.family, field="recipe_family", bound=64),
            "recipe_intent": _bounded(template.intent, field="recipe_intent", bound=400),
            "recipe_aggregation": _bounded(template.aggregation,
                                           field="recipe_aggregation", bound=64),
        }
        if template.stage:
            metadata["funnel_stage"] = _bounded(template.stage, field="funnel_stage",
                                                bound=64)
        if template.use_cases:
            metadata["legacy_use_case_tags"] = [
                _bounded(tag, field="legacy_use_case_tags", bound=64)
                for tag in template.use_cases]
        items.append(BatchItem(ref=template.id, metadata=metadata))
    return items


def _extract_proposal(entry: Mapping) -> str:
    proposal = entry.get("proposal")
    return json.dumps(proposal, sort_keys=True) if isinstance(proposal, dict) else ""


def _accept_proposal(raw: str) -> tuple[str | None, str]:
    """Validate one proposal EXCLUSIVELY against existing controlled IDs plus abstention.
    Rejection returns a typed reason code; free text is never coerced into an ID."""
    try:
        proposal = json.loads(raw)
    except (TypeError, ValueError):
        return None, "proposal_unparseable"
    if not isinstance(proposal, dict):
        return None, "proposal_unparseable"
    category = proposal.get("feature_category")
    if not isinstance(category, str) or (
            category != DISCOVERY_PROPOSAL_ABSTAIN
            and category not in FEATURE_CATEGORY_REGISTRY):
        return None, "category_uncontrolled"
    use_case_ids = proposal.get("use_case_ids")
    if not isinstance(use_case_ids, list) or not all(
            isinstance(u, str) for u in use_case_ids):
        return None, "use_case_uncontrolled"
    leaves = set(selectable_leaves())
    if any(u not in leaves for u in use_case_ids):
        return None, "use_case_uncontrolled"
    if len(set(use_case_ids)) != len(use_case_ids):
        return None, "use_case_duplicate"
    business_value = proposal.get("business_value")
    if not isinstance(business_value, str):
        return None, "business_value_unsafe"
    if business_value != DISCOVERY_PROPOSAL_ABSTAIN:
        try:
            business_value = safe_registry_text(
                business_value, field="proposed business_value",
                max_len=MAX_BUSINESS_VALUE_LEN)
        except TaxonomyValidationError:
            return None, "business_value_unsafe"
    keywords = proposal.get("keywords")
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        return None, "keyword_unsafe"
    if len(keywords) > MAX_KEYWORDS:
        return None, "keyword_budget_exceeded"
    safe_keywords = []
    for keyword in keywords:
        try:
            safe_keywords.append(safe_registry_text(keyword, field="proposed keyword",
                                                    max_len=MAX_KEYWORD_LEN))
        except TaxonomyValidationError:
            return None, "keyword_unsafe"
    if len({k.casefold() for k in safe_keywords}) != len(safe_keywords):
        return None, "keyword_duplicate"
    accepted = {"feature_category": category, "use_case_ids": use_case_ids,
                "business_value": business_value, "keywords": safe_keywords}
    return json.dumps(accepted, sort_keys=True), "valid"


def _result_from_value(template_id: str, revision: str, value: str
                       ) -> DiscoveryProposalResultV1:
    accepted = json.loads(value)
    abstained = (accepted["feature_category"] == DISCOVERY_PROPOSAL_ABSTAIN
                 and not accepted["use_case_ids"]
                 and accepted["business_value"] == DISCOVERY_PROPOSAL_ABSTAIN
                 and not accepted["keywords"])
    if abstained:
        return DiscoveryProposalResultV1(
            template_id=template_id, recipe_revision_id=revision, status="abstained",
            proposal=None, reason_codes=(DISCOVERY_PROPOSAL_ABSTAIN,))
    proposal = DiscoveryProposalV1(
        feature_category=(None if accepted["feature_category"] == DISCOVERY_PROPOSAL_ABSTAIN
                          else accepted["feature_category"]),
        canonical_use_cases=tuple(accepted["use_case_ids"]),
        business_value=(None if accepted["business_value"] == DISCOVERY_PROPOSAL_ABSTAIN
                        else accepted["business_value"]),
        keywords=tuple(accepted["keywords"]))
    return DiscoveryProposalResultV1(
        template_id=template_id, recipe_revision_id=revision, status="proposed",
        proposal=proposal, reason_codes=("valid",))


def templates_needing_proposal(templates: Sequence[Template],
                               proposed_revision_ids: Iterable[str]) -> tuple[Template, ...]:
    """Once per template REVISION: a template whose current ``recipe_revision_id`` already has
    a stored proposal is filtered out; a recipe-content edit re-revisions it and re-qualifies
    it, while discovery-side edits (keywords/mappings) never do."""
    done = set(proposed_revision_ids)
    return tuple(t for t in templates if recipe_revision_id(t) not in done)


def run_discovery_proposal_batch(conn, client, *,
                                 templates: Sequence[Template] | None = None,
                                 items: list | None = None,
                                 actor=None,
                                 max_failure_fraction: float = 0.5,
                                 chunk_max_items: int = 24,
                                 chunk_max_input_tokens: int = 8000,
                                 ) -> DiscoveryProposalBatchResult:
    """Run ONE bounded, audited taxonomy-proposal batch through the governed batch seam
    (``audited_batch_call``: per-item egress gate, batch egress backstop, schema-validated
    array call, immutable ``llm_call`` audit row). Never called at import, from tests with a
    real client, or on a page read — the caller owns the separately approved dispatch.

    Every requested template yields a typed :class:`DiscoveryProposalResultV1`; failures
    (malformed / missing / egress-blocked) are counted against ``max_failure_fraction`` and
    flip ``batch_status`` to ``failure_budget_exceeded`` without discarding the typed results.
    """
    if not 0.0 <= max_failure_fraction <= 1.0:
        raise ValueError("max_failure_fraction must be within [0, 1]")
    from featuregen.overlay.upload.enrich_batch import (  # lazy: heavy import chain
        BLANK,
        DUPLICATE,
        EGRESS,
        INVALID,
        MISSING,
        VALID,
        chunk_items,
    )
    from featuregen.overlay.upload.enrich_llm import (
        audited_batch_call,
        current_enrichment_generation_settings,
    )
    chosen = tuple(templates) if templates is not None else ALL_TEMPLATES
    by_id = {t.id: t for t in chosen}
    if items is None:
        items = discovery_proposal_items(chosen)
    shared_metadata = {
        "feature_category_ids": sorted(FEATURE_CATEGORY_REGISTRY),
        "canonical_use_case_ids": sorted(selectable_leaves()),
        "abstention_token": DISCOVERY_PROPOSAL_ABSTAIN,
    }
    outcomes = []
    provider_calls = input_tokens = output_tokens = 0
    for chunk in chunk_items(items, max_items=chunk_max_items,
                             max_input_tokens=chunk_max_input_tokens):
        res = audited_batch_call(
            conn, client, task=DISCOVERY_PROPOSAL_TASK,
            prompt_id=DISCOVERY_PROPOSAL_PROMPT_ID,
            schema_id=DISCOVERY_PROPOSAL_SCHEMA_ID,
            shared_metadata=shared_metadata, items=chunk, out_key="proposal",
            instruction=_DISCOVERY_PROPOSAL_INSTRUCTION,
            accept=_accept_proposal, extract=_extract_proposal, actor=actor)
        outcomes.extend(res.outcomes)
        provider_calls += res.provider_calls
        input_tokens += res.input_tokens
        output_tokens += res.output_tokens
    results: list[DiscoveryProposalResultV1] = []
    for outcome in outcomes:
        template = by_id.get(outcome.ref)
        if template is None:
            continue   # an EXTRA ref the provider invented — classified by the seam, no template
        revision = recipe_revision_id(template)
        if outcome.status == VALID and outcome.value is not None:
            results.append(_result_from_value(template.id, revision, outcome.value))
        elif outcome.status == EGRESS:
            results.append(DiscoveryProposalResultV1(
                template_id=template.id, recipe_revision_id=revision,
                status="egress_blocked", proposal=None,
                reason_codes=tuple(outcome.reason_codes)))
        elif outcome.status == MISSING:
            results.append(DiscoveryProposalResultV1(
                template_id=template.id, recipe_revision_id=revision, status="missing",
                proposal=None, reason_codes=tuple(outcome.reason_codes)))
        elif outcome.status in (BLANK, INVALID, DUPLICATE):
            results.append(DiscoveryProposalResultV1(
                template_id=template.id, recipe_revision_id=revision, status="malformed",
                proposal=None, reason_codes=tuple(outcome.reason_codes)))
    settings = current_enrichment_generation_settings()
    provenance = DiscoveryProposalProvenanceV1(
        task=DISCOVERY_PROPOSAL_TASK, prompt_id=DISCOVERY_PROPOSAL_PROMPT_ID,
        prompt_version=1, schema_id=DISCOVERY_PROPOSAL_SCHEMA_ID, schema_version=1,
        provider=str(settings.get("provider", "")), model=str(settings.get("model", "")),
        producer=TEMPLATE_DISCOVERY_OWNER)
    failures = sum(1 for r in results if r.status in _PROPOSAL_FAILURE_STATUSES)
    total = len(results)
    batch_status = ("failure_budget_exceeded"
                    if total and failures / total > max_failure_fraction else "completed")
    return DiscoveryProposalBatchResult(
        batch_status=batch_status, results=tuple(results), provenance=provenance,
        provider_calls=provider_calls, input_tokens=input_tokens,
        output_tokens=output_tokens)


def _llm_evidence(provenance: DiscoveryProposalProvenanceV1) -> tuple[EvidenceAuthorityV1, ...]:
    return (EvidenceAuthorityV1(
        producer=EvidenceProducer.LLM,
        strength=AssertionStrength.PROPOSED,
        lifecycle=EvidenceLifecycle.ACTIVE,
        producer_ref=(f"{provenance.provider}/{provenance.model}:"
                      f"{provenance.prompt_id}@{provenance.schema_id}"),
        evidence_id=None),)


def proposal_assignments(result: DiscoveryProposalResultV1,
                         provenance: DiscoveryProposalProvenanceV1
                         ) -> DiscoveryProposalAssignments:
    """Convert one PROPOSED result into registry-shaped assignments: ``basis=llm_proposed``,
    ``operational_influence='hint'``, LLM evidence. Anything but a proposed result converts
    to nothing — an abstention or failure never fabricates a value."""
    if result.status != "proposed" or result.proposal is None:
        return DiscoveryProposalAssignments(
            feature_category=None, canonical_use_cases=(), keywords=(), business_value=None)
    evidence = _llm_evidence(provenance)
    proposal = result.proposal
    feature_category = (DiscoveryControlledAssignmentV1(
        controlled_id=proposal.feature_category, basis="llm_proposed", evidence=evidence,
        operational_influence=_LLM_PROPOSED_INFLUENCE)
        if proposal.feature_category else None)
    canonical_use_cases = tuple(
        DiscoveryControlledAssignmentV1(controlled_id=uid, basis="llm_proposed",
                                        evidence=evidence,
                                        operational_influence=_LLM_PROPOSED_INFLUENCE)
        for uid in proposal.canonical_use_cases)
    keywords = tuple(
        DiscoveryTextAssignmentV1(value=keyword, basis="llm_proposed", evidence=evidence,
                                  operational_influence=_LLM_PROPOSED_INFLUENCE)
        for keyword in proposal.keywords)
    business_value = (DiscoveryTextAssignmentV1(
        value=proposal.business_value, basis="llm_proposed", evidence=evidence,
        operational_influence=_LLM_PROPOSED_INFLUENCE)
        if proposal.business_value else None)
    return DiscoveryProposalAssignments(
        feature_category=feature_category, canonical_use_cases=canonical_use_cases,
        keywords=keywords, business_value=business_value)


# ── contract registrations (one owner; contract_hash_v1 refuses unregistered pairs) ────────────
register_contract_version("template-discovery-metadata", "1", owner=TEMPLATE_DISCOVERY_OWNER)
register_contract_version("template-discovery-registry", "1", owner=TEMPLATE_DISCOVERY_OWNER)
register_contract_version("legacy-use-case-audit-manifest", "1",
                          owner=TEMPLATE_DISCOVERY_OWNER)
register_contract_version("family-feature-category-mapping", "1",
                          owner=TEMPLATE_DISCOVERY_OWNER)

# ── build + import-time validation: a malformed registry dies at import ────────────────────────
_validate_family_mapping()
_FAMILY_MAPPING_CONTENT_HASH = family_feature_category_mapping_content_hash()
_ENTRIES: tuple[TemplateDiscoveryMetadataV1, ...] = tuple(
    _authored_entry(t, family_mapping_hash=_FAMILY_MAPPING_CONTENT_HASH)
    for t in ALL_TEMPLATES)
validate_discovery_entries(_ENTRIES)

#: The discovery-metadata registry: exact template-ID coverage, per-value provenance,
#: explicit dispositions. Read-only.
DISCOVERY_METADATA: dict[str, TemplateDiscoveryMetadataV1] = {
    e.template_id: e for e in _ENTRIES}
