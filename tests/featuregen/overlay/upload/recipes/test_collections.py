"""BR-12 — the collections pack: contract grain first, outcomes fenced post-default."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.collections import (
    COLLECTIONS_RECIPES,
    POST_DEFAULT_ONLY,
)
from featuregen.overlay.upload.templates import COLLECTIONS_TEMPLATES

BY_ID = {r.recipe_id: r for r in COLLECTIONS_RECIPES}


def test_every_legacy_collections_id_is_explicitly_replaced():
    assert {t.id for t in COLLECTIONS_TEMPLATES} <= v2_replaced_legacy_ids()


def test_collections_outputs_are_correct_at_facility_grain_before_rollup():
    """The acceptance: contract grain first; the customer rollup is a SEPARATE recipe under
    the governed contract-to-customer policy."""
    rollups = [r for r in COLLECTIONS_RECIPES if r.output_grain == "customer"]
    assert [r.recipe_id for r in rollups] == ["customer_worst_days_in_collection"]
    assert any(ref.startswith("allocation:contract-to-customer")
               for ref in rollups[0].eligibility.policy_refs)
    assert all(r.output_grain == "facility"
               for r in COLLECTIONS_RECIPES if r not in rollups)


def test_promises_read_the_governed_promise_concepts():
    for rid in ("promise_kept_share", "promise_amount_collected_share"):
        concepts = {op.concept for op in BY_ID[rid].operands}
        assert {"promise_amount", "promise_due_date", "promise_outcome"} <= concepts, rid
    # a broken promise partially paid: the amount share carries the allocation policy
    amt = BY_ID["promise_amount_collected_share"]
    assert any(ref.startswith("allocation:") for ref in amt.eligibility.policy_refs)
    assert "partially paid" in amt.business_definition


def test_rpc_reads_contact_events_never_cost():
    rate = BY_ID["right_party_contact_rate"]
    concepts = {op.concept for op in rate.operands}
    assert {"contact_attempt_event", "contact_outcome", "right_party_contact_flag"} <= concepts
    assert "cost_to_collect" not in concepts
    assert "never a proxy" in rate.business_definition
    # ...and the cost recipe is about cost, with its own post-default fence
    cost = BY_ID["cost_to_collect_ratio"]
    assert any(op.concept == "cost_to_collect" for op in cost.operands)
    assert cost.leakage.classification == "outcome"


def test_cure_and_roll_forward_compare_state_at_window_start_and_end():
    for rid in ("cured_in_window_flag", "rolled_forward_flag"):
        assert "window START" in BY_ID[rid].temporal.snapshot_policy, rid
    assert "re-default inside the window honestly reads NOT cured" in \
        BY_ID["cured_in_window_flag"].business_definition


def test_post_default_outcomes_are_fenced_from_pre_default_models():
    """The acceptance: pre-default applicability refuses post-default outcomes — write-off,
    recovery and cure outcomes prohibit origination and default prediction by declaration."""
    assert POST_DEFAULT_ONLY.classification == "outcome"
    for rid in ("cured_in_window_flag", "cost_to_collect_ratio", "recovery_rate",
                "write_off_amount_sum", "write_off_severity_share"):
        leak = BY_ID[rid].leakage
        assert leak.classification == "outcome", rid
        assert "default_prediction" in leak.prohibited_stages, rid


def test_recovery_divides_by_the_frozen_defaulted_balance():
    r = BY_ID["recovery_rate"]
    denom = next(op for op in r.operands if op.role == "defaulted_balance")
    assert denom.economic_role == "defaulted_balance_snapshot"
    assert "frozen at default" in r.business_definition
    assert any(ref.startswith("currency_conversion:") for ref in r.eligibility.policy_refs)


def test_hardship_reads_its_lifecycle_not_payment_shapes():
    r = BY_ID["hardship_arrangement_in_window"]
    assert r.source_grain == "hardship_event"
    assert "never inferred from payment shapes" in r.business_definition


def test_plan_adherence_reads_the_arrangement_schedule():
    r = BY_ID["plan_installments_met_streak"]
    concepts = {op.concept for op in r.operands}
    assert {"due_date", "scheduled_amount", "payment_allocation"} <= concepts
    assert r.source_grain == "arrangement_schedule"
