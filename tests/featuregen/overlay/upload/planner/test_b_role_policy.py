"""Phase 3C.2b-i-B · Task 6 — the refined computation-role policy.

Pins: (1) ``computation_role`` is TOTAL over every concept ``group`` in the real registry — it
always returns a ``SemanticRole`` or a ``RolePolicyRejection``, never ``None``, never raises;
(2) MEASURE gates on ``additivity != "n/a"`` for the 6 MEASURE-eligible groups, NOT on ``group``
alone (``impairment_stage``/``green_flag`` are accounting/esg but ``additivity == "n/a"`` and must
reject, not measure); (3) TIME gates on ``pit_role`` for ``group == "temporal"``, NOT on ``group``
alone (6 of 20 temporal concepts carry ``pit_role == "none"`` and are never time anchors); (4) the
policy is pure (no DB/conn param) and reuses ``SemanticRole``/``ROLE_POLICY_VERSION`` by import,
never redefining them."""
from __future__ import annotations

from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY, Concept, concept
from featuregen.overlay.upload.planner import b_role_policy as rp
from featuregen.overlay.upload.planner.b_dispositions import (
    ROLE_POLICY_VERSION,
    BDisposition,
)
from featuregen.overlay.upload.planner.multisource_contracts import SemanticRole

# ---------------------------------------------------------------------------
# MEASURE — gates on additivity, not group alone.
# ---------------------------------------------------------------------------


def test_additive_monetary_concept_is_measure() -> None:
    c = concept("monetary_flow")
    assert c is not None
    assert c.additivity == "additive"
    assert rp.computation_role(c) is SemanticRole.measure


def test_semi_additive_concept_is_measure() -> None:
    c = concept("ead")
    assert c is not None
    assert c.additivity == "semi_additive"
    assert rp.computation_role(c) is SemanticRole.measure


def test_non_additive_concept_is_measure() -> None:
    c = concept("lgd")
    assert c is not None
    assert c.additivity == "non_additive"
    assert rp.computation_role(c) is SemanticRole.measure


def test_impairment_stage_is_not_a_measure_despite_accounting_group() -> None:
    """THE TRAP: accounting group, additivity='n/a' (an ordinal) — must reject, not measure."""
    c = concept("impairment_stage")
    assert c is not None
    assert c.group == "accounting"
    assert c.additivity == "n/a"
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.additivity_not_asserted
    assert rp.reason_to_b_disposition(result.reason) is BDisposition.role_not_aggregatable


def test_green_flag_is_not_a_measure_despite_esg_group() -> None:
    """THE TRAP: esg group, additivity='n/a' (a boolean flag) — must reject, not measure."""
    c = concept("green_flag")
    assert c is not None
    assert c.group == "esg"
    assert c.additivity == "n/a"
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.additivity_not_asserted


# ---------------------------------------------------------------------------
# TIME — gates on pit_role, not group alone.
# ---------------------------------------------------------------------------


def test_temporal_concept_with_accepted_pit_role_is_time() -> None:
    c = concept("as_of_date")
    assert c is not None
    assert c.pit_role == "as_of"
    assert rp.computation_role(c) is SemanticRole.time


def test_temporal_concept_with_event_pit_role_is_time() -> None:
    c = concept("event_timestamp")
    assert c is not None
    assert c.pit_role == "event"
    assert rp.computation_role(c) is SemanticRole.time


def test_temporal_with_pit_role_none_and_no_additivity_is_rejected() -> None:
    """THE TRAP: group='temporal' does NOT imply time — vintage has pit_role='none' and
    additivity='n/a', so it is neither TIME nor MEASURE."""
    c = concept("vintage")
    assert c is not None
    assert c.group == "temporal"
    assert c.pit_role == "none"
    assert c.additivity == "n/a"
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.temporal_not_anchor


def test_temporal_with_pit_role_none_but_additivity_asserted_is_measure() -> None:
    """duration_tenure: temporal group, pit_role='none' (never time), but additivity='non_additive'
    (asserted numeric) — falls through to MEASURE within the temporal branch."""
    c = concept("duration_tenure")
    assert c is not None
    assert c.group == "temporal"
    assert c.pit_role == "none"
    assert c.additivity == "non_additive"
    assert rp.computation_role(c) is SemanticRole.measure


# ---------------------------------------------------------------------------
# COUNTED — identifiers with an entity_link.
# ---------------------------------------------------------------------------


def test_customer_id_is_counted() -> None:
    c = concept("customer_id")
    assert c is not None
    assert c.entity_link == "customer"
    assert rp.computation_role(c) is SemanticRole.counted


def test_account_id_is_counted() -> None:
    c = concept("account_id")
    assert c is not None
    assert c.entity_link == "account"
    assert rp.computation_role(c) is SemanticRole.counted


# ---------------------------------------------------------------------------
# Non-computational groups — reject regardless of additivity.
# ---------------------------------------------------------------------------


def test_currency_group_concept_is_rejected() -> None:
    c = concept("currency_code")
    assert c is not None
    assert c.group == "currency"
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.group_not_computational


def test_categorical_group_concept_is_rejected() -> None:
    c = concept("category_code")
    assert c is not None
    assert c.group == "categorical"
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.group_not_computational


def test_flag_group_concept_is_rejected() -> None:
    c = concept("boolean_flag")
    assert c is not None
    assert c.group == "flag"
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.group_not_computational


def test_text_group_concept_is_rejected() -> None:
    c = concept("free_text")
    assert c is not None
    assert c.group == "text"
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.group_not_computational


# ---------------------------------------------------------------------------
# Drift branches — hand-built Concept objects, no registry mutation.
# ---------------------------------------------------------------------------


def test_identifier_without_entity_link_is_rejected() -> None:
    c = Concept("synthetic_id", "identifier", entity_link=None)
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.identifier_without_entity_link
    assert rp.reason_to_b_disposition(result.reason) is BDisposition.role_not_aggregatable


def test_unknown_group_is_rejected_by_the_totality_guard() -> None:
    c = Concept("x", "made_up_group")
    result = rp.computation_role(c)
    assert isinstance(result, rp.RolePolicyRejection)
    assert result.reason is rp.RolePolicyReason.unknown_group
    assert rp.reason_to_b_disposition(result.reason) is BDisposition.role_not_aggregatable


# ---------------------------------------------------------------------------
# Totality over the whole registry — the load-bearing proof.
# ---------------------------------------------------------------------------


def test_computation_role_is_total_over_the_entire_registry() -> None:
    """Every concept in CONCEPT_REGISTRY must yield a SemanticRole or a RolePolicyRejection —
    never raise, never return None. This is the "total over every group" proof."""
    for c in CONCEPT_REGISTRY.values():
        result = rp.computation_role(c)
        assert result is not None
        assert isinstance(result, SemanticRole) or isinstance(result, rp.RolePolicyRejection)


def test_every_reason_folds_to_role_not_aggregatable() -> None:
    """All five RolePolicyReason members fold to the single coarse BDisposition bucket."""
    for reason in rp.RolePolicyReason:
        assert rp.reason_to_b_disposition(reason) is BDisposition.role_not_aggregatable


# ---------------------------------------------------------------------------
# Purity / versioning.
# ---------------------------------------------------------------------------


def test_computation_role_has_no_db_parameter() -> None:
    import inspect

    sig = inspect.signature(rp.computation_role)
    params = list(sig.parameters)
    assert params == ["concept"]
    for name in params:
        assert "conn" not in name.lower()
        assert "db" not in name.lower()


def test_role_policy_version_is_reused_not_redefined() -> None:
    assert rp.ROLE_POLICY_VERSION == "3c2bib.role.1.0.0"
    assert rp.ROLE_POLICY_VERSION is ROLE_POLICY_VERSION


# ---------------------------------------------------------------------------
# Group-set constants — the closed vocabularies are auditable module constants.
# ---------------------------------------------------------------------------


def test_measure_eligible_groups_constant() -> None:
    assert rp.MEASURE_ELIGIBLE_GROUPS == frozenset(
        {"monetary", "quantity_risk", "accounting", "regulatory_capital", "esg", "crypto"})


def test_non_computational_groups_constant() -> None:
    assert rp.NON_COMPUTATIONAL_GROUPS == frozenset({
        "categorical", "geographic", "flag", "sensitive", "text", "label", "behavioural",
        "network", "bitemporal", "currency", "eligibility",
    })


def test_all_19_groups_are_covered_by_the_policy_branches() -> None:
    all_groups = {c.group for c in CONCEPT_REGISTRY.values()}
    assert all_groups == {
        "monetary", "quantity_risk", "accounting", "regulatory_capital", "esg", "crypto",
        "temporal", "identifier", "categorical", "geographic", "flag", "sensitive", "text",
        "label", "behavioural", "network", "bitemporal", "currency", "eligibility",
    }
    assert len(all_groups) == 19
    covered = rp.MEASURE_ELIGIBLE_GROUPS | {"temporal", "identifier"} | rp.NON_COMPUTATIONAL_GROUPS
    assert covered == all_groups
