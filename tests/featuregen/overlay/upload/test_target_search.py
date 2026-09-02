"""Registry search — the step that runs BEFORE any model call, so an existing label surfaces
instead of being re-invented."""
from __future__ import annotations

from dataclasses import replace

from featuregen.overlay.upload.target_contract import StateChangeRuleV1, TargetHeaderV1
from featuregen.overlay.upload.target_search import near_duplicates, search_targets
from featuregen.overlay.upload.target_store import register_target, targets_for_entity


def _rule(name: str = "tgt_npe_90d", entity: str = "customer",
          window: int = 90) -> StateChangeRuleV1:
    return StateChangeRuleV1(
        header=TargetHeaderV1(name=name, entity=entity, anchor_catalog="cib",
                              grain_ref="public.bo_cib_customer.cust_num",
                              as_of_ref="public.bo_cib_customer.business_dt",
                              window_days=window, as_of_frequency="monthly",
                              label_type="binary", operator=">=", threshold=1.0),
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Non-performing",))


def test_a_matching_label_surfaces_for_a_related_hypothesis(db):
    register_target(db, _rule(), description="customer becomes non-performing",
                    registered_by="a")
    hits = search_targets(db, entity="customer",
                          hypothesis="which customers will become non-performing")
    assert [h["name"] for h in hits] == ["tgt_npe_90d"]
    assert "performing" in hits[0]["match_terms"]


def test_search_is_scoped_to_the_entity(db):
    """A customer label must not surface for an account hypothesis — the grain differs, so it is
    not reusable however similar the words."""
    register_target(db, _rule(), description="non-performing", registered_by="a")
    assert search_targets(db, entity="account", hypothesis="non-performing") == []


def test_an_unrelated_hypothesis_matches_nothing(db):
    register_target(db, _rule(), description="non-performing", registered_by="a")
    assert search_targets(db, entity="customer", hypothesis="which payments settle late") == []


def test_an_empty_registry_returns_empty_rather_than_failing(db):
    """The FIRST person to define a label for an entity is the common case on a new deployment —
    an ordinary empty result, not an error path."""
    assert search_targets(db, entity="customer", hypothesis="anything") == []


def test_a_proposal_differing_only_in_its_WINDOW_is_named_as_a_near_duplicate(db):
    """The twin case. Content-addressing cannot catch it — the hashes differ, legitimately — so it
    must be SAID before the person submits, or the registry fills with near-identical labels."""
    register_target(db, _rule(), description="d", registered_by="a")
    twins = near_duplicates(db, _rule(name="tgt_npe_60d", window=60))
    assert [(t["name"], t["differs_in"]) for t in twins] == [("tgt_npe_90d", ("window_days",))]


def test_a_twin_is_found_even_though_tuples_round_trip_as_LISTS(db):
    """The bug this guards: `canonical_target` yields tuples, the stored jsonb yields lists, and
    `("Performing",) != ["Performing"]`. Comparing them directly disables twin detection entirely
    — silently, while a feature claims to prevent twins."""
    register_target(db, _rule(), description="d", registered_by="a")
    stored = targets_for_entity(db, "customer")[0]["rule"]
    assert isinstance(stored["from_values"], list), "the round trip really does change the type"
    assert near_duplicates(db, _rule(name="tgt_npe_60d", window=60)) != []


def test_a_genuinely_different_rule_is_not_a_near_duplicate(db):
    """Only fields that make two rules the SAME QUESTION asked slightly differently count. A
    different watched column is a different label, not a twin."""
    register_target(db, _rule(), description="d", registered_by="a")
    other = replace(_rule(name="tgt_susp_90d"),
                    column_ref="public.bo_cib_customer.cust_susp_flg")
    assert near_duplicates(db, other) == []
