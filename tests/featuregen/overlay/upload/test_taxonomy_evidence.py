"""Task 7 — concept-registry taxonomy derivation with strength propagation.

Once a column's concept is known, its behavioural fields (additivity, temporal/PIT role, sensitivity
floor, leakage anchor) are DERIVED from the concept registry — not independently hallucinated. The
governing invariant (spec §3.2) is STRENGTH PROPAGATION: a derivation from an ``llm/proposed`` concept
yields ``proposed`` derivations, a derivation from a ``confirmed`` concept yields ``confirmed`` ones —
never stronger than the concept it was derived from.
"""
from featuregen.overlay.evidence import AssertionStrength
from featuregen.overlay.upload.concepts import UNCLASSIFIED, concept
from featuregen.overlay.upload.taxonomy_evidence import derive_concept_evidence

# The REAL registry values for monetary_stock (read from concepts.py, not assumed):
#   additivity="semi_additive", pit_role="none", sensitivity="public", leakage_anchor=False.
_MS = concept("monetary_stock")
assert _MS is not None and _MS.additivity == "semi_additive"
assert _MS.pit_role == "none" and _MS.sensitivity == "public" and _MS.leakage_anchor is False


def test_monetary_stock_derives_its_behavioural_fields_at_proposed():
    triples = derive_concept_evidence("monetary_stock", AssertionStrength.PROPOSED)
    assert ("additivity", "semi_additive", AssertionStrength.PROPOSED) in triples
    assert ("temporal_role", "none", AssertionStrength.PROPOSED) in triples
    assert ("sensitivity_floor", "public", AssertionStrength.PROPOSED) in triples
    assert ("leakage_anchor", False, AssertionStrength.PROPOSED) in triples


def test_every_derived_strength_equals_input_and_is_never_higher():
    # The core §3.2 invariant: a PROPOSED concept yields ONLY proposed derivations — nothing is
    # silently promoted to supported/attested/confirmed.
    triples = derive_concept_evidence("monetary_stock", AssertionStrength.PROPOSED)
    assert triples, "monetary_stock must derive behavioural evidence"
    assert all(strength == AssertionStrength.PROPOSED for _, _, strength in triples)
    stronger = {
        AssertionStrength.SUPPORTED,
        AssertionStrength.ATTESTED,
        AssertionStrength.CONFIRMED,
    }
    assert not any(strength in stronger for _, _, strength in triples)


def test_confirmed_concept_yields_confirmed_derivations():
    triples = derive_concept_evidence("monetary_stock", AssertionStrength.CONFIRMED)
    assert ("additivity", "semi_additive", AssertionStrength.CONFIRMED) in triples
    assert ("temporal_role", "none", AssertionStrength.CONFIRMED) in triples
    assert ("sensitivity_floor", "public", AssertionStrength.CONFIRMED) in triples
    assert ("leakage_anchor", False, AssertionStrength.CONFIRMED) in triples
    assert all(strength == AssertionStrength.CONFIRMED for _, _, strength in triples)


def test_sensitivity_is_emitted_as_a_floor_not_an_operational_classification():
    # Review #8: sensitivity is a FLOOR, emitted under a DISTINCT field name so Task 8 feeds it
    # through safety_floor.apply_sensitivity_floor — it must NOT masquerade as an operational
    # "sensitivity" classification field.
    triples = derive_concept_evidence("monetary_stock", AssertionStrength.PROPOSED)
    field_names = {field for field, _, _ in triples}
    assert "sensitivity_floor" in field_names
    assert "sensitivity" not in field_names


def test_additivity_is_skipped_when_not_applicable():
    # A concept whose additivity is the "n/a" not-applicable sentinel (e.g. a temporal date) emits
    # no additivity triple — but still derives its real temporal role.
    triples = derive_concept_evidence("as_of_date", AssertionStrength.PROPOSED)
    field_names = {field for field, _, _ in triples}
    assert "additivity" not in field_names
    assert ("temporal_role", "as_of", AssertionStrength.PROPOSED) in triples


def test_proxy_sensitivity_propagates_as_the_floor_value():
    triples = derive_concept_evidence("country_code", AssertionStrength.PROPOSED)
    assert ("sensitivity_floor", "proxy", AssertionStrength.PROPOSED) in triples


def test_leakage_anchor_true_is_derived_for_a_label_concept():
    triples = derive_concept_evidence("outcome_label", AssertionStrength.CONFIRMED)
    assert ("leakage_anchor", True, AssertionStrength.CONFIRMED) in triples
    assert all(strength == AssertionStrength.CONFIRMED for _, _, strength in triples)


def test_unknown_concept_yields_no_evidence():
    assert derive_concept_evidence("definitely_not_a_concept", AssertionStrength.CONFIRMED) == []


def test_unclassified_yields_no_evidence():
    assert derive_concept_evidence(UNCLASSIFIED, AssertionStrength.PROPOSED) == []
