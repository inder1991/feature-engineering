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
_FAMILY_FEATURE_CATEGORY: dict[str, str] = {
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
    for family, category in _FAMILY_FEATURE_CATEGORY.items():
        if family not in authored:
            raise TaxonomyValidationError(
                f"family mapping names unauthored family {family!r}")
        if category not in FEATURE_CATEGORY_REGISTRY:
            raise TaxonomyValidationError(
                f"family {family!r} maps to unknown feature category {category!r}")


# ──────────────────────────────────────────────────────────────────────────────────────────────
# Entry construction (template_authored only; nothing invented)
# ──────────────────────────────────────────────────────────────────────────────────────────────
def _authored_evidence(template: Template) -> EvidenceAuthorityV1:
    """Template-authored values cite the recipe revision (plan: "Template-authored labels cite
    recipe_revision_id"). The citation is occurrence provenance — excluded from semantic
    hashes — so re-citing a re-revisioned recipe never churns the discovery revision."""
    return EvidenceAuthorityV1(
        producer=EvidenceProducer.TAXONOMY,
        strength=AssertionStrength.ATTESTED,
        lifecycle=EvidenceLifecycle.ACTIVE,
        producer_ref=f"recipe-revision:{recipe_revision_id(template)}",
        evidence_id=None,
    )


def _authored_entry(template: Template) -> TemplateDiscoveryMetadataV1:
    evidence = (_authored_evidence(template),)
    category_id = _FAMILY_FEATURE_CATEGORY.get(template.family)
    feature_category = (DiscoveryControlledAssignmentV1(
        controlled_id=category_id, basis="template_authored", evidence=evidence,
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
    duplicates; keyword/prose bounds; basis/influence/evidence rules; and the disposition is
    the honest computed summary (``needs_sme`` allowed only as an authored escalation of a
    non-complete entry)."""
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


# ── contract registrations (one owner; contract_hash_v1 refuses unregistered pairs) ────────────
register_contract_version("template-discovery-metadata", "1", owner=TEMPLATE_DISCOVERY_OWNER)
register_contract_version("template-discovery-registry", "1", owner=TEMPLATE_DISCOVERY_OWNER)
register_contract_version("legacy-use-case-audit-manifest", "1",
                          owner=TEMPLATE_DISCOVERY_OWNER)

# ── build + import-time validation: a malformed registry dies at import ────────────────────────
_validate_family_mapping()
_ENTRIES: tuple[TemplateDiscoveryMetadataV1, ...] = tuple(
    _authored_entry(t) for t in ALL_TEMPLATES)
validate_discovery_entries(_ENTRIES)

#: The discovery-metadata registry: exact template-ID coverage, per-value provenance,
#: explicit dispositions. Read-only.
DISCOVERY_METADATA: dict[str, TemplateDiscoveryMetadataV1] = {
    e.template_id: e for e in _ENTRIES}
