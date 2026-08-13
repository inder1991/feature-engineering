"""C3's ratchet — no recipe-contract field without a consumer.

Every behavior-bearing field on ``OperandSpecV2`` / ``OutputSpecV2`` must appear in the
registered-consumer map below, as ONE of:

* ``enforced:<where>`` — a live check consumes it (binder / eligibility fold / gauntlet /
  temporal compiler / activation policy);
* ``partial:<where + honest gap>`` — the verifiable half is enforced and the gap is named;
* ``expectation:<where>`` — authored prose validated against at a LATER seam (formula
  authoring/review), deliberately never a binding-time authority;
* ``display:<where>`` — identity/presentation only, by design.

A NEW field added to either contract fails this test until its consumer (or its honest
non-consumer disposition) is registered — the review's "transported-but-unconsumed" failure
mode cannot recur silently.
"""
from __future__ import annotations

import dataclasses

from featuregen.overlay.upload.recipe_contract_v2 import OperandSpecV2, OutputSpecV2

OPERAND_FIELD_CONSUMERS: dict[str, str] = {
    "role": "enforced:binder verdict identity + temporal role references",
    "concept": "enforced:two-tier matcher + eligibility concept match",
    "operand_class": "enforced:eligibility class/type-family laws + typed gauntlet checks",
    "required": "enforced:binder REQUIRED_OPERAND_MISSING vs optional degrade",
    "allowed_source_grains": ("partial:eligibility 2c enforces the event/snapshot AXIS at "
                              "declared+ (SOURCE_GRAIN_MISMATCH); finer row-kinds have no "
                              "catalog fact yet — named honest gap"),
    "join_role": ("partial:cross-catalog planner (3C.2a) consumes join paths; a "
                  "single-source frozen plan has no join to check by construction"),
    "temporal_role": "enforced:compile_temporal role references (event/as-of/knowledge)",
    "distinct_binding_group": "enforced:opposing-legs law (C3 sign representations)",
    "unit_expectation": ("partial:eligibility 2d enforces the currency-contradiction sliver "
                         "(UNIT_INCOMPATIBLE); absent unit facts report through C5's "
                         "family tri-state"),
    "currency_expectation": "enforced:CURRENCY_POLICY_MISSING floor on currency-less columns",
    "economic_role": "enforced:governed economic-role evidence match (both binders)",
    "sign_direction_expectation": ("expectation:formula-expectation validation at the "
                                   "authoring seam — NEVER binding authority (C3; the "
                                   "pre-C3 defect treated it as a license)"),
    "status_policy_ref": ("enforced:STATUS_POLICY_UNRESOLVED rides every dependent "
                          "candidate as named setup work until a resolver exists"),
    "relationship_requirement": ("enforced:RELATIONSHIP_REQUIRED (eligibility) + the "
                                 "dataset story's cross-dataset refusal (B7)"),
    "suggestion_authority": "enforced:AUTHORITY_MATRIX suggestion floor (SE-4)",
    "execution_authority": "enforced:AUTHORITY_MATRIX execution floor (C2 activation)",
}

OUTPUT_FIELD_CONSUMERS: dict[str, str] = {
    "output_id": "display:card/variant identity",
    "display_label": "display:card title",
    "output_type": "expectation:formula-expectation schema at the authoring seam",
    "additivity": ("partial:the OUTPUT's own additivity is contract prose; the additivity "
                   "LAW (eligibility 2e) enforces against the BOUND MEASURE's declared "
                   "additivity, which is the checkable fact"),
    "unit_kind": "enforced:ratio-shape detection (gauntlet zero-denominator law) + card unit",
    "unit_policy": "expectation:formula-expectation validation at the authoring seam",
    "currency_policy": "expectation:formula-expectation validation at the authoring seam",
    "null_input_policy": "expectation:formula-expectation validation at the authoring seam",
    "empty_population_policy": "expectation:formula-expectation validation at the authoring "
                               "seam",
    "zero_denominator_policy": ("enforced:gauntlet OUTPUT_POLICY_INCOMPLETE when the output "
                                "is ratio-shaped and the policy is unauthored"),
    "valid_range": "expectation:formula-expectation validation at the authoring seam",
    "scale_policy": "expectation:formula-expectation validation at the authoring seam",
    "aggregation_over_entity": "enforced:additivity law (2e) + ratio-shape detection",
    "aggregation_over_time": "enforced:additivity law (2e) + ratio-shape detection",
}

_DISPOSITIONS = ("enforced:", "partial:", "expectation:", "display:")


def test_every_operand_field_has_a_registered_consumer():
    declared = {f.name for f in dataclasses.fields(OperandSpecV2)}
    assert declared == set(OPERAND_FIELD_CONSUMERS), (
        "OperandSpecV2 fields changed — register the new field's consumer (or its honest "
        f"non-consumer disposition): {declared ^ set(OPERAND_FIELD_CONSUMERS)}")
    for field, disposition in OPERAND_FIELD_CONSUMERS.items():
        assert disposition.startswith(_DISPOSITIONS), (field, disposition)


def test_every_output_field_has_a_registered_consumer():
    declared = {f.name for f in dataclasses.fields(OutputSpecV2)}
    assert declared == set(OUTPUT_FIELD_CONSUMERS), (
        "OutputSpecV2 fields changed — register the new field's consumer (or its honest "
        f"non-consumer disposition): {declared ^ set(OUTPUT_FIELD_CONSUMERS)}")
    for field, disposition in OUTPUT_FIELD_CONSUMERS.items():
        assert disposition.startswith(_DISPOSITIONS), (field, disposition)


def test_the_enforced_entries_point_at_real_code():
    """The map's ENFORCED claims are not prose: the named codes exist in the closed reason
    vocabulary, so a retired check breaks this pin."""
    from featuregen.overlay.upload import semantic_eligibility_reasons as R

    for code in ("SOURCE_GRAIN_MISMATCH", "UNIT_INCOMPATIBLE", "ADDITIVITY_INCOMPATIBLE",
                 "STATUS_POLICY_UNRESOLVED", "OUTPUT_POLICY_INCOMPLETE",
                 "CURRENCY_POLICY_MISSING", "RELATIONSHIP_REQUIRED"):
        assert getattr(R, code) in R.REASON_FAMILIES
