"""Delivery B item 8 — source-authority field policies + capability-profile version constant.

Seven registered policies govern how load-bearing a source-declared field may be:

* ``unit`` / ``currency`` — OPERATIONAL, but load-bearing ONLY under a source-attested or
  human-confirmed signal (an LLM proposal alone is never enough, §8).
* ``data_type`` — OPERATIONAL only when a technical STRUCTURAL source attests it; a human
  confirmation alone does not certify a physical type.
* ``business_term`` / ``term_type`` / ``declared_type`` / ``entity`` — RECOMMENDATION-ceilinged
  advisory fields (the glossary-declared SQL type is a HINT; a VERIFIED ``entity_assignment`` fact
  remains the operational entity path, built in Delivery E).

Plus ``SOURCE_CAPABILITY_PROFILE_VERSION`` — the version stamp later evidence writers record.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import Disqualifier, InfluenceTier, evaluate
from featuregen.overlay.upload.field_policies import _POLICIES, policy_for
from featuregen.overlay.upload.source_profile import SOURCE_CAPABILITY_PROFILE_VERSION

# Active evidence sets for exercising operational_rule via the pure evaluator (spec §4.1).
_SOURCE_ATTESTED_SET = frozenset({(EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)})
_HUMAN_CONFIRMED_SET = frozenset({(EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)})
_LLM_PROPOSED_ONLY_SET = frozenset({(EvidenceProducer.LLM, AssertionStrength.PROPOSED)})

_NEW_FIELDS = (
    "business_term",
    "term_type",
    "declared_type",
    "data_type",
    "unit",
    "currency",
    "entity",
)


def test_all_seven_new_fields_registered():
    for field in _NEW_FIELDS:
        assert field in _POLICIES, f"{field} missing from _POLICIES"
        assert policy_for(field) is not None, field


@pytest.mark.parametrize("field", ["unit", "currency"])
def test_unit_and_currency_operational_source_attested_or_human_confirmed(field):
    p = policy_for(field)
    assert p is not None
    assert p.influence_max is InfluenceTier.OPERATIONAL
    # The rule accepts a source-attested OR human-confirmed active set...
    assert evaluate(p.operational_rule, _SOURCE_ATTESTED_SET)
    assert evaluate(p.operational_rule, _HUMAN_CONFIRMED_SET)
    # ...and REJECTS an LLM-proposed-only set (§8: an LLM proposal is never load-bearing alone).
    assert not evaluate(p.operational_rule, _LLM_PROPOSED_ONLY_SET)
    # A material change flags the field pending revalidation, blocking the load-bearing value.
    assert Disqualifier.CONFIRMATION_PENDING_REVALIDATION in p.disqualifiers


def test_data_type_operational_only_when_source_attested():
    p = policy_for("data_type")
    assert p is not None
    assert p.influence_max is InfluenceTier.OPERATIONAL
    assert evaluate(p.operational_rule, _SOURCE_ATTESTED_SET)
    # Neither a human confirmation nor an LLM proposal certifies a PHYSICAL type on its own.
    assert not evaluate(p.operational_rule, _HUMAN_CONFIRMED_SET)
    assert not evaluate(p.operational_rule, _LLM_PROPOSED_ONLY_SET)
    assert Disqualifier.CONFIRMATION_PENDING_REVALIDATION in p.disqualifiers


@pytest.mark.parametrize("field", ["business_term", "term_type", "declared_type", "entity"])
def test_advisory_fields_are_recommendation_ceilinged(field):
    p = policy_for(field)
    assert p is not None
    assert p.influence_max is InfluenceTier.RECOMMENDATION
    # The advisory operational_rule still requires a source/human signal (documents intent for a
    # future promotion; the ceiling is the hard guarantee).
    assert evaluate(p.operational_rule, _SOURCE_ATTESTED_SET)
    assert evaluate(p.operational_rule, _HUMAN_CONFIRMED_SET)
    assert not evaluate(p.operational_rule, _LLM_PROPOSED_ONLY_SET)


def test_source_capability_profile_version_is_nonempty_str():
    assert isinstance(SOURCE_CAPABILITY_PROFILE_VERSION, str)
    assert SOURCE_CAPABILITY_PROFILE_VERSION != ""
