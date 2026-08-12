"""Joint Task 4 (c) / semantic Task 3 — replay identity over the EXACT rendered payload.

The two properties the plan demands, and the one pre-existing defect it fixes:

* mutating any RENDERED meaning-bearing registry field invalidates replay;
* mutating a registry field that is NOT rendered to that task does NOT;
* reordering equivalent dicts/sets/registry DECLARATION order does not (the old names-only
  `json.dumps` over `_ALL` in declaration order made merely moving a `Concept(...)` line invalidate
  every cached classification in the platform).

Sequenced AFTER the payload change on purpose (review C Task 3): before the widening, the
classifier rendered only `{name, group, hint}` while the fingerprint claimed more, so the two
properties were unsatisfiable together.

D12.5 RECONCILIATION, asserted here rather than argued in prose: the plan says "hash the exact
purpose-filtered context bytes"; the CONTROLLING doc says the roster/table context enter the prompt
but not the per-column cache key. Both hold because the identity hashes the context SHAPE (which
keys the adapter renders) and never the per-column context VALUES.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.overlay.upload import concepts as concepts_mod
from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import semantic_context as sc
from featuregen.overlay.upload.attest import concept_critic as critic


def _patch_registry(monkeypatch, mutate) -> None:
    """Apply `mutate` to a COPY of the concept registry and re-derive everything both fingerprints
    read from it — the registry mapping, the ordered tuple, and the classifier's rendered
    vocabulary snapshot."""
    registry = dict(concepts_mod.CONCEPT_REGISTRY)
    ordered = list(concepts_mod._ALL)
    mutate(registry, ordered)
    monkeypatch.setattr(concepts_mod, "CONCEPT_REGISTRY", registry)
    monkeypatch.setattr(concepts_mod, "_ALL", tuple(ordered))
    # The critic binds the registry by NAME at import; patch its binding too, or its fingerprint
    # would keep reading the pristine module-level mapping and the property would be vacuous.
    monkeypatch.setattr(critic, "CONCEPT_REGISTRY", registry)
    monkeypatch.setattr(enrich, "_CONCEPT_VOCABULARY",
                        list(concepts_mod.classification_vocabulary()))


def _set(name: str, **fields):
    def _mutate(registry, ordered):
        updated = replace(registry[name], **fields)
        registry[name] = updated
        for i, c in enumerate(ordered):
            if c.name == name:
                ordered[i] = updated
    return _mutate


# ── the classifier fingerprint ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field,value", [
    ("group", "monetary"),                       # rendered as `group`
    ("description", "A completely different meaning entirely. Second sentence."),   # -> `hint`
])
def test_mutating_a_rendered_registry_field_invalidates_classifier_replay(
        monkeypatch, field, value) -> None:
    before = enrich._vocab_fingerprint()
    _patch_registry(monkeypatch, _set("customer_id", **{field: value}))
    assert enrich._vocab_fingerprint() != before


@pytest.mark.parametrize("field,value", [
    ("sensitivity", "special_category"),
    ("additivity", "additive"),
    ("namespace", "some_other_namespace"),
    ("entity_link", "some_other_entity"),
])
def test_mutating_an_unrendered_registry_field_does_not_invalidate_classifier_replay(
        monkeypatch, field, value) -> None:
    """The classifier is shown `{name, group, hint}` and nothing else. A namespace repair is
    meaning-bearing for the CRITIC (which renders it) and must re-key THERE — but re-classifying
    every column in the platform for it would be a paid re-run for information the classifier never
    saw."""
    before = enrich._vocab_fingerprint()
    _patch_registry(monkeypatch, _set("customer_id", **{field: value}))
    assert enrich._vocab_fingerprint() == before


def test_registry_declaration_order_does_not_invalidate_classifier_replay(monkeypatch) -> None:
    before = enrich._vocab_fingerprint()

    def _reverse(registry, ordered):
        ordered.reverse()
        for key in list(registry):
            registry[key] = registry.pop(key)      # re-insert: a different dict iteration order

    _patch_registry(monkeypatch, _reverse)
    assert enrich._vocab_fingerprint() == before


def test_the_classifier_identity_folds_in_the_request_contract_and_context_shape() -> None:
    """The cache version binds the prompt/schema contract AND the adapter's rendered-key list, so a
    widened payload re-asks the question once — while a per-column context VALUE never enters it."""
    version = enrich._concept_cache_version()
    assert version == f"concept:v3:{enrich._vocab_fingerprint()}"
    assert set(enrich.CLASSIFIER_RENDERED_REGISTRY_FIELDS) == {"name", "group", "hint"}
    # Exactly the fields `classification_vocabulary` renders — a drift here is the defect the two
    # property tests above exist to catch.
    rendered = set(enrich._CONCEPT_VOCABULARY[0])
    assert rendered == set(enrich.CLASSIFIER_RENDERED_REGISTRY_FIELDS)


def test_widening_the_rendered_context_shape_re_keys_the_cache(monkeypatch) -> None:
    before = enrich._classifier_contract_fingerprint()
    monkeypatch.setattr(sc, "CONCEPT_ENRICHMENT_RENDERED_KEYS",
                        (*sc.CONCEPT_ENRICHMENT_RENDERED_KEYS, "a_newly_rendered_key"))
    assert enrich._classifier_contract_fingerprint() != before


def test_reordering_the_rendered_context_shape_does_not(monkeypatch) -> None:
    before = enrich._classifier_contract_fingerprint()
    monkeypatch.setattr(sc, "CONCEPT_ENRICHMENT_RENDERED_KEYS",
                        tuple(reversed(sc.CONCEPT_ENRICHMENT_RENDERED_KEYS)))
    assert enrich._classifier_contract_fingerprint() == before


# ── the critic fingerprint ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("field,value", [
    ("group", "monetary"),
    ("namespace", "some_other_namespace"),       # the load-bearing join axis
    ("entity_link", "some_other_entity"),        # rendered in `proposed_concept_meaning`
    ("is_a", "party_identifier"),                # rendered as the `concept_path` ancestry
    ("description", "Something else entirely. Second sentence."),   # the rendered HINT
])
def test_mutating_any_field_the_critic_renders_invalidates_its_replay(
        monkeypatch, field, value) -> None:
    before = critic._registry_fingerprint()
    _patch_registry(monkeypatch, _set("customer_id", **{field: value}))
    assert critic._registry_fingerprint() != before


@pytest.mark.parametrize("field,value", [
    ("sensitivity", "special_category"),
    ("additivity", "additive"),
    ("near_label", True),
])
def test_mutating_a_field_the_critic_never_renders_does_not(monkeypatch, field, value) -> None:
    before = critic._registry_fingerprint()
    _patch_registry(monkeypatch, _set("customer_id", **{field: value}))
    assert critic._registry_fingerprint() == before


def test_critic_fingerprint_is_registry_order_insensitive(monkeypatch) -> None:
    before = critic._registry_fingerprint()

    def _reverse(registry, ordered):
        ordered.reverse()
        for key in list(registry):
            registry[key] = registry.pop(key)

    _patch_registry(monkeypatch, _reverse)
    assert critic._registry_fingerprint() == before


def test_the_critic_hint_hashed_is_the_hint_rendered() -> None:
    """One function computes both, so the fingerprint can never hash a different slice of the
    description than the payload sends."""
    registered = concepts_mod.CONCEPT_REGISTRY["customer_id"]
    item = critic.ConceptCriticItemV1(
        logical_ref="ftr::s.t.c", column_name="cif_id", declared_type="varchar",
        definition=None, proposed_concept="customer_id")
    payload = critic._item_payload(item, ())
    assert (payload["proposed_concept_meaning"]["hint"]
            == critic._description_hint(registered.description))


def test_the_critic_replay_identity_moves_with_its_context(monkeypatch) -> None:
    """A column whose SEMANTIC CONTEXT changed is a new question for the critic — unlike the
    classifier cache (D12.5), the critic's replay store is content-addressed on the exact payload it
    was asked about, so a changed context must not replay yesterday's verdict."""
    item = critic.ConceptCriticItemV1(
        logical_ref="ftr::s.t.c", column_name="cif_id", declared_type="varchar",
        definition=None, proposed_concept="customer_id",
        context={"data_domain": "Party"})
    other = critic.ConceptCriticItemV1(
        logical_ref="ftr::s.t.c", column_name="cif_id", declared_type="varchar",
        definition=None, proposed_concept="customer_id",
        context={"data_domain": "Compliance"})
    assert (critic._input_hash(critic._item_payload(item, ()), "rev")
            != critic._input_hash(critic._item_payload(other, ()), "rev"))


def test_the_critic_result_version_was_bumped_with_the_payload() -> None:
    """A v1 stored verdict was reached against a narrower payload and an identifier-only scope; the
    version bump retires those rows rather than replaying them under a changed question."""
    assert critic.CONCEPT_CRITIC_VERSION == 2
