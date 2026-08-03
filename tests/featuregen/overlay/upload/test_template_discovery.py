"""Task 1 — per-template discovery metadata (template_discovery.py).

Full-template coverage, referential validation (no uncontrolled string can become a facet),
the D5 identity split (recipe_revision_id vs discovery_metadata_revision_id), the brief's
mutation + must-survive proofs, and the reviewed legacy use-case audit manifest.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from featuregen.contracts.contract_versions import contract_owner
from featuregen.contracts.evidence_axes import EvidenceAuthorityV1
from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.upload.recipe_grounding_context import canonical_template, content_hash
from featuregen.overlay.upload.suggestion_taxonomy import (
    FEATURE_CATEGORY_REGISTRY,
    TaxonomyValidationError,
)
from featuregen.overlay.upload.taxonomy.legacy_crosswalk import LEGACY_TAG_CROSSWALK
from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves
from featuregen.overlay.upload.template_discovery import (
    DISCOVERY_BASIS_VALUES,
    DISCOVERY_DISPOSITIONS,
    DISCOVERY_METADATA,
    TEMPLATE_DISCOVERY_OWNER,
    UNRESOLVED_MAPPING,
    DiscoveryControlledAssignmentV1,
    DiscoveryTextAssignmentV1,
    TemplateDiscoveryMetadataV1,
    compute_discovery_disposition,
    discovery_metadata_revision_id,
    discovery_registry_content_hash,
    expected_legacy_audit_rows,
    legacy_audit_manifest_content_hash,
    recipe_revision_id,
    validate_discovery_entries,
    validate_legacy_audit_manifest,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES

_BY_ID = {t.id: t for t in ALL_TEMPLATES}
_FIXTURE = (Path(__file__).parent / "fixtures" / "legacy_use_case_audit_manifest_v1.json")


def _evidence(producer=EvidenceProducer.TAXONOMY, ref="recipe-revision:test") -> EvidenceAuthorityV1:
    return EvidenceAuthorityV1(producer=producer, strength=AssertionStrength.ATTESTED,
                               lifecycle=EvidenceLifecycle.ACTIVE, producer_ref=ref,
                               evidence_id=None)


def _entries() -> list[TemplateDiscoveryMetadataV1]:
    return [DISCOVERY_METADATA[t.id] for t in ALL_TEMPLATES]


# ── full-template coverage and explicit dispositions ───────────────────────────────────────────

def test_every_template_has_exactly_one_entry_with_explicit_disposition():
    assert set(DISCOVERY_METADATA) == set(_BY_ID)
    assert len(DISCOVERY_METADATA) == 157
    for entry in DISCOVERY_METADATA.values():
        assert entry.disposition in DISCOVERY_DISPOSITIONS


def test_only_the_14_authored_objective_templates_carry_canonical_use_cases():
    with_cases = [e for e in DISCOVERY_METADATA.values() if e.canonical_use_cases]
    assert len(with_cases) == 14
    leaves = set(selectable_leaves())
    for entry in with_cases:
        template = _BY_ID[entry.template_id]
        assert template.primary_objective  # only authored objectives, never converted legacy tags
        authored = {template.primary_objective, *template.supporting_objectives}
        for assignment in entry.canonical_use_cases:
            assert assignment.controlled_id in leaves
            assert assignment.controlled_id in authored
            assert assignment.basis == "template_authored"
            assert assignment.operational_influence is None
            assert assignment.evidence  # cites the recipe revision
    without = [e for e in DISCOVERY_METADATA.values() if not e.canonical_use_cases]
    assert len(without) == 143  # 0F-2: unmapped templates stay explicit, never invented


def test_business_domains_are_empty_until_the_shared_registry_lands():
    # D9: no controlled business-domain resolver exists; absence is honest, never guessed.
    assert all(e.business_domains == () for e in DISCOVERY_METADATA.values())


def test_no_authored_business_value_or_keywords_are_fabricated_at_baseline():
    assert all(e.business_value is None and e.keywords == ()
               for e in DISCOVERY_METADATA.values())


def test_feature_category_assignments_are_deterministic_and_controlled():
    categorized = [e for e in DISCOVERY_METADATA.values() if e.feature_category]
    assert categorized  # the mapping covers most families…
    for entry in categorized:
        assert entry.feature_category.controlled_id in FEATURE_CATEGORY_REGISTRY
        assert entry.feature_category.basis == "template_authored"
        assert entry.feature_category.operational_influence is None
    # …but a genuinely ambiguous family stays unclassified (rule 12: no invented metadata):
    # balance_stock mixes trend/days-below/volatility shapes in one family.
    assert DISCOVERY_METADATA["balance_trend"].feature_category is None
    # same-family templates always agree (the mapping is family-deterministic)
    by_family: dict[str, set[str | None]] = {}
    for entry in DISCOVERY_METADATA.values():
        cat = entry.feature_category.controlled_id if entry.feature_category else None
        by_family.setdefault(_BY_ID[entry.template_id].family, set()).add(cat)
    assert all(len(cats) == 1 for cats in by_family.values())


def test_dispositions_match_the_computed_summary():
    for entry in DISCOVERY_METADATA.values():
        assert entry.disposition == compute_discovery_disposition(
            feature_category=entry.feature_category,
            business_domains=entry.business_domains,
            canonical_use_cases=entry.canonical_use_cases,
            keywords=entry.keywords,
            business_value=entry.business_value,
        )
        # Release A cannot be "complete": domains are unresolvable (D9) and honesty wins.
        assert entry.disposition in ("partial", "unclassified")


# ── validator: loud death on every forbidden shape ─────────────────────────────────────────────

def test_validator_dies_on_orphan_entry():
    orphan = dataclasses.replace(_entries()[0], template_id="no_such_template")
    with pytest.raises(TaxonomyValidationError, match="no_such_template"):
        validate_discovery_entries([*_entries(), orphan])


def test_validator_dies_on_duplicate_template_entry():
    entries = _entries()
    with pytest.raises(TaxonomyValidationError, match="duplicate"):
        validate_discovery_entries([*entries, entries[0]])


def test_validator_dies_on_missing_template_coverage():
    with pytest.raises(TaxonomyValidationError, match="missing"):
        validate_discovery_entries(_entries()[:-1])


def test_validator_dies_on_unknown_feature_category_id():
    entries = _entries()
    bad = dataclasses.replace(
        entries[0],
        feature_category=DiscoveryControlledAssignmentV1(
            controlled_id="not_a_category", basis="template_authored",
            evidence=(_evidence(),), operational_influence=None))
    with pytest.raises(TaxonomyValidationError, match="not_a_category"):
        validate_discovery_entries([bad, *entries[1:]])


def test_mutation_canonical_id_replaced_by_plausible_legacy_string_dies():
    """The brief's mutation test: swap one canonical use-case ID for the plausible legacy tag
    it descends from and the registry validator must die."""
    entries = _entries()
    idx = next(i for i, e in enumerate(entries) if e.template_id == "nmd_stickiness")
    entry = entries[idx]
    swapped = tuple(
        dataclasses.replace(a, controlled_id="deposit_stability")  # legacy tag, not a leaf ID
        if a.controlled_id == "treasury_alm.deposit_stability" else a
        for a in entry.canonical_use_cases)
    entries[idx] = dataclasses.replace(entry, canonical_use_cases=swapped)
    with pytest.raises(TaxonomyValidationError, match="deposit_stability"):
        validate_discovery_entries(entries)


def test_validator_dies_on_non_leaf_and_non_selectable_use_cases():
    entries = _entries()
    idx = next(i for i, e in enumerate(entries) if e.canonical_use_cases)
    for bad_id in ("credit", "financial_crime"):  # registry nodes, but never selectable leaves
        swapped = dataclasses.replace(
            entries[idx],
            canonical_use_cases=(dataclasses.replace(
                entries[idx].canonical_use_cases[0], controlled_id=bad_id),))
        with pytest.raises(TaxonomyValidationError, match=bad_id):
            validate_discovery_entries([*entries[:idx], swapped, *entries[idx + 1:]])


def test_validator_dies_on_duplicate_canonical_use_case():
    entries = _entries()
    idx = next(i for i, e in enumerate(entries) if e.canonical_use_cases)
    doubled = dataclasses.replace(
        entries[idx],
        canonical_use_cases=entries[idx].canonical_use_cases * 2)
    with pytest.raises(TaxonomyValidationError, match="duplicate"):
        validate_discovery_entries([*entries[:idx], doubled, *entries[idx + 1:]])


def test_validator_dies_on_uncontrolled_business_domain():
    # D9: no controlled business-domain registry exists, so NO domain ID can validate.
    entries = _entries()
    bad = dataclasses.replace(
        entries[0],
        business_domains=(DiscoveryControlledAssignmentV1(
            controlled_id="retail_banking", basis="template_authored",
            evidence=(_evidence(),), operational_influence=None),),
        disposition="partial")
    with pytest.raises(TaxonomyValidationError, match="business.domain"):
        validate_discovery_entries([bad, *entries[1:]])


def test_validator_dies_on_wrong_disposition_summary():
    entries = _entries()
    lying = dataclasses.replace(entries[0], disposition="complete")
    with pytest.raises(TaxonomyValidationError, match="disposition"):
        validate_discovery_entries([lying, *entries[1:]])


def test_blank_business_value_cannot_be_marked_mapped():
    with pytest.raises(TaxonomyValidationError):
        DiscoveryTextAssignmentV1(value="   ", basis="human", evidence=(_evidence(),),
                                  operational_influence=None)


def test_assignment_basis_vocabulary_is_closed():
    assert DISCOVERY_BASIS_VALUES == frozenset({"template_authored", "human", "llm_proposed"})
    with pytest.raises(TaxonomyValidationError):
        DiscoveryControlledAssignmentV1(controlled_id="ratio", basis="catalog_resolved",
                                        evidence=(_evidence(),), operational_influence=None)


def test_operational_influence_rules():
    # llm_proposed is a hint, never governed, never silently authorityless.
    with pytest.raises(TaxonomyValidationError):
        DiscoveryControlledAssignmentV1(controlled_id="ratio", basis="llm_proposed",
                                        evidence=(_evidence(EvidenceProducer.LLM),),
                                        operational_influence=None)
    with pytest.raises(TaxonomyValidationError):
        DiscoveryControlledAssignmentV1(controlled_id="ratio", basis="llm_proposed",
                                        evidence=(_evidence(EvidenceProducer.LLM),),
                                        operational_influence="governed")
    # authored/human discovery values carry no operational influence in v1 (0F-5).
    with pytest.raises(TaxonomyValidationError):
        DiscoveryControlledAssignmentV1(controlled_id="ratio", basis="template_authored",
                                        evidence=(_evidence(),), operational_influence="hint")


def test_assignment_requires_evidence():
    with pytest.raises(TaxonomyValidationError, match="evidence"):
        DiscoveryControlledAssignmentV1(controlled_id="ratio", basis="template_authored",
                                        evidence=(), operational_influence=None)


# ── identity: recipe revision reuse (D5) + the separate discovery revision ─────────────────────

def test_recipe_revision_id_reuses_the_existing_template_content_hash():
    for template in ALL_TEMPLATES[:5]:
        assert recipe_revision_id(template) == content_hash(canonical_template(template))


def test_keyword_edit_changes_discovery_revision_but_never_recipe_identity():
    template = _BY_ID["nmd_stickiness"]
    entry = DISCOVERY_METADATA[template.id]
    with_keywords = dataclasses.replace(
        entry,
        keywords=(DiscoveryTextAssignmentV1(value="deposit stickiness", basis="human",
                                            evidence=(_evidence(EvidenceProducer.HUMAN),),
                                            operational_influence=None),),
        disposition=entry.disposition)
    assert (discovery_metadata_revision_id(with_keywords)
            != discovery_metadata_revision_id(entry))
    # the recipe's computational identity is untouched by any discovery edit
    assert recipe_revision_id(template) == content_hash(canonical_template(template))


def test_template_content_edit_moves_recipe_revision_not_discovery_revision():
    template = _BY_ID["nmd_stickiness"]
    entry = DISCOVERY_METADATA[template.id]
    edited = dataclasses.replace(template, notes=("edited",))
    assert recipe_revision_id(edited) != recipe_revision_id(template)  # D5, accepted
    # Re-citing the NEW recipe revision in the evidence is occurrence provenance (0F-4 rule 3),
    # so the discovery revision stands still — the two identities never drag each other.
    recited = dataclasses.replace(
        entry,
        canonical_use_cases=tuple(
            dataclasses.replace(a, evidence=(dataclasses.replace(
                a.evidence[0],
                producer_ref=f"recipe-revision:{recipe_revision_id(edited)}"),))
            for a in entry.canonical_use_cases))
    assert discovery_metadata_revision_id(recited) == discovery_metadata_revision_id(entry)


def test_discovery_revision_survives_reordering_and_evidence_replay():
    entry = DISCOVERY_METADATA["nmd_stickiness"]
    baseline = discovery_metadata_revision_id(entry)
    reordered = dataclasses.replace(
        entry, canonical_use_cases=tuple(reversed(entry.canonical_use_cases)))
    assert discovery_metadata_revision_id(reordered) == baseline
    # replaying identical evidence under a new occurrence id changes no revision (0F-4 rule 3)
    replayed = dataclasses.replace(
        entry,
        canonical_use_cases=tuple(
            dataclasses.replace(a, evidence=(*a.evidence,
                                             dataclasses.replace(a.evidence[0],
                                                                 evidence_id="evi_new")))
            for a in entry.canonical_use_cases))
    assert discovery_metadata_revision_id(replayed) == baseline


def test_discovery_revision_moves_on_controlled_content_change():
    entry = DISCOVERY_METADATA["nmd_stickiness"]
    changed = dataclasses.replace(
        entry, canonical_use_cases=entry.canonical_use_cases[:-1])
    assert discovery_metadata_revision_id(changed) != discovery_metadata_revision_id(entry)


def test_registry_content_hash_survives_entry_reordering():
    entries = _entries()
    assert (discovery_registry_content_hash(list(reversed(entries)))
            == discovery_registry_content_hash(entries))


def test_contracts_are_registered_to_the_owner_module():
    assert TEMPLATE_DISCOVERY_OWNER == "featuregen.overlay.upload.template_discovery"
    for name in ("template-discovery-metadata", "template-discovery-registry",
                 "legacy-use-case-audit-manifest"):
        assert contract_owner(name, "1") == TEMPLATE_DISCOVERY_OWNER


# ── the reviewed legacy use-case audit manifest ────────────────────────────────────────────────

def _fixture_rows() -> list[dict]:
    return json.loads(_FIXTURE.read_text())["rows"]


def test_manifest_fixture_is_valid_and_matches_the_derived_expectation():
    rows = _fixture_rows()
    validate_legacy_audit_manifest(rows)
    expected = expected_legacy_audit_rows()
    key = lambda r: r["legacy_tag"]  # noqa: E731
    assert sorted(rows, key=key) == sorted(expected, key=key)


def test_manifest_covers_all_107_legacy_tags_and_resolves_nothing_silently():
    rows = _fixture_rows()
    assert len(rows) == 107
    assert {r["legacy_tag"] for r in rows} == set(LEGACY_TAG_CROSSWALK)
    # migration evidence only: every proposed mapping is explicitly unresolved at baseline
    assert all(r["proposed_canonical_use_case"] == UNRESOLVED_MAPPING for r in rows)


def test_manifest_mutation_legacy_string_as_proposed_mapping_dies():
    rows = _fixture_rows()
    rows[0] = {**rows[0], "proposed_canonical_use_case": "deposit_attrition"}  # legacy tag
    with pytest.raises(TaxonomyValidationError, match="deposit_attrition"):
        validate_legacy_audit_manifest(rows)


def test_manifest_accepts_a_selectable_leaf_as_a_future_resolved_mapping():
    rows = _fixture_rows()
    rows[0] = {**rows[0],
               "proposed_canonical_use_case": "customer.relationship_attrition.churn"}
    validate_legacy_audit_manifest(rows)  # the later human/LLM pass has a legal landing spot


def test_manifest_hash_survives_row_reordering_and_moves_on_content():
    rows = _fixture_rows()
    baseline = legacy_audit_manifest_content_hash(rows)
    assert legacy_audit_manifest_content_hash(list(reversed(rows))) == baseline
    changed = [{**r} for r in rows]
    changed[0]["proposed_canonical_use_case"] = "customer.relationship_attrition.churn"
    assert legacy_audit_manifest_content_hash(changed) != baseline


def test_manifest_fixture_hash_is_pinned():
    """Golden: the reviewed manifest's content hash. Any resolution of a proposed mapping is a
    deliberate, reviewed change that re-pins this — never a silent drive-by edit."""
    assert legacy_audit_manifest_content_hash(_fixture_rows()) == (
        "a0deaff160f220dbf125062ab6dc465dc9bd97132840accbc9980c44d53ed77c")


def test_manifest_rejects_unknown_tag_and_crosswalk_drift():
    rows = _fixture_rows()
    with pytest.raises(TaxonomyValidationError, match="not_a_tag"):
        validate_legacy_audit_manifest([*rows, {**rows[0], "legacy_tag": "not_a_tag"}])
    drifted = [{**r} for r in rows]
    drifted[0]["crosswalk_status"] = "mapped" if drifted[0][
        "crosswalk_status"] != "mapped" else "deprecated"
    with pytest.raises(TaxonomyValidationError, match="crosswalk"):
        validate_legacy_audit_manifest(drifted)
