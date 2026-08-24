# Cross-catalog Stage 1 — acceptance thresholds

**Status: DRAFT — awaiting SME signature.**

These numbers — not vibes — are Stage-2 entry evidence. Until the signatory block below is
filled in, **these thresholds gate nothing**: an unsigned draft is a proposal, and a Stage-2
entry argument that cites an unsigned threshold is citing nobody's judgement. Every number in
the table is a proposed DRAFT value; the SME may replace any of them at signature time.

Companion: the operational side (migrations, the flag, the worker, the report queries) is
[`cross-catalog-stage1-runbook.md`](cross-catalog-stage1-runbook.md).

## Signatory

The signing role is the **SME reviewer** — the role whose plan judgements are recorded as
`governed_plan_review_event` rows (migration `1120`: `reviewer`, `reviewer_role`, `decision` ∈
`approved | changes_required | rejected`, append-only) and whose corpus promotions
(`draft → reviewed`) the hypothesis-corpus module defines as an operator act recorded in that
same event stream (`src/featuregen/overlay/upload/hypothesis_corpus.py`). Note honestly: as of
this draft **no production code path writes those rows yet** — only tests do — so recording the
first real review is itself an operator act the runbook's explicit-go table covers.

| | |
|---|---|
| Signed by (name) | _________________________ |
| Role | SME reviewer (`reviewer_role` as it will appear in `governed_plan_review_event`) |
| Date | _________________________ |
| Thresholds amended at signature? | yes / no — amendments listed below the table |

## Thresholds

Every "measured by" entry is the **exact section key** of the wave-1 report payload
(`wave1_report` in `src/featuregen/overlay/upload/governed_planning_report.py`, served at
`GET /governance/cross-catalog-report`). The report's **own** sections ship rates beside their
denominators and return `None` — never `0.0` — on a zero denominator. One exception, worth
knowing before the first look: `origin_coverage` embeds the store's `resolution_summary`
verbatim, and the store's rate helper reports **`0.0`** on a zero denominator
(`governed_observation_store.py`) — so `origin_coverage.totals.resolution_rate` reads `0.0`
over an empty ledger, which is exactly the pre-flag-flip state the first operator look hits.

| Metric | Proposed threshold (DRAFT) | Measured by (report section key) | Rationale |
|---|---|---|---|
| Minimum resolution rate per domain | ≥ 0.60 per bucket, over ≥ 20 observations in that bucket | `resolution_by_domain` (bucket = the V2 registry's recipe `family`, plus the `llm_intent`, `legacy_template` and `unmapped_recipe` buckets); cross-checked against `origin_coverage.by_origin` | Below 0.6 the governed planner refuses more than it plans for that pack, and serving it in Stage 2 would surface mostly refusals. 0.6 is a floor to clear, not a target. |
| Maximum stale-registry ("stale bridge") rate | ≤ 0.05, over ≥ 20 recipe-origin observations | `bridge_demand.stale_registry.stale_rate` (numerator and denominator are both recipe-origin by construction) | A stale row means the registry moved under a frozen work item; above 5% the telemetry is measuring registry churn, not planning quality. |
| Chooser accuracy floor | ≥ 0.70, over ≥ 10 token-evaluable picks | **Not yet a report key** — today it sits in `not_computable_in_stage_1` as `chooser_accuracy`. The raw rows exist: the shadow chooser writes `param_divergence` entries with `"source": "chooser"` carrying `agrees_with_hypothesis_tokens` (`true`/`false`/`null`; `null` = no token window or no pick, excluded from the denominator). Computing the number needs BOTH shadow rows accruing (a scheduler wiring a real `param_chooser`) AND a report-side aggregation that does not exist yet — `NOT_COMPUTABLE_IN_STAGE_1` is a static tuple and no accuracy section exists. | 0.7 is the floor below which the Stage-2 promotion of the same chooser to serving (the S1C-3 design intent) is not defensible. The number means "the chooser's pick agrees with the **production hypothesis's own implied window**" (the run's redacted hypothesis) — NOT the S1C-1 corpus: observations are not corpus-keyed, and true corpus anchoring requires the evaluation harness that plans corpus hypotheses (the report's `corpus_expectation_accuracy` gap). |
| Worker p95 latency | `enqueue_to_complete_seconds` p95 ≤ 300 s, over ≥ 20 done outbox items | `worker_latency.enqueue_to_complete_seconds` (`p95`; the section states `includes_queue_wait: true`) | The label is the report's own: this measures **enqueue-to-complete including queue wait**, so it bounds evidence freshness, not planner CPU. 300 s matches the outbox's default lease (`claim_telemetry_work` `lease_seconds=300`): a healthy worker finishes an item within one lease. |
| Per-item planning request ceiling | stays **60** (not raised without a new signature) | Not a report key — a code constant: `MAX_REQUESTS_PER_ITEM = 60` (`governed_telemetry_worker.py`). Drops past the cap surface **only** in worker logs and the invocation's returned summary (its `dropped` key) — never in the report; `volumes.by_mode` shows observation volumes, not drops | The ceiling bounds worker cost per item. Raising it is a cost decision, so it is pinned here rather than left to drift; the cap drops intents last (a capped recipe is still in the registry tomorrow). |

The report's authority-floor pass rate (`authority_floor.pass_rate`, denominator = `met + unmet`
only) is reported but deliberately **not thresholded** in this draft: the `concept`-tier
promotion that would make the stronger floor meaningful is chartered Stage-2 work (see the
runbook's residuals section), so a Stage-1 number would threshold a floor known to be
tier-informational.

## Minimum-evidence floors

The Stage-2 gate reads **"threshold met over at least N"**, never a bare rate — a rate over 3
rows proves nothing. Below its floor a threshold is **not evaluable**: neither met nor failed.

| Threshold | Not evaluable below |
|---|---|
| Resolution rate per domain | 20 observations **in that bucket** (a platform-wide total does not evaluate a domain) |
| Stale-registry rate | 20 recipe-origin observations (`bridge_demand.stale_registry.recipe_origin_observations`) |
| Chooser accuracy | 10 token-evaluable picks (rows where `agrees_with_hypothesis_tokens` is not `null`) |
| Worker p95 | 20 done outbox items (the report itself refuses a percentile below 2: `enqueue_to_complete_seconds: null` with a reason) |
| Per-item request ceiling | always evaluable — it is a code constant, checked by inspection |

Evidence accrues **only while `FEATUREGEN_INTENT_SHADOW_TELEMETRY` is on and a worker runs** —
see the runbook. With the flag off, every floor above starves.

## Not thresholded in Stage 1 — and why

These are the report's own `not_computable_in_stage_1` entries, metric names verbatim
(`NOT_COMPUTABLE_IN_STAGE_1` in `governed_planning_report.py`). Each names the evidence it
lacks; thresholding any of them in Stage 1 would threshold a fabrication.

| Metric (report's name) | Why not computable in Stage 1 |
|---|---|
| `chooser_accuracy` | No chooser shadow rows until a scheduler wires a real chooser and its shadow decisions accrue (the S1C-3 machinery is merged; the report text still says "until S1C-3 lands" — accrual, not landing, is the real dependency). Computing it is ALSO a report-side code change: `NOT_COMPUTABLE_IN_STAGE_1` is a static tuple and no accuracy aggregation exists yet. Thresholded above as a DRAFT number so the floor exists the day both halves do. |
| `corpus_expectation_accuracy` | Observations are not corpus-keyed (no observation column joins to `corpus_id`); pairing expectation with outcome needs an evaluation harness that plans the corpus hypotheses. |
| `fan_out_risk_distribution` | `governed_planning_observation` (1120) persists no segment-cardinality column; `hop_count`/`bridge_count` carry reach, not cardinality. Stage-2's plan-envelope work is where that evidence arrives. |
| `incremental_cross_catalog_relevance` | Needs served A/B exposure; Stage 1 serves nothing. |
| `per_query_db_percentiles` | Per-query DB timings are never persisted; the only persisted timestamps are the outbox's enqueue/complete pair. |
| `production_certification_outcomes` | Stage 1 produces no production-certification validation claims to count. |
| `sandbox_profiling_outcomes` | Stage 1 produces no sandbox-profiling validation claims to count. |
| `served_ranking_quality` | Nothing is served in Stage 1 — no served rankings to score. |
| `sme_review_of_served_cards` | Nothing is served in Stage 1; `governed_plan_review_event` reviews PLANS, not servings. |

## The S1C-1 corpus baseline (review context)

The packaged S1C-1 corpus (`hypothesis_corpus_v1.json`, loaded by `hypothesis_corpus.py`):
17 entries across the seven closed banking domains (retail 3, customer 3, servicing 3, cib 2,
payments 2, cards 2, accounts 2), all `review_status: "draft"` (pinned — the file may not claim
reviews that do not exist), 11 of 17 expecting cross-catalog reach. The report's
`corpus_status` section shows these same counts live.
