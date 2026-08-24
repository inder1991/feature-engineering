"""S1C-2 — ``wave1_report``: the wave-1 quality report over the governed observation ledger.

Every metric is tested over rows the REAL store writers seeded, and the pins the brief names are
here by name:

* the authority-floor denominator pin — 2 met + 1 unmet + 5 unevaluated is a pass rate of 2/3,
  never 2/8 ("% met" must never use all-rows as its denominator);
* the legacy-sentinel pin — a ``planning_request_hash = 'legacy_template'`` row buckets under its
  own key and never inflates ``recipe_v2``'s bucket (the S1B-4 re-review's aggregation caveat,
  resolved by construction);
* the not-computable section pinned EXHAUSTIVE — the exact set of metric names, so adding or
  removing one is a deliberate test edit, and fan-out-risk sits IN it (1120 persists no segment
  cardinalities — verified against the schema, not the plan's optimism);
* corpus-error isolation — a corrupt corpus poisons ``corpus_status`` only, never the report.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

import featuregen.overlay.upload.governed_planning_report as report_module
from featuregen.overlay.upload.feature_metadata_snapshot import ensure_generation_run
from featuregen.overlay.upload.governed_observation_store import (
    STALE_REGISTRY,
    claim_telemetry_work,
    complete_telemetry_work,
    enqueue_governed_telemetry,
    record_bridge_demand,
    record_planning_observations,
)
from featuregen.overlay.upload.governed_planning_report import (
    LEGACY_TEMPLATE_PLANNING_REQUEST_HASH,
    wave1_report,
)
from featuregen.overlay.upload.hypothesis_corpus import load_hypothesis_corpus

#: Every section the report carries — the shape a consumer renders, pinned once and reused by the
#: isolation tests so "every other section still renders" is an exact statement.
EXPECTED_SECTIONS = frozenset({
    "as_of",
    "resolution_by_domain",
    "origin_coverage",
    "hop_distribution",
    "authority_floor",
    "bridge_demand",
    "refusal_taxonomy",
    "param_divergence_rate",
    "volumes",
    "worker_latency",
    "corpus_status",
    "review_activity",
    "not_computable_in_stage_1",
})

#: The EXHAUSTIVE wave-2 enumeration. A literal, never imported from the module under test —
#: importing it back would pin nothing. Editing this set is the deliberate act the brief demands.
EXPECTED_NOT_COMPUTABLE = frozenset({
    "chooser_accuracy",
    "corpus_expectation_accuracy",
    "fan_out_risk_distribution",
    "incremental_cross_catalog_relevance",
    "per_query_db_percentiles",
    "production_certification_outcomes",
    "sandbox_profiling_outcomes",
    "served_ranking_quality",
    "sme_review_of_served_cards",
})


# ── seeding (real writers; runs minted via ensure_generation_run) ──────────────────────────────


def _run(conn, suffix: str) -> tuple[str, str]:
    intent_id, run_id = f"s1c2_int_{suffix}", f"s1c2_run_{suffix}"
    conn.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
                 "VALUES (%s, 'h', 'hypothesis')", (intent_id,))
    ensure_generation_run(conn, run_id, {}, {}, intent_id=intent_id)
    return intent_id, run_id


def _observation(suffix: str, **overrides) -> dict:
    row = {
        "definition_origin": "recipe_v2",
        "canonical_definition_id": "recipe:rail_txn_count",
        "recipe_id": "rail_txn_count",
        "governed_variant_id": f"gvar_{suffix}",
        "planning_request_hash": "p" * 64,
        "physical_plan_content_hash": "c" * 64,
        "target_entity": "account",
        "anchor_catalog_source": "ops",
        "resolution_status": "resolved",
    }
    row.update(overrides)
    return row


def _seed(conn, suffix: str, *, mode: str = "live", demands=None, **overrides) -> str:
    intent_id, run_id = _run(conn, suffix)
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode=mode,
        rows=[_observation(suffix, **overrides)])[0]
    if demands:
        record_bridge_demand(conn, observation_id=observation_id, rejections=demands)
    return observation_id


def _hop(**overrides) -> dict:
    demand = {
        "verdict": "unsanctioned_bridge",
        "recipe_revision_hash": "r" * 64,
        "relationship_id": "transaction_to_account",
        "relationship_version": "1.0.0",
        "from_entity": "transaction",
        "to_entity": "account",
        "position_catalog": "ops",
        "position_table_ref": "public.transactions",
        "hop_index": 0,
        "realizers": [],
        "near_side_key_refs": [],
    }
    demand.update(overrides)
    return demand


# ── resolution_by_domain ───────────────────────────────────────────────────────────────────────


def test_resolution_by_domain_joins_the_registrys_family_field(conn) -> None:
    """The registry's pack/domain membership field is ``RecipeDefinitionV2.family`` — the report
    joins ``recipe_id`` onto it, buckets llm rows under ``llm_intent``, and never invents a family
    for a recipe id the registry does not carry."""
    _seed(conn, "dom_pay_ok")                                        # payments, resolved
    _seed(conn, "dom_pay_no", resolution_status="unresolved",
          physical_plan_content_hash="unresolved")                   # payments, refused
    _seed(conn, "dom_llm", definition_origin="llm_intent", recipe_id=None,
          canonical_definition_id="intent:cross_catalog_reach")      # llm bucket
    _seed(conn, "dom_ghost", recipe_id="s1c2_not_a_recipe",
          canonical_definition_id="recipe:s1c2_not_a_recipe",
          resolution_status="unresolved", physical_plan_content_hash="unresolved")

    rows = wave1_report(conn)["resolution_by_domain"]
    by_bucket = {row["bucket"]: row for row in rows}
    assert by_bucket["payments"] == {"bucket": "payments", "observations": 2, "resolved": 1,
                                     "resolution_rate": 0.5}
    assert by_bucket["llm_intent"] == {"bucket": "llm_intent", "observations": 1, "resolved": 1,
                                       "resolution_rate": 1.0}
    assert by_bucket["unmapped_recipe"]["observations"] == 1
    assert by_bucket["unmapped_recipe"]["resolved"] == 0
    # deterministic: sorted by bucket key
    assert [row["bucket"] for row in rows] == sorted(row["bucket"] for row in rows)


# ── origin_coverage ────────────────────────────────────────────────────────────────────────────


def test_origin_coverage_is_the_stores_resolution_summary_not_a_copy(conn, monkeypatch) -> None:
    """The per-origin rates are the STORE's derivation, imported — never re-derived SQL. Replacing
    the store function must replace the section's content."""
    monkeypatch.setattr(report_module, "resolution_summary",
                        lambda conn, *, as_of=None: {"sentinel": True})
    assert wave1_report(conn)["origin_coverage"]["sentinel"] is True


def test_a_legacy_template_row_never_inflates_the_recipe_v2_bucket(conn) -> None:
    """The S1B-4 re-review's aggregation caveat, resolved by construction: a row carrying the
    ``legacy_template`` sentinel hash buckets under its own key in the report's split — and in the
    domain section too, where its recipe_id could otherwise collide with a reused V2 id.

    The same sentinel guards the two RECIPE-ORIGIN RATES: a live legacy row wears ``recipe_v2``
    as its least-bad origin, so without the guard it would dilute the param-divergence rate's
    denominator and the stale-registry rate's — both are claims about the V2 lane, and the second
    legacy row seeded here (divergence-bearing, stale-status) must land in NEITHER half of
    either."""
    divergence = [{"parameter": "window", "hypothesis_implied": "90d", "primary_value": "30d"}]
    _seed(conn, "origin_v2")
    _seed(conn, "origin_legacy",
          planning_request_hash=LEGACY_TEMPLATE_PLANNING_REQUEST_HASH,
          canonical_definition_id="template:salary_signal", recipe_id="salary_signal",
          resolution_status="resolved")
    # a legacy row that would pollute BOTH rates if the sentinel guard were missing
    _seed(conn, "origin_legacy_stale",
          planning_request_hash=LEGACY_TEMPLATE_PLANNING_REQUEST_HASH,
          canonical_definition_id="template:salary_signal", recipe_id="salary_signal",
          resolution_status=STALE_REGISTRY, physical_plan_content_hash="unresolved",
          param_divergence=divergence)
    _seed(conn, "origin_llm", definition_origin="llm_intent", recipe_id=None,
          canonical_definition_id="intent:x", resolution_status="unresolved",
          physical_plan_content_hash="unresolved")

    report = wave1_report(conn)
    coverage = report["origin_coverage"]
    split = {row["bucket"]: row for row in coverage["by_origin_with_legacy_sentinel"]}
    assert split["recipe_v2"]["observations"] == 1          # the legacy rows are NOT in here
    assert split["legacy_template"] == {"bucket": "legacy_template", "observations": 2,
                                        "resolved": 1, "resolution_rate": 0.5}
    assert split["llm_intent"]["observations"] == 1
    # the store summary rides along verbatim (it fuses by definition_origin — 3 recipe_v2 rows)
    by_origin = {row["definition_origin"]: row for row in coverage["by_origin"]}
    assert by_origin["recipe_v2"]["observations"] == 3

    domains = {row["bucket"]: row for row in report["resolution_by_domain"]}
    assert domains["legacy_template"]["observations"] == 2

    # param-divergence: the legacy divergence row is in NEITHER the numerator NOR the denominator
    divergence_rate = report["param_divergence_rate"]
    assert divergence_rate["recipe_origin_observations"] == 1     # origin_v2 alone
    assert divergence_rate["divergent_recipe_observations"] == 0
    assert divergence_rate["rate"] == 0.0

    # stale-registry: the legacy stale row is in NEITHER half either
    stale = report["bridge_demand"]["stale_registry"]
    assert stale == {"stale_observations": 0, "recipe_origin_observations": 1,
                     "stale_rate": 0.0}


def test_the_legacy_sentinel_is_gate1s_own_spelling() -> None:
    from featuregen.overlay.upload.contract.gate1 import _LEGACY_TEMPLATE_REQUEST_HASH
    assert LEGACY_TEMPLATE_PLANNING_REQUEST_HASH == _LEGACY_TEMPLATE_REQUEST_HASH


# ── hop_distribution ───────────────────────────────────────────────────────────────────────────


def test_hop_distribution_splits_resolved_from_refused(conn) -> None:
    _seed(conn, "hop_a", hop_count=0)
    _seed(conn, "hop_b", hop_count=2)
    _seed(conn, "hop_c", hop_count=2, resolution_status="unsanctioned_bridge",
          physical_plan_content_hash="unresolved")

    assert wave1_report(conn)["hop_distribution"] == [
        {"hop_count": 0, "observations": 1, "resolved": 1, "refused": 0},
        {"hop_count": 2, "observations": 2, "resolved": 1, "refused": 1},
    ]


# ── authority_floor: THE denominator pin ───────────────────────────────────────────────────────


def test_authority_floor_pass_rate_uses_met_plus_unmet_only(conn) -> None:
    """2 met + 1 unmet + 5 unevaluated (+1 blank) -> 2/3. NEVER 2/8 or 2/9: unevaluated rows are
    absence of a measurement, and counting them in the denominator would report the measuring
    backlog as governance failure."""
    intent_id, run_id = _run(conn, "floor")
    statuses = ["met", "met", "unmet"] + ["unevaluated"] * 5 + [""]
    record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(f"floor_{i}", authority_floor_status=status)
              for i, status in enumerate(statuses)])

    floor = wave1_report(conn)["authority_floor"]
    assert (floor["met"], floor["unmet"], floor["unevaluated"], floor["blank"]) == (2, 1, 5, 1)
    assert floor["denominator"] == 3
    assert floor["pass_rate"] == pytest.approx(2 / 3)


# ── bridge_demand ──────────────────────────────────────────────────────────────────────────────


def test_bridge_demand_counts_queues_and_distinct_identities(conn) -> None:
    _seed(conn, "bd_one", resolution_status="unsanctioned_bridge",
          physical_plan_content_hash="unresolved",
          demands=[_hop(),
                   _hop(verdict="missing_realization"),
                   {"verdict": "bounded_out_max_frontier_states",
                    "recipe_revision_hash": "r" * 64, "anchor_catalog_source": "ops"}])
    # the SAME hop demanded from a second observation: 2 rows, 1 identity
    _seed(conn, "bd_two", resolution_status="unsanctioned_bridge",
          physical_plan_content_hash="unresolved", demands=[_hop()])

    section = wave1_report(conn)["bridge_demand"]
    assert section["queues"]["bridge_demand"] == {"demand_rows": 2,
                                                  "distinct_demand_identities": 1}
    assert section["queues"]["realization_gap"] == {"demand_rows": 1,
                                                    "distinct_demand_identities": 1}
    assert section["queues"]["planner_capacity"] == {"demand_rows": 1,
                                                     "distinct_demand_identities": 1}


def test_the_stale_registry_count_is_its_own_line(conn) -> None:
    """``stale_registry`` is the telemetry worker's ADAPTER-level refusal — the registry moved
    under the frozen work item. Its rate divides by recipe-origin rows, the only lane that can
    go stale — and the numerator carries the SAME origin filter, so an llm-origin row that
    somehow wore the status could never push the rate past 1."""
    _seed(conn, "stale_a", mode="telemetry", resolution_status=STALE_REGISTRY,
          physical_plan_content_hash="unresolved")
    _seed(conn, "stale_b")
    _seed(conn, "stale_c", definition_origin="llm_intent", recipe_id=None,
          canonical_definition_id="intent:x")
    # an llm-origin row wearing the status counts in NEITHER half of the rate
    _seed(conn, "stale_llm", definition_origin="llm_intent", recipe_id=None,
          canonical_definition_id="intent:y", resolution_status=STALE_REGISTRY,
          physical_plan_content_hash="unresolved")

    stale = wave1_report(conn)["bridge_demand"]["stale_registry"]
    assert stale == {"stale_observations": 1, "recipe_origin_observations": 2,
                     "stale_rate": 0.5}


# ── refusal_taxonomy ───────────────────────────────────────────────────────────────────────────


def test_refusal_taxonomy_counts_codes_over_refused_rows_only(conn) -> None:
    _seed(conn, "tax_a", resolution_status="unsanctioned_bridge",
          physical_plan_content_hash="unresolved",
          reason_codes=["unsanctioned_bridge", "missing_realization"])
    _seed(conn, "tax_b", resolution_status="bounded_out",
          physical_plan_content_hash="unresolved", reason_codes=["unsanctioned_bridge"])
    # a RESOLVED row's codes never count as refusals
    _seed(conn, "tax_c", reason_codes=["selected_best_single_catalog"])

    taxonomy = wave1_report(conn)["refusal_taxonomy"]
    assert taxonomy["top"] == [
        {"reason_code": "unsanctioned_bridge", "occurrences": 2},
        {"reason_code": "missing_realization", "occurrences": 1},
    ]
    assert taxonomy["distinct_reason_codes"] == 2
    assert taxonomy["tail"] == {"reason_codes": 0, "occurrences": 0}


def test_refusal_taxonomy_tail_carries_what_top_n_cut(conn, monkeypatch) -> None:
    monkeypatch.setattr(report_module, "REFUSAL_TAXONOMY_TOP_N", 1)
    _seed(conn, "tail_a", resolution_status="unresolved",
          physical_plan_content_hash="unresolved",
          reason_codes=["unsanctioned_bridge", "missing_realization"])
    _seed(conn, "tail_b", resolution_status="unresolved",
          physical_plan_content_hash="unresolved", reason_codes=["unsanctioned_bridge"])

    taxonomy = wave1_report(conn)["refusal_taxonomy"]
    assert taxonomy["top"] == [{"reason_code": "unsanctioned_bridge", "occurrences": 2}]
    assert taxonomy["distinct_reason_codes"] == 2
    assert taxonomy["tail"] == {"reason_codes": 1, "occurrences": 1}


# ── param_divergence_rate ──────────────────────────────────────────────────────────────────────


def test_param_divergence_rate_over_recipe_origin_rows(conn) -> None:
    divergence = [{"parameter": "window", "hypothesis_implied": "90d", "primary_value": "30d"}]
    _seed(conn, "div_a", param_divergence=divergence)
    _seed(conn, "div_b")
    _seed(conn, "div_c")
    _seed(conn, "div_llm", definition_origin="llm_intent", recipe_id=None,
          canonical_definition_id="intent:x", param_divergence=divergence)

    section = wave1_report(conn)["param_divergence_rate"]
    assert section["recipe_origin_observations"] == 3
    assert section["divergent_recipe_observations"] == 1
    assert section["rate"] == pytest.approx(1 / 3)
    # an llm divergence is SHOWN, never folded into the recipe-origin rate
    assert section["divergent_llm_observations"] == 1
    assert section["parameter_frequency"] == [{"parameter": "window", "occurrences": 2}]


# ── volumes ────────────────────────────────────────────────────────────────────────────────────


def test_volumes_count_modes_runs_intents_and_the_outbox(conn) -> None:
    _seed(conn, "vol_a")
    _seed(conn, "vol_b")
    intent_id, run_id = _run(conn, "vol_tele")
    record_planning_observations(conn, generation_run_id=run_id, intent_id=intent_id,
                                 observation_mode="telemetry", rows=[_observation("vol_tele")])

    retry_item = enqueue_governed_telemetry(conn, generation_run_id=run_id,
                                            intent_id=intent_id, frozen_inputs={})
    done_item = enqueue_governed_telemetry(conn, generation_run_id=run_id,
                                           intent_id=intent_id, frozen_inputs={})
    queued_item = enqueue_governed_telemetry(conn, generation_run_id=run_id,
                                             intent_id=intent_id, frozen_inputs={})
    # Claims take the OLDEST claimable item by (recorded_at, work_item_id) — but recorded_at
    # defaults to now(), which is TRANSACTION-FIXED in Postgres, so all three enqueues share one
    # timestamp and the ULID tiebreak's random suffix would make the claim order a coin flip.
    # Pin DISTINCT timestamps explicitly (the outbox is the ledger pair's one mutable table) so
    # each item's role is deterministic.
    for offset, work_item_id in enumerate((retry_item, done_item, queued_item)):
        conn.execute("UPDATE governed_telemetry_outbox SET recorded_at = %s "
                     "WHERE work_item_id = %s",
                     (datetime(2026, 8, 20, 0, 0, offset, tzinfo=UTC), work_item_id))
    first = claim_telemetry_work(conn, owner="w1")
    second = claim_telemetry_work(conn, owner="w1")
    assert [first["work_item_id"], second["work_item_id"]] == [retry_item, done_item]
    # a reclaim after lease expiry is a RETRY: attempt_count rises past 1
    conn.execute("UPDATE governed_telemetry_outbox SET lease_expires_at = now() - interval '1s' "
                 "WHERE work_item_id = %s", (retry_item,))
    reclaimed = claim_telemetry_work(conn, owner="w2")
    assert reclaimed["work_item_id"] == retry_item and reclaimed["attempt_count"] == 2
    assert complete_telemetry_work(conn, work_item_id=done_item, owner="w1",
                                   fence=second["lease_fence"], ok=True)

    volumes = wave1_report(conn)["volumes"]
    assert volumes["by_mode"]["live"] == {"observations": 2, "distinct_runs": 2,
                                          "distinct_intents": 2}
    assert volumes["by_mode"]["telemetry"] == {"observations": 1, "distinct_runs": 1,
                                               "distinct_intents": 1}
    assert volumes["outbox"] == {"queued": 1, "leased": 1, "done": 1, "failed": 0,
                                 "retried_items": 1}


# ── worker_latency ─────────────────────────────────────────────────────────────────────────────


def _done_outbox_item(conn, run_id: str, intent_id: str, *, seconds: int, owner: str) -> None:
    """One done item whose enqueue→complete distance is EXACT: the outbox is the ledger pair's one
    mutable table, so pinning explicit timestamps is a plain UPDATE, not a trigger fight."""
    work_item_id = enqueue_governed_telemetry(conn, generation_run_id=run_id,
                                              intent_id=intent_id, frozen_inputs={})
    item = claim_telemetry_work(conn, owner=owner)
    assert complete_telemetry_work(conn, work_item_id=item["work_item_id"], owner=owner,
                                   fence=item["lease_fence"], ok=True)
    conn.execute(
        "UPDATE governed_telemetry_outbox SET recorded_at = %s, completed_at = %s "
        "WHERE work_item_id = %s",
        (datetime(2026, 8, 20, tzinfo=UTC),
         datetime(2026, 8, 20, 0, 0, seconds, tzinfo=UTC), work_item_id))


def test_worker_latency_percentiles_over_done_items(conn) -> None:
    intent_id, run_id = _run(conn, "lat")
    for owner, seconds in (("w1", 10), ("w2", 20), ("w3", 30)):
        _done_outbox_item(conn, run_id, intent_id, seconds=seconds, owner=owner)

    latency = wave1_report(conn)["worker_latency"]
    assert latency["done_items"] == 3
    assert latency["includes_queue_wait"] is True
    assert latency["enqueue_to_complete_seconds"]["p50"] == pytest.approx(20.0)
    assert latency["enqueue_to_complete_seconds"]["p95"] == pytest.approx(29.0)


def test_worker_latency_refuses_to_fake_a_percentile(conn) -> None:
    """One done item is a data point, not a distribution. Null with a reason — never p50=p95=the
    only number we have, dressed as a percentile."""
    intent_id, run_id = _run(conn, "lat_one")
    _done_outbox_item(conn, run_id, intent_id, seconds=10, owner="w1")

    latency = wave1_report(conn)["worker_latency"]
    assert latency["enqueue_to_complete_seconds"] is None
    assert latency["done_items"] == 1
    assert "fewer than 2" in latency["reason"]


# ── corpus_status ──────────────────────────────────────────────────────────────────────────────


def test_corpus_status_counts_the_real_corpus(conn) -> None:
    load_hypothesis_corpus.cache_clear()
    entries = load_hypothesis_corpus()

    status = wave1_report(conn)["corpus_status"]
    assert status["entries"] == len(entries)
    expected_domains: dict[str, int] = {}
    for entry in entries:
        expected_domains[entry.banking_domain] = expected_domains.get(entry.banking_domain, 0) + 1
    assert status["by_domain"] == dict(sorted(expected_domains.items()))
    assert status["by_review_status"] == {"draft": len(entries)}   # the packaged file is draft-only
    assert status["expects_cross_catalog"] == sum(
        1 for entry in entries if entry.expects_cross_catalog)


def test_a_corrupt_corpus_poisons_only_its_own_section(conn, monkeypatch) -> None:
    def _boom():
        raise ValueError("corpus is broken")
    monkeypatch.setattr(report_module, "load_hypothesis_corpus", _boom)

    report = wave1_report(conn)
    assert report["corpus_status"] == {"error": "corpus is broken"}
    assert set(report) == EXPECTED_SECTIONS                          # everything else rendered


# ── review_activity ────────────────────────────────────────────────────────────────────────────


def test_review_activity_counts_by_decision(conn) -> None:
    observation_id = _seed(conn, "review")
    for i, decision in enumerate(("approved", "rejected", "rejected")):
        conn.execute(
            "INSERT INTO governed_plan_review_event "
            "(event_id, observation_id, reviewer, reviewer_role, decision) "
            "VALUES (%s, %s, 'sme', 'domain-sme', %s)",
            (f"s1c2_rev_{i}", observation_id, decision))

    assert wave1_report(conn)["review_activity"] == {
        "approved": 1, "changes_required": 0, "rejected": 2, "events": 3}


# ── not_computable_in_stage_1: pinned exhaustive ───────────────────────────────────────────────


def test_the_not_computable_section_is_exhaustive_and_deliberate(conn) -> None:
    section = wave1_report(conn)["not_computable_in_stage_1"]
    assert {entry["metric"] for entry in section} == EXPECTED_NOT_COMPUTABLE
    assert [entry["metric"] for entry in section] == sorted(entry["metric"] for entry in section)
    assert all(entry["missing_evidence"].strip() for entry in section)
    assert all(set(entry) == {"metric", "missing_evidence"} for entry in section)


def test_fan_out_risk_names_the_absent_column_not_the_plans_optimism(conn) -> None:
    """VERIFIED against 1120 itself: ``governed_planning_observation`` persists NO segment
    cardinalities (they live in plan evidence the schema never carries), so the fan-out-risk
    distribution is honestly not computable — the report says which evidence is missing rather
    than faking a distribution out of hop_count."""
    (entry,) = [row for row in wave1_report(conn)["not_computable_in_stage_1"]
                if row["metric"] == "fan_out_risk_distribution"]
    assert "segment cardinalit" in entry["missing_evidence"]
    assert "governed_planning_observation" in entry["missing_evidence"]


def test_chooser_accuracy_names_the_true_gap_not_a_landed_milestone(conn) -> None:
    """S1C-3 LANDED (the shadow chooser ships in ``param_choice.py`` and rides the telemetry
    worker), so "until S1C-3 lands" stopped being the missing evidence. The honest gap is
    twofold: nothing constructs a real chooser in production yet, and no report section
    aggregates its shadow entries into an accuracy number."""
    (entry,) = [row for row in wave1_report(conn)["not_computable_in_stage_1"]
                if row["metric"] == "chooser_accuracy"]
    assert "until S1C-3 lands" not in entry["missing_evidence"]
    assert "param_chooser" in entry["missing_evidence"]
    assert "aggregation" in entry["missing_evidence"]


# ── as_of: filters every section, echoed in the payload ────────────────────────────────────────


def test_as_of_filters_every_ledger_section_and_is_echoed(conn) -> None:
    _seed(conn, "asof", resolution_status="unsanctioned_bridge",
          physical_plan_content_hash="unresolved", authority_floor_status="met",
          reason_codes=["unsanctioned_bridge"], demands=[_hop()])
    intent_id, run_id = _run(conn, "asof_ob")
    _done_outbox_item(conn, run_id, intent_id, seconds=10, owner="w1")
    conn.execute("UPDATE governed_telemetry_outbox SET recorded_at = now(), completed_at = now()")

    past = datetime(2000, 1, 1, tzinfo=UTC)
    report = wave1_report(conn, as_of=past)
    assert report["as_of"] == past
    assert report["resolution_by_domain"] == []
    assert report["origin_coverage"]["totals"]["observations"] == 0
    assert report["hop_distribution"] == []
    floor = report["authority_floor"]
    assert (floor["met"], floor["denominator"], floor["pass_rate"]) == (0, 0, None)
    assert "reason" in floor
    assert all(queue == {"demand_rows": 0, "distinct_demand_identities": 0}
               for queue in report["bridge_demand"]["queues"].values())
    assert report["bridge_demand"]["stale_registry"]["stale_rate"] is None
    assert report["refusal_taxonomy"]["top"] == []
    assert report["param_divergence_rate"]["rate"] is None
    assert report["volumes"]["by_mode"]["live"]["observations"] == 0
    assert report["volumes"]["outbox"]["done"] == 0
    assert report["worker_latency"]["enqueue_to_complete_seconds"] is None
    assert report["review_activity"]["events"] == 0
