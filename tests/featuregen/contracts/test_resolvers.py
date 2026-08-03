"""Task 0S bullet 4 — the registered shared domain/entity resolver seam (0F-6 / D9).

No controlled business-domain or entity resolver exists at this baseline. The seam is
frozen so that ABSENCE yields attributed text and no controlled facet — never an invented
ID — and so the semantic plan can later register the real resolver without any suggestion
-side change.
"""
from __future__ import annotations

import pytest

from featuregen.contracts.evidence_axes import (
    AttributedLabelV1,
    AttributedTextV1,
    EvidenceAuthorityV1,
)
from featuregen.contracts.resolvers import (
    RESOLVER_AXES,
    ResolverSeamError,
    register_resolver,
    registered_resolver,
    reset_resolver,
    resolve_controlled,
    resolve_or_text,
)
from featuregen.overlay.evidence import AssertionStrength, EvidenceLifecycle, EvidenceProducer

_EVIDENCE = (EvidenceAuthorityV1(
    producer=EvidenceProducer.SOURCE, strength=AssertionStrength.SUPPORTED,
    lifecycle=EvidenceLifecycle.ACTIVE, producer_ref=None, evidence_id=None),)


@pytest.fixture(autouse=True)
def _clean_seam():
    yield
    for axis in RESOLVER_AXES:
        reset_resolver(axis)


class _StaticResolver:
    def __init__(self, mapping):
        self._mapping = mapping

    def resolve(self, text):
        return self._mapping.get(text)


def test_the_frozen_axes_are_domain_and_entity():
    assert RESOLVER_AXES == ("business_domain", "entity")


def test_absent_resolver_yields_attributed_text_and_no_controlled_facet():
    assert registered_resolver("business_domain") is None
    assert resolve_controlled("business_domain", "Retail Lending") is None
    out = resolve_or_text("business_domain", "Retail Lending", basis="llm_proposed",
                          evidence=_EVIDENCE, source_refs=("catalog:acme.loans",))
    assert isinstance(out, AttributedTextV1)  # text, not a facet ID
    assert out.value == "Retail Lending"  # verbatim — never lowercased/slugged into an ID
    assert out.basis == "llm_proposed"
    assert out.evidence == _EVIDENCE
    assert out.operational_influence is None
    assert out.source_refs == ("catalog:acme.loans",)


def test_registered_resolver_that_cannot_map_still_yields_text():
    register_resolver("entity", _StaticResolver({}))
    out = resolve_or_text("entity", "the counterparty", basis="human", evidence=())
    assert isinstance(out, AttributedTextV1)
    assert out.value == "the counterparty"


def test_registered_resolver_supplies_the_controlled_label():
    label = AttributedLabelV1(id="dom_retail_lending", display_name="Retail Lending",
                              basis="catalog_resolved", evidence=_EVIDENCE,
                              operational_influence=None, source_refs=())
    register_resolver("business_domain", _StaticResolver({"Retail Lending": label}))
    assert resolve_controlled("business_domain", "Retail Lending") is label
    assert resolve_or_text("business_domain", "Retail Lending", basis="catalog_resolved",
                           evidence=()) is label


def test_unknown_axis_fails_loudly():
    with pytest.raises(ResolverSeamError):
        register_resolver("product_line", _StaticResolver({}))
    with pytest.raises(ResolverSeamError):
        resolve_controlled("product_line", "x")
    with pytest.raises(ResolverSeamError):
        reset_resolver("product_line")


def test_second_registration_for_an_axis_fails_loudly():
    register_resolver("entity", _StaticResolver({}))
    with pytest.raises(ResolverSeamError):
        register_resolver("entity", _StaticResolver({}))


def test_a_resolver_returning_a_non_label_fails_loudly():
    register_resolver("entity", _StaticResolver({"x": "ent_x"}))  # a bare ID is an invented facet
    with pytest.raises(ResolverSeamError):
        resolve_controlled("entity", "x")
