"""S1C-2 — the wave-1 cross-catalog quality report: every number real, every gap named.

One read-only aggregation, :func:`wave1_report`, over the governed observation ledger (migrations
1120 + 1121: ``governed_planning_observation`` / ``bridge_demand_observation`` /
``governed_telemetry_outbox`` / ``governed_plan_review_event``) plus the S1C-1 hypothesis corpus.
It is the evidence surface Stage-2 entry reads, and it holds two disciplines everywhere:

* **Reuse, never re-derive.** The per-origin resolution rates are the store's own
  ``resolution_summary``, imported — a second copy of that SQL would drift into a second truth.
  Only aggregations the store does NOT own live here.
* **Never fake a number.** Every rate ships beside its denominator; a rate whose denominator is
  zero is ``None``, not ``0.0``; a percentile over fewer than two points is ``None`` with a
  reason; and ``not_computable_in_stage_1`` enumerates every wave-2 metric with the evidence it
  lacks — including fan-out-risk, which the plan hoped to compute and the schema honestly cannot
  (see :data:`NOT_COMPUTABLE_IN_STAGE_1`).

Determinism: every data-driven list is sorted (by key, or by count descending then key for
frequency lists); ``as_of`` filters ``recorded_at <= as_of`` in every ledger section and is echoed
in the payload.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from featuregen.contracts import DbConn
from featuregen.overlay.upload.governed_observation_store import (
    RESOLVED_STATUSES,
    STALE_REGISTRY,
    resolution_summary,
)
from featuregen.overlay.upload.hypothesis_corpus import load_hypothesis_corpus
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

#: gate1's live-lane sentinel for the legacy Template branch (``_LEGACY_TEMPLATE_REQUEST_HASH``),
#: spelled here for the read side and pinned equal by test. Rows carrying it are bucketed under
#: their own key and NEVER fused into ``recipe_v2`` — the S1B-4 re-review's aggregation caveat,
#: resolved by construction (the lane is route-dead today; correct anyway).
LEGACY_TEMPLATE_PLANNING_REQUEST_HASH = "legacy_template"

#: The domain bucket for a recipe-origin row whose ``recipe_id`` the V2 registry does not carry
#: (or that carries none at all). An honest bucket, never an invented family.
UNMAPPED_RECIPE_BUCKET = "unmapped_recipe"

#: How many reason codes ``refusal_taxonomy.top`` shows; the tail is counted, never dropped.
REFUSAL_TAXONOMY_TOP_N = 10

_QUEUE_NAMES = ("bridge_demand", "planner_capacity", "realization_gap")
_OUTBOX_STATUSES = ("queued", "leased", "done", "failed")
_REVIEW_DECISIONS = ("approved", "changes_required", "rejected")
_FLOOR_STATUSES = ("met", "unmet", "unevaluated")

#: The wave-2 metrics this report REFUSES to fake, each naming the evidence it lacks. Fan-out-risk
#: is in here by VERIFICATION, not by plan: 1120's ``governed_planning_observation`` was read
#: column by column — it persists hop/bridge counts and scope material, but no segment
#: cardinalities (those live in plan evidence the observation schema never carries), and a
#: distribution computed without them would be an invention.
NOT_COMPUTABLE_IN_STAGE_1: tuple[dict[str, str], ...] = tuple(sorted((
    {"metric": "served_ranking_quality",
     "missing_evidence": "nothing is served in Stage 1 — there are no served rankings to score"},
    {"metric": "sme_review_of_served_cards",
     "missing_evidence": "nothing is served in Stage 1 — there are no served cards for an SME "
                         "to review (governed_plan_review_event reviews PLANS, not servings)"},
    {"metric": "incremental_cross_catalog_relevance",
     "missing_evidence": "needs served A/B exposure; Stage 1 serves nothing, so there is no "
                         "exposed cohort to compare"},
    {"metric": "corpus_expectation_accuracy",
     "missing_evidence": "needs an evaluation harness that PLANS the corpus hypotheses — "
                         "observations are not corpus-keyed (no observation column joins to "
                         "corpus_id), so expectation vs outcome cannot be paired"},
    {"metric": "chooser_accuracy",
     "missing_evidence": "no chooser shadow rows exist until S1C-3 lands and its shadow "
                         "decisions accrue"},
    {"metric": "per_query_db_percentiles",
     "missing_evidence": "per-query DB timings are never persisted; the only persisted "
                         "timestamps are the outbox's enqueue/complete pair"},
    {"metric": "sandbox_profiling_outcomes",
     "missing_evidence": "the four-way validation vocabulary — Stage 1 produces no "
                         "sandbox-profiling validation claims to count"},
    {"metric": "production_certification_outcomes",
     "missing_evidence": "the four-way validation vocabulary — Stage 1 produces no "
                         "production-certification validation claims to count"},
    {"metric": "fan_out_risk_distribution",
     "missing_evidence": "segment cardinalities live in plan evidence, and "
                         "governed_planning_observation (1120) persists no segment-cardinality "
                         "column — hop_count/bridge_count carry reach, not cardinality, and a "
                         "distribution built from them would be fabricated"},
), key=lambda entry: entry["metric"]))


def wave1_report(conn: DbConn, *, as_of: datetime | None = None) -> dict:
    """The wave-1 offline quality report — read-only SQL over the observation ledger plus the
    corpus loader. Writes nothing; every section filters ``recorded_at <= as_of``."""
    resolved_as_of = as_of if as_of is not None else conn.execute("SELECT now()").fetchone()[0]
    return {
        "as_of": resolved_as_of,
        "resolution_by_domain": _resolution_by_domain(conn, resolved_as_of),
        "origin_coverage": _origin_coverage(conn, resolved_as_of),
        "hop_distribution": _hop_distribution(conn, resolved_as_of),
        "authority_floor": _authority_floor(conn, resolved_as_of),
        "bridge_demand": _bridge_demand(conn, resolved_as_of),
        "refusal_taxonomy": _refusal_taxonomy(conn, resolved_as_of),
        "param_divergence_rate": _param_divergence(conn, resolved_as_of),
        "volumes": _volumes(conn, resolved_as_of),
        "worker_latency": _worker_latency(conn, resolved_as_of),
        "corpus_status": _corpus_status(),
        "review_activity": _review_activity(conn, resolved_as_of),
        "not_computable_in_stage_1": [dict(entry) for entry in NOT_COMPUTABLE_IN_STAGE_1],
    }


# ── sections ───────────────────────────────────────────────────────────────────────────────────


def _resolution_by_domain(conn: DbConn, as_of: datetime) -> list[dict]:
    """Resolution per pack — the registry's membership field is ``RecipeDefinitionV2.family``,
    joined in-process via ``v2_recipe_by_id`` (the registry is code, not a table).

    Bucket precedence: the legacy sentinel first (its recipe_id may collide with a reused V2 id,
    and attributing a V1 planning row to a V2 pack would be the fusion the split exists to
    prevent), then ``llm_intent``, then the recipe's family, then ``unmapped_recipe``.
    """
    buckets: dict[str, dict[str, int]] = {}
    for origin, request_hash, recipe_id, observations, resolved in conn.execute(
            "SELECT definition_origin, planning_request_hash, COALESCE(recipe_id, ''),"
            "       count(*), count(*) FILTER (WHERE resolution_status = ANY(%s)) "
            "  FROM governed_planning_observation WHERE recorded_at <= %s "
            " GROUP BY 1, 2, 3", (sorted(RESOLVED_STATUSES), as_of)).fetchall():
        if request_hash == LEGACY_TEMPLATE_PLANNING_REQUEST_HASH:
            bucket = "legacy_template"
        elif origin == "llm_intent":
            bucket = "llm_intent"
        else:
            recipe = v2_recipe_by_id(recipe_id) if recipe_id else None
            bucket = recipe.family if recipe is not None else UNMAPPED_RECIPE_BUCKET
        counts = buckets.setdefault(bucket, {"observations": 0, "resolved": 0})
        counts["observations"] += observations
        counts["resolved"] += resolved
    return [{"bucket": bucket, "observations": counts["observations"],
             "resolved": counts["resolved"],
             "resolution_rate": _rate(counts["resolved"], counts["observations"])}
            for bucket, counts in sorted(buckets.items())]


def _origin_coverage(conn: DbConn, as_of: datetime) -> dict:
    """The store's ``resolution_summary`` VERBATIM, plus the legacy-sentinel split the summary's
    origin grouping cannot express: rows carrying the ``legacy_template`` hash bucket separately,
    never fused into ``recipe_v2``."""
    split = [
        {"bucket": bucket, "observations": observations, "resolved": resolved,
         "resolution_rate": _rate(resolved, observations)}
        for bucket, observations, resolved in conn.execute(
            "SELECT CASE WHEN planning_request_hash = %s THEN 'legacy_template' "
            "            ELSE definition_origin END,"
            "       count(*), count(*) FILTER (WHERE resolution_status = ANY(%s)) "
            "  FROM governed_planning_observation WHERE recorded_at <= %s "
            " GROUP BY 1 ORDER BY 1",
            (LEGACY_TEMPLATE_PLANNING_REQUEST_HASH, sorted(RESOLVED_STATUSES),
             as_of)).fetchall()]
    return {**resolution_summary(conn, as_of=as_of), "by_origin_with_legacy_sentinel": split}


def _hop_distribution(conn: DbConn, as_of: datetime) -> list[dict]:
    """Observations per ``hop_count``, split resolved/refused. ``refused`` is observations minus
    STRICTLY resolved: ``resolved_with_ambiguity`` and ``partially_resolved`` count as refused,
    per the store's ``RESOLVED_STATUSES`` ruling ("resolution rate" never quietly means "produced
    something")."""
    return [
        {"hop_count": hop_count, "observations": observations, "resolved": resolved,
         "refused": observations - resolved}
        for hop_count, observations, resolved in conn.execute(
            "SELECT hop_count, count(*),"
            "       count(*) FILTER (WHERE resolution_status = ANY(%s)) "
            "  FROM governed_planning_observation WHERE recorded_at <= %s "
            " GROUP BY 1 ORDER BY 1", (sorted(RESOLVED_STATUSES), as_of)).fetchall()]


def _authority_floor(conn: DbConn, as_of: datetime) -> dict:
    """Pass rate over ``met + unmet`` ONLY — the ruling: "% met" must never use all-rows as its
    denominator, because ``unevaluated`` and ``''`` are absence of a measurement, not failures.
    Both are counted and shown; anything outside the vocabulary lands in ``other`` rather than
    vanishing."""
    counts = {status: 0 for status in _FLOOR_STATUSES}
    blank = 0
    other: dict[str, int] = {}
    for status, observations in conn.execute(
            "SELECT authority_floor_status, count(*) "
            "  FROM governed_planning_observation WHERE recorded_at <= %s "
            " GROUP BY 1 ORDER BY 1", (as_of,)).fetchall():
        if status in counts:
            counts[status] = observations
        elif status == "":
            blank = observations
        else:
            other[status] = observations
    denominator = counts["met"] + counts["unmet"]
    section: dict[str, Any] = {
        **counts, "blank": blank, "other": dict(sorted(other.items())),
        "denominator": denominator,
        "pass_rate": _rate(counts["met"], denominator),
    }
    if section["pass_rate"] is None:
        section["reason"] = "no evaluated rows: met + unmet = 0, and 0/0 is not a rate"
    return section


def _bridge_demand(conn: DbConn, as_of: datetime) -> dict:
    """Per-queue totals and distinct demand identities, plus the ``stale_registry`` count as its
    own line. Stale rate divides by RECIPE-origin rows — only the recipe lane can find the
    registry moved under a frozen work item — and the numerator carries the SAME origin filter,
    so rate <= 1 holds by construction, not by adjacency."""
    queues = {name: {"demand_rows": 0, "distinct_demand_identities": 0}
              for name in _QUEUE_NAMES}
    for queue, demand_rows, identities in conn.execute(
            "SELECT demand_queue, count(*), count(DISTINCT demand_identity_hash) "
            "  FROM bridge_demand_observation WHERE recorded_at <= %s "
            " GROUP BY 1 ORDER BY 1", (as_of,)).fetchall():
        queues[queue] = {"demand_rows": demand_rows, "distinct_demand_identities": identities}
    stale, recipe_origin = conn.execute(
        "SELECT count(*) FILTER (WHERE resolution_status = %s"
        "                          AND definition_origin = 'recipe_v2'),"
        "       count(*) FILTER (WHERE definition_origin = 'recipe_v2') "
        "  FROM governed_planning_observation WHERE recorded_at <= %s",
        (STALE_REGISTRY, as_of)).fetchone()
    return {
        "queues": queues,
        "stale_registry": {"stale_observations": stale,
                           "recipe_origin_observations": recipe_origin,
                           "stale_rate": _rate(stale, recipe_origin)},
    }


def _refusal_taxonomy(conn: DbConn, as_of: datetime) -> dict:
    """Reason-code frequency over REFUSED observations (anything outside the store's
    ``RESOLVED_STATUSES``): top-N by count, with the full tail counted rather than dropped."""
    rows = conn.execute(
        "SELECT code, count(*) "
        "  FROM governed_planning_observation,"
        "       LATERAL jsonb_array_elements_text(reason_codes) AS code "
        " WHERE recorded_at <= %s AND resolution_status <> ALL(%s) "
        " GROUP BY 1 ORDER BY 2 DESC, 1", (as_of, sorted(RESOLVED_STATUSES))).fetchall()
    top = rows[:REFUSAL_TAXONOMY_TOP_N]
    tail = rows[REFUSAL_TAXONOMY_TOP_N:]
    return {
        "top": [{"reason_code": code, "occurrences": occurrences}
                for code, occurrences in top],
        "distinct_reason_codes": len(rows),
        "tail": {"reason_codes": len(tail),
                 "occurrences": sum(occurrences for _code, occurrences in tail)},
    }


def _param_divergence(conn: DbConn, as_of: datetime) -> dict:
    """Divergent recipe-origin rows over recipe-origin rows. An llm-origin divergence is SHOWN
    beside the rate, never folded into it — mixing lanes would let one inflate the other's
    numerator past its denominator."""
    recipe_origin, divergent_recipe, divergent_llm = conn.execute(
        "SELECT count(*) FILTER (WHERE definition_origin = 'recipe_v2'),"
        "       count(*) FILTER (WHERE definition_origin = 'recipe_v2'"
        "                          AND jsonb_array_length(param_divergence) > 0),"
        "       count(*) FILTER (WHERE definition_origin = 'llm_intent'"
        "                          AND jsonb_array_length(param_divergence) > 0) "
        "  FROM governed_planning_observation WHERE recorded_at <= %s", (as_of,)).fetchone()
    frequency = conn.execute(
        "SELECT COALESCE(entry->>'parameter', ''), count(*) "
        "  FROM governed_planning_observation,"
        "       LATERAL jsonb_array_elements(param_divergence) AS entry "
        " WHERE recorded_at <= %s GROUP BY 1 ORDER BY 2 DESC, 1", (as_of,)).fetchall()
    return {
        "recipe_origin_observations": recipe_origin,
        "divergent_recipe_observations": divergent_recipe,
        "rate": _rate(divergent_recipe, recipe_origin),
        "divergent_llm_observations": divergent_llm,
        "parameter_frequency": [{"parameter": parameter, "occurrences": occurrences}
                                for parameter, occurrences in frequency],
    }


def _volumes(conn: DbConn, as_of: datetime) -> dict:
    by_mode = {mode: {"observations": 0, "distinct_runs": 0, "distinct_intents": 0}
               for mode in ("live", "telemetry")}
    for mode, observations, runs, intents in conn.execute(
            "SELECT observation_mode, count(*), count(DISTINCT generation_run_id),"
            "       count(DISTINCT intent_id) "
            "  FROM governed_planning_observation WHERE recorded_at <= %s "
            " GROUP BY 1 ORDER BY 1", (as_of,)).fetchall():
        by_mode[mode] = {"observations": observations, "distinct_runs": runs,
                         "distinct_intents": intents}
    outbox: dict[str, int] = {status: 0 for status in _OUTBOX_STATUSES}
    for status, items in conn.execute(
            "SELECT status, count(*) FROM governed_telemetry_outbox "
            " WHERE recorded_at <= %s GROUP BY 1 ORDER BY 1", (as_of,)).fetchall():
        outbox[status] = items
    outbox["retried_items"] = conn.execute(
        "SELECT count(*) FROM governed_telemetry_outbox "
        " WHERE recorded_at <= %s AND attempt_count > 1", (as_of,)).fetchone()[0]
    return {"by_mode": by_mode, "outbox": outbox}


def _worker_latency(conn: DbConn, as_of: datetime) -> dict:
    """p50/p95 of ``completed_at - recorded_at`` over DONE outbox items. That pair measures
    enqueue-to-complete — queue wait INCLUDED — which is why the label says so and why
    ``includes_queue_wait`` is stated rather than implied. Fewer than two done items is a point,
    not a distribution: ``None`` with a reason, never a fake percentile."""
    done_items, p50, p95 = conn.execute(
        "SELECT count(*),"
        "       percentile_cont(0.5) WITHIN GROUP (ORDER BY"
        "           EXTRACT(EPOCH FROM (completed_at - recorded_at))),"
        "       percentile_cont(0.95) WITHIN GROUP (ORDER BY"
        "           EXTRACT(EPOCH FROM (completed_at - recorded_at))) "
        "  FROM governed_telemetry_outbox "
        " WHERE status = 'done' AND completed_at IS NOT NULL AND recorded_at <= %s",
        (as_of,)).fetchone()
    section: dict[str, Any] = {"done_items": done_items, "includes_queue_wait": True}
    if done_items < 2:
        section["enqueue_to_complete_seconds"] = None
        section["reason"] = (f"fewer than 2 done outbox items ({done_items}) — a percentile over "
                             "0 or 1 points is a fabrication")
    else:
        section["enqueue_to_complete_seconds"] = {"p50": float(p50), "p95": float(p95)}
    return section


def _corpus_status() -> dict:
    """Via the S1C-1 loader. A corrupt corpus poisons THIS section only (``{"error": ...}``) —
    the ledger sections are DB facts and still render (no savepoint needed: nothing here writes)."""
    try:
        entries = load_hypothesis_corpus()
    except Exception as exc:  # noqa: BLE001 — the loader's refusals are the section's content
        return {"error": str(exc)}
    by_domain: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for entry in entries:
        by_domain[entry.banking_domain] = by_domain.get(entry.banking_domain, 0) + 1
        by_status[entry.review_status] = by_status.get(entry.review_status, 0) + 1
    return {
        "entries": len(entries),
        "by_domain": dict(sorted(by_domain.items())),
        "by_review_status": dict(sorted(by_status.items())),
        "expects_cross_catalog": sum(1 for entry in entries if entry.expects_cross_catalog),
    }


def _review_activity(conn: DbConn, as_of: datetime) -> dict:
    """SME judgement counts by decision — zero live today; the section exists so the day the first
    review lands, the report already has a place to show it."""
    section = {decision: 0 for decision in _REVIEW_DECISIONS}
    for decision, events in conn.execute(
            "SELECT decision, count(*) FROM governed_plan_review_event "
            " WHERE recorded_at <= %s GROUP BY 1 ORDER BY 1", (as_of,)).fetchall():
        section[decision] = events
    section["events"] = sum(section[decision] for decision in _REVIEW_DECISIONS)
    return section


def _rate(numerator: int, denominator: int) -> float | None:
    """``None`` when the denominator is zero: 0/0 reported as 0.0 would read as a measured zero,
    and this report never fakes a number."""
    return (numerator / denominator) if denominator else None


__all__ = [
    "LEGACY_TEMPLATE_PLANNING_REQUEST_HASH",
    "NOT_COMPUTABLE_IN_STAGE_1",
    "REFUSAL_TAXONOMY_TOP_N",
    "UNMAPPED_RECIPE_BUCKET",
    "wave1_report",
]
