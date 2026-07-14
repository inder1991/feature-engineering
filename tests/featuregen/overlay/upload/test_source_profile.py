from featuregen.overlay.evidence import AssertionStrength
from featuregen.overlay.upload.source_profile import (
    FTR_GLOSSARY_PROFILE,
    TECHNICAL_CSV_PROFILE,
    SourceCapabilityProfile,
    profile_for_upload,
    strength_for,
)

# Canonical technical-CSV headers (spec §U.1 — structure-vouched) and a glossary-shaped upload.
_CANONICAL_HEADERS = ["source", "table", "column", "type", "definition", "sensitivity"]
_GLOSSARY_HEADERS = ["business_term", "definition", "bian_path", "fibo_path", "domain"]


def test_glossary_attests_definition_but_proposes_domain_and_sensitivity():
    # A glossary vouches for its semantics (definition) but only PROPOSES the rest.
    assert strength_for(FTR_GLOSSARY_PROFILE, "definition") == AssertionStrength.ATTESTED
    assert strength_for(FTR_GLOSSARY_PROFILE, "domain") == AssertionStrength.PROPOSED
    assert strength_for(FTR_GLOSSARY_PROFILE, "sensitivity") == AssertionStrength.PROPOSED


def test_unknown_field_defaults_to_proposed():
    assert strength_for(FTR_GLOSSARY_PROFILE, "not_a_real_field") == AssertionStrength.PROPOSED
    assert strength_for(TECHNICAL_CSV_PROFILE, "not_a_real_field") == AssertionStrength.PROPOSED


def test_technical_profile_attests_physical_type():
    # The technical profile vouches for structure: `type` is a structural attested field.
    assert TECHNICAL_CSV_PROFILE.attests("type") is True
    assert strength_for(TECHNICAL_CSV_PROFILE, "type") == AssertionStrength.ATTESTED


def test_glossary_does_not_attest_physical_type():
    # Drives Task-4's profile-aware validation: a glossary's `type` is a readiness gap, not a fact.
    assert FTR_GLOSSARY_PROFILE.attests("type") is False
    assert strength_for(FTR_GLOSSARY_PROFILE, "type") == AssertionStrength.PROPOSED


def test_attests_covers_attested_and_structural_but_not_proposed():
    # Glossary: attested field yes, proposed field no.
    assert FTR_GLOSSARY_PROFILE.attests("business_term") is True
    assert FTR_GLOSSARY_PROFILE.attests("domain") is False
    # Technical: attested field yes, structural field yes.
    assert TECHNICAL_CSV_PROFILE.attests("additivity") is True
    assert TECHNICAL_CSV_PROFILE.attests("joins_to") is True


def test_profile_shapes_match_spec_u1():
    assert FTR_GLOSSARY_PROFILE.source_type == "ftr_glossary"
    assert FTR_GLOSSARY_PROFILE.attested_fields == frozenset(
        {"definition", "business_term", "bian_path", "fibo_path"})
    assert FTR_GLOSSARY_PROFILE.proposed_fields == frozenset(
        {"domain", "sample_profile", "sensitivity"})
    assert FTR_GLOSSARY_PROFILE.structural_fields == frozenset()

    assert TECHNICAL_CSV_PROFILE.source_type == "technical_csv"
    assert TECHNICAL_CSV_PROFILE.attested_fields == frozenset(
        {"definition", "sensitivity", "additivity", "unit", "currency", "entity"})
    assert TECHNICAL_CSV_PROFILE.proposed_fields == frozenset()
    assert TECHNICAL_CSV_PROFILE.structural_fields == frozenset(
        {"type", "grain", "joins_to", "cardinality"})


def test_profile_for_upload_picks_glossary_for_glossary_headers():
    assert profile_for_upload(_GLOSSARY_HEADERS) is FTR_GLOSSARY_PROFILE


def test_profile_for_upload_picks_technical_for_canonical_headers():
    assert profile_for_upload(_CANONICAL_HEADERS) is TECHNICAL_CSV_PROFILE


def test_profile_for_upload_is_header_normalization_tolerant():
    # Glossary detection survives casing / spaces / a UTF-8 BOM on the first header (Excel export).
    messy = ["﻿Business Term", "Definition", "BIAN Path", "FIBO Path"]
    assert profile_for_upload(messy) is FTR_GLOSSARY_PROFILE


def test_technical_alias_keys_block_glossary_misclassification():
    # M-7: the technical reader accepts `attribute`/`columnname` as `column` and `tablename` as
    # `table` (_headers._ALIASES), so a technical CSV keyed on those aliases plus a stray
    # glossary-signal header must stay TECHNICAL — flipping it to the glossary profile drops the
    # FQN row key and quarantines every row. "Absence of technical row-keys" must match what the
    # reader actually accepts as a row key.
    assert profile_for_upload(["attribute", "tablename", "type", "business_term"]) \
        is TECHNICAL_CSV_PROFILE
    assert profile_for_upload(["Column Name", "table", "type", "bian_path"]) \
        is TECHNICAL_CSV_PROFILE
    # A real glossary (glossary signals, NO column/table alias at all) is still detected.
    assert profile_for_upload(["business_term", "definition", "bian_path"]) is FTR_GLOSSARY_PROFILE


def test_profile_is_frozen():
    import dataclasses

    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        FTR_GLOSSARY_PROFILE.source_type = "mutated"  # type: ignore[misc]


def test_profile_is_constructible():
    p = SourceCapabilityProfile(
        source_type="x",
        attested_fields=frozenset({"a"}),
        proposed_fields=frozenset({"b"}),
        structural_fields=frozenset({"c"}))
    assert p.attests("a") is True and p.attests("c") is True and p.attests("b") is False
