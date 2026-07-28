# Catalog Search Freshness: Problem and 90-Day Solution

**Date:** 2026-07-23  
**Status:** Design recommendation only; no runtime code or deployment configuration changed

## 1. Problem

The catalog search page at `http://localhost:8080/#/search` shows no results even though the FTR
upload was successfully persisted.

This is not a frontend rendering failure and it is not an empty database. The search API returns
HTTP 200 with an intentionally empty result:

```json
{
  "hits": [],
  "total": 0
}
```

The backend removes every FTR node because its source freshness watermark is older than the
hard-coded 24-hour search window.

## 2. Verified Evidence

The deployed PostgreSQL database contained:

| Observation | Value |
|---|---:|
| `graph_node` rows | 127 |
| Catalog sources | 1 (`ftr`) |
| FTR watermark | `2026-07-21 19:14:37 UTC` |
| Watermark age at investigation | approximately 40 hours |
| Current search freshness window | 24 hours |
| Rows passing freshness | 0 |

The stored full-text search documents are valid:

| Query | Matching stored nodes before freshness filtering |
|---|---:|
| `customer` | 76 |
| `amount` | 32 |

Therefore, ingestion, graph persistence and full-text indexing succeeded. Freshness filtering is the
only reason the results disappear.

## 3. Current Code Path

`src/featuregen/overlay/upload/search.py` defines:

```python
fresh_within: timedelta = timedelta(hours=24)
```

Every search applies this non-optional predicate:

```sql
COALESCE(n.attested_at, w.last_completed_at) >= :cutoff
```

The query also performs an inner join from `graph_node` to `overlay_drift_watermark`. A node is
therefore absent when:

1. Its source has no watermark.
2. Its own `attested_at` is older than the cutoff.
3. Its source watermark is older than the cutoff.

The API route currently calls `search(...)` without supplying `fresh_within`, so the 24-hour default
always controls catalog search.

## 4. Why 24 Hours Is Wrong for FTR

A 24-hour policy can be appropriate for a live connector expected to scan every few hours. It is
not appropriate for a manually uploaded glossary or catalog mapping that may remain authoritative
for weeks or months.

Under the current behavior:

1. An FTR file uploads successfully.
2. Search works for one day.
3. After 24 hours, all assets silently disappear.
4. The UI looks like ingestion failed even though all graph nodes remain stored.

This creates a false product signal and makes manually managed catalogs unusable without daily
re-upload.

## 5. Immediate Operational Workaround

Re-upload the same FTR file through the normal ingestion API. A successful ingestion advances the
source watermark and makes the nodes searchable for another 24 hours.

Do not manually update `overlay_drift_watermark` in PostgreSQL. That would claim a source was checked
when no ingestion or source verification occurred.

## 6. Recommended Permanent Solution

Use a source-aware freshness policy and set manually uploaded FTR catalogs to 90 days.

Recommended policy:

| Source type | Suggested freshness |
|---|---:|
| Streaming or frequently scanned connector | 1 day |
| Daily or weekly connector | 7-14 days |
| Periodic governed file feed | 30 days |
| Manual FTR/catalog upload | 90 days |

For the first release, a single deployment-wide 90-day setting is acceptable if FTR is the only
catalog source. Ninety days equals `129600` minutes.

The platform already has a sealed configuration field:

```text
OVERLAY_DRIFT_FRESHNESS_SLA_MIN
```

The deployment value for 90 days would be:

```text
OVERLAY_DRIFT_FRESHNESS_SLA_MIN=129600
```

However, setting this environment variable alone does not fix search today because the search route
does not consume the sealed overlay freshness setting.

The required implementation is:

1. Read `current_overlay_config().drift_freshness_sla` in the search API route.
2. Pass that duration into `search(..., fresh_within=...)`.
3. Configure the FTR deployment with `OVERLAY_DRIFT_FRESHNESS_SLA_MIN=129600`.
4. Restart the backend so the sealed configuration is rebuilt.

This is preferable to introducing a search-only environment variable because the same source should
not be "current" in search while being "stale" in planner, governed-fact or feature-validation paths.

## 7. Longer-Term Source-Specific Model

A deployment-wide 90-day value becomes too broad when live connectors and manual uploads coexist.
The durable end state should resolve freshness by source capability:

```text
catalog_source
  -> source capability profile
  -> expected refresh cadence
  -> freshness SLA
```

The read path should use the source-specific SLA when evaluating each node. The policy must be
server-owned and versioned; callers must not be allowed to supply a larger freshness window.

Suggested source policy fields:

```text
catalog_source
source_kind
expected_refresh_interval
freshness_sla
policy_version
effective_from
```

## 8. UI Requirement

An expired source should not look identical to an empty catalog.

Search should expose a read-scoped status such as:

```text
127 assets are hidden because source "ftr" is stale.
Last successful refresh: 2026-07-21 19:14 UTC.
Required refresh interval: 90 days.
```

The API can provide aggregate stale-source metadata without returning restricted asset identities.
This preserves fail-closed search behavior while making the reason actionable.

## 9. Required Tests

The implementation should add tests proving:

1. The API route passes the sealed freshness SLA to search.
2. A 30-day-old FTR watermark is visible under a 90-day SLA.
3. A 91-day-old watermark is hidden.
4. Missing watermarks remain fail-closed.
5. Quarantine-resolved rows continue to use their own `attested_at`.
6. Facet counts exclude stale rows using the same policy as hits.
7. Planner and governed-fact freshness use the same configured duration.
8. Invalid or non-positive freshness configuration fails at startup.

## 10. Acceptance Criteria

After the 90-day policy is implemented and deployed:

1. `GET /search?q=&limit=20` returns FTR assets while the FTR watermark is at most 90 days old.
2. Searching `customer` returns matches from the existing set of 76 matching search documents.
3. Searching `amount` returns matches from the existing set of 32 matching search documents.
4. On day 91, the source is hidden unless it has been successfully refreshed.
5. The UI explains stale-source exclusion instead of presenting an unexplained empty catalog.
6. No database watermark is advanced without a real ingestion or trusted source refresh.

## 11. Adjacent RBAC Issue

The dev UI offers the role string `platform-admin`, while functional catalog permissions define
`platform_admin`. Selecting the hyphenated role causes `/search` to return:

```text
403 missing permission: catalog:read
```

This did not cause the verified empty FTR result, which returned HTTP 200 under `data_owner`, but it
is a separate role-vocabulary integration defect that should be reconciled before release.

## 12. Feature-Generation Brief Wiring

### 12.1 Problem

The product requires every free-form feature-generation decision to consider both:

```text
hypothesis
prediction goal
```

The current main generation loop mostly does this, but the guarantee is not end-to-end. Some LLM
calls receive both values, while other calls silently lose one or both. Consequently, a feature can
be generated under one business brief and later ranked or revised under a weaker or different brief.

### 12.2 Verified Current Behavior

The Workbench sends both fields to the governed considered-set endpoint:

```text
contractConsideredSet(hypothesis, objective, ...)
```

`build_considered_set` redacts the prediction goal and constructs one combined instruction:

```text
<redacted hypothesis>

prediction goal: <redacted prediction goal>
```

That combined instruction reaches the initial free-form author, candidate critic and automatic
critic-repair call. The audited LLM seam stores it as `redacted_intent`, applies the egress guard,
records the call and passes it to the Anthropic adapter.

The complete verified matrix is:

| LLM stage | Hypothesis | Prediction goal | Status |
|---|---:|---:|---|
| Recognition | Yes | Yes | Correct |
| Initial free-form author | Yes | Yes | Correct, but concatenated |
| Candidate critic | Yes | Yes | Correct, but concatenated |
| Automatic critic repair | Yes | Yes | Correct, but concatenated |
| Feature-set recommendation | Yes | No | Defect |
| Manual candidate refinement | No | Yes | Defect |
| Definition-mode anchor generation | No | No | Uses only the definition |
| Direct `/features/recommend*` APIs | Not separate | One `objective` field only | Contract gap |

### 12.3 Verified Wiring Defects

#### Feature-set recommendation drops the prediction goal

After alternatives have been generated and validated, `build_considered_set` calls
`recommend_set(...)` with only `intent.redacted_hypothesis`. The recommendation LLM therefore ranks
the validated sets without the prediction target or horizon.

Example:

```text
Hypothesis: declining account activity indicates disengagement
Prediction goal: predict customer churn in the next 30 days
```

The author sees both statements, but the set recommender sees only the first. It can prefer
long-term descriptive features even though the actual goal requires near-term churn signals.

#### Manual refinement drops the hypothesis

The Workbench passes the current goal to `/features/refine`, but it does not pass the hypothesis.
The backend sends the human repair instruction as the LLM instruction and places the goal in
catalog metadata. The model therefore revises the candidate without the original causal premise.

The frontend also sends the editable current `goal`, not the immutable `roundObjective` captured
when the generation round started. If the user edits the goal after generation, refinement can run
under a different objective from the one that produced the candidate.

#### Confirmed scope is not wired into free-form context selection

The backend persists the human-confirmed taxonomy scope and uses it for recipe applicability,
dispositions and ranking. The free-form `_generate` function supports a `ConfirmedScope`, and its
metadata selector can use that scope as a relevance signal. However,
`recommend_feature_sets_report` has no scope parameter and `build_considered_set` does not pass the
confirmed scope into free-form generation.

This does not make free-form generation unrestricted in a useful way. It causes the metadata menu
to omit an available relevance signal, potentially selecting less relevant columns when the graph
contains many assets. Scope should be a soft context and ranking signal, not a hard filter.

#### Direct feature-assist APIs do not enforce a complete brief

`/features/recommend` and `/features/recommend-sets` accept one required `objective` string rather
than separate required `hypothesis` and `prediction_goal` fields. They remain callable without a
complete business brief even though the governed Workbench route supplies both.

The definition-mode anchor has a similar gap: its author receives only the submitted definition,
not the hypothesis and prediction goal associated with the considered-set request.

### 12.4 Product Impact

These defects do not prevent an LLM call from completing. They create inconsistent feature quality:

1. Initial proposals can fit both the hypothesis and goal.
2. The set recommender can select a winner using only the hypothesis.
3. A human refinement can change the candidate using only the goal and repair instruction.
4. A confirmed taxonomy scope can improve recipes while having no effect on free-form metadata
   selection.

The final visible feature may therefore no longer reflect the complete brief that started the
generation round. The behavior is especially risky for prediction horizons, target events and
hypotheses that distinguish correlation from causally meaningful precursor signals.

### 12.5 Recommended Permanent Solution

Introduce a versioned, server-owned `FeatureGenerationBriefV1`:

```text
FeatureGenerationBriefV1
  hypothesis
  prediction_goal
  confirmed_scope
  lens
  human_feedback
```

`hypothesis` and `prediction_goal` must be required for normal free-form generation. The remaining
fields are optional but explicitly represented. Both free-text fields must pass through the current
redaction and egress controls before dispatch.

Thread the brief through:

1. Initial free-form authoring.
2. Candidate criticism.
3. Automatic critic repair.
4. Feature-set recommendation.
5. Manual candidate refinement.
6. Definition-mode anchor generation.
7. Direct feature-assist endpoints that remain publicly supported.

The prompt adapter should render clearly labelled sections rather than relying on an untyped
concatenated string:

```text
Hypothesis:
<redacted hypothesis>

Prediction goal:
<redacted prediction goal>

Confirmed scope:
<soft relevance context>

Strategy lens:
<lens>

Human feedback:
<optional feedback>
```

The same canonical, redacted brief should be hashed and recorded with each LLM call so author,
critic, repair, recommendation and refinement calls can be proven to belong to the same generation
round.

### 12.6 Required Wiring Changes

1. Add `hypothesis` and `prediction_goal` as separate required fields to supported feature-assist
   request contracts.
2. Replace the single `objective` argument across the free-form author/critic/repair APIs with a
   typed brief.
3. Pass the confirmed `ConfirmedScope` from `_scoped_considered_set` through
   `build_considered_set` and `recommend_feature_sets_report` into `_generate`.
4. Keep scope advisory for free-form relevance; do not use it to prohibit proposals outside the
   authored recipe taxonomy.
5. Change `recommend_set` to consume the same canonical brief as the author.
6. Change `/features/refine` and the Workbench caller to send the immutable `roundHypothesis` and
   `roundObjective`.
7. Include the full brief in definition-anchor generation while retaining the definition as the
   anchor-specific instruction.
8. Version the request contract and LLM prompt because this changes model-visible input.

### 12.7 Required Tests

Add outbound-request capture tests proving:

1. Recognition receives the redacted hypothesis and prediction goal.
2. Every initial author call receives both fields.
3. Every critic and automatic repair call receives the identical round brief.
4. Feature-set recommendation receives both fields.
5. Manual refinement receives the original round hypothesis and objective even if the editable UI
   fields have subsequently changed.
6. Definition-mode anchor generation receives definition, hypothesis and prediction goal.
7. Confirmed scope reaches free-form relevance selection as soft context.
8. Direct feature-assist endpoints reject a request missing either required field.
9. PII redaction and the metadata-only egress guard continue to apply to both free-text fields.
10. The immutable LLM audit records carry a stable hash of the same canonical brief across the
    generation round.

The existing `test_considered_set_threads_the_objective` is insufficient: it only asserts that
alternatives were produced. It must inspect captured outbound requests and prove the objective was
present at every downstream LLM stage.

### 12.8 Acceptance Criteria

The fix is complete when:

1. No supported free-form generation entrypoint can start without both a hypothesis and prediction
   goal.
2. Author, critic, repair, recommendation and refinement calls carry the same immutable brief.
3. Manual refinement cannot drift to edited Workbench inputs from a later round.
4. Confirmed taxonomy scope improves metadata relevance without restricting free-form ideation to
   authored recipes.
5. Definition mode retains its definition anchor while also carrying the complete business brief.
6. LLM audit records make context propagation independently verifiable.
7. Flag-off or legacy behavior is either explicitly migrated or rejected; there is no silent
   one-field compatibility path that weakens the guarantee.

## 13. Scope-Confirmation Flag Coupling Defect

### 13.1 Problem

The intended product flow requires the recognition LLM to propose a taxonomy scope and a human to
confirm, modify or explicitly broaden that scope before recipe generation. The current deployment
can bypass this gate completely because the confirmation UI and backend recipe narrowing are
controlled by independent, default-off flags:

```text
Frontend: VITE_INTENT_CONFIRMATION_UI
Backend:  FEATUREGEN_INTENT_SCOPED_APPLICABILITY
```

When the frontend flag is off, Generate does not call `/contract/recognitions`. It immediately calls
`/contract/considered-set` without `confirmed_scope`. The backend then receives
`applicability=None`, and `_templates_to_ground` returns `ALL_TEMPLATES`.

This is not an implicit human approval of an LLM-proposed scope. Recognition and scope creation
never happen. The system silently uses the legacy one-shot path:

```text
hypothesis + prediction goal
  -> considered set
  -> all authored recipes
  -> ground every buildable recipe against the selected catalog
```

### 13.2 Verified Product Effect

The FTR test produced recipe proposals from unrelated use-case families in one response:

```text
Churn / engagement:
  dormancy_days
  txn_frequency_trend_90d
  salary_signal_90d

Fraud:
  merchant_risk_anomaly

AML:
  structuring_smurfing_30d
  cash_intensity_ratio_90d
  rapid_movement_passthrough_7d
  fan_in_fan_out_30d
  dormant_reactivation_180d

Payments:
  purpose_code_diversity_90d

Cross-sell:
  channel_adoption_depth_90d
```

These recipes appeared because their required metadata concepts could be bound to FTR columns. For
example:

```text
event_timestamp        -> tran_date
customer_id            -> cif_id
monetary_flow          -> actual_counter_party_amt
debit_credit_indicator -> tran_dc
iso20022_purpose_code  -> tran_particular_code
channel                -> channel_desc
```

This proves that buildability, not hypothesis relevance, selected the recipe proposals. The
hypothesis still reaches the separate free-form LLM path, but it does not select the recipe
candidates when scoped applicability is bypassed.

`DESIGN-CHECKED` on these proposals means that the metadata bindings passed the design-time
gauntlet. It does not prove that a proposal is relevant to the hypothesis, has been executed against
data, or has predictive value.

### 13.3 Flag-State Matrix

The two flags are independent, creating four deployment states:

| Confirmation UI | Scoped applicability | Actual behavior |
|---|---|---|
| Off | Off | No recognition or approval; all recipes attempted |
| On | Off | Human confirms scope, but backend still attempts all recipes |
| Off | On | No confirmed scope reaches backend; all recipes attempted |
| On | On | Human confirms scope and backend narrows to eligible recipes |

Only the final state delivers the intended product behavior.

Because `VITE_*` values are embedded by Vite, changing
`VITE_INTENT_CONFIRMATION_UI` requires rebuilding or restarting the frontend with the value present.
Changing only the backend runtime environment cannot make the confirmation screen appear.

### 13.4 Why This Is a Release Defect

The current fallback is fail-open for relevance. It can return structurally valid but unrelated
recipes and present them beside hypothesis-grounded free-form proposals without making the
difference obvious to the user.

This causes:

1. Mixed churn, fraud, AML, payments and cross-sell proposals in one generation round.
2. Higher grounding cost because the complete recipe registry is evaluated.
3. A misleading impression that every `DESIGN-CHECKED` recipe was selected for the hypothesis.
4. Inconsistent behavior across deployments depending on two independently configured flags.
5. No durable evidence that a human accepted the scope governing the recipe set.

For the first release, silent unscoped recipe generation should not be the normal fallback.

### 13.5 Recommended Fix

Enable both flags for the intended release:

```text
VITE_INTENT_CONFIRMATION_UI=1
FEATUREGEN_INTENT_SCOPED_APPLICABILITY=1
```

Configuration alone is not a sufficient permanent control. The backend must enforce the product
invariant:

> Recipe generation requires a persisted human-confirmed scope, unless the human explicitly chose
> "Show all buildable recipes."

Implement the invariant as follows:

1. Make scoped generation the normal server contract, not a frontend convention.
2. Reject a recipe-generating considered-set request that omits `confirmed_scope`, instead of
   silently substituting `ALL_TEMPLATES`.
3. Represent "Show all buildable recipes" explicitly as `unscoped=true` with a trusted
   `confirmation_source="broaden"` and persist that human decision.
4. Retain the legacy one-shot path only behind an explicit emergency rollback mode that is visible
   in health/status output and audit records.
5. Add a startup or deployment check that rejects mismatched frontend/backend scope flags.
6. Expose the effective backend scope mode through a configuration endpoint so the frontend can
   detect a build/runtime mismatch.
7. Label template-origin and free-form-origin proposals distinctly in the response and UI.

### 13.6 Required Tests

Add tests proving:

1. With both flags enabled, Generate calls recognition and does not generate until the human
   confirms or broadens.
2. A confirmed narrow scope passes only `applicability.eligible_ids` to recipe grounding.
3. UI-on/backend-off is detected and cannot silently attempt all recipes.
4. UI-off/backend-on is detected and cannot silently attempt all recipes.
5. A request without `confirmed_scope` is rejected in normal release mode.
6. Explicit `unscoped=true` is accepted only through the broaden action and is persisted.
7. A churn scope cannot surface AML-, fraud-, payments- or cross-sell-primary recipes unless they
   are deterministically classified as supporting.
8. Template candidates carry their applicability relationship and reason codes to the response.
9. Emergency rollback is explicit, auditable and visible in health status.

### 13.7 Acceptance Criteria

The defect is closed when:

1. A normal user cannot reach recipe generation without confirming or explicitly broadening scope.
2. Both frontend and backend agree on the effective scope mode.
3. Missing scope never silently means `ALL_TEMPLATES`.
4. "Show all buildable recipes" is a deliberate, persisted human action.
5. Every surfaced recipe records whether it was a primary or supporting scope match.
6. Recipe relevance comes from deterministic confirmed-scope applicability; catalog grounding then
   decides buildability.

## 14. Recipe Retrieval Authority and Label-Mapping Defect

### 14.1 Current Flow

Recipe retrieval is not currently an LLM search over recipe descriptions. The system separates
intent recognition from recipe selection:

```text
redacted hypothesis + prediction goal
  -> recognition LLM
  -> closed-taxonomy primary/secondary use-case ids
  -> human-confirmed scope
  -> deterministic applicability evaluation
  -> catalog grounding and design-time validation
  -> feature proposals
```

The recognition LLM receives the redacted request and proposes identifiers from the closed taxonomy.
It does not receive catalog columns or select recipe ids directly
(`src/featuregen/overlay/upload/taxonomy/recognizer.py`).

The API builds one `ApplicabilityResult` from the confirmed scope before generation
(`src/featuregen/api/routes/contract.py::_scoped_considered_set`). The applicability evaluator scans
`ALL_TEMPLATES` and classifies each recipe exactly once:

```text
recipe primary objective matches confirmed scope
  -> primary

recipe supporting objective matches confirmed scope
  -> supporting

otherwise
  -> out_of_scope
```

That deterministic classification is implemented in
`src/featuregen/overlay/upload/taxonomy/applicability.py::in_scope_recipes`. Eligible primary and
supporting recipes are then grounded against the catalog. Grounding, freshness, leakage, point-in-time,
grain and join checks can still reject a relevant recipe that cannot be built safely from the available
metadata.

After candidate construction, `feature_assist.recommend_set` makes an advisory LLM choice between
feature-set lenses. It does not govern individual recipe eligibility and must not be treated as the
recipe retrieval authority.

### 14.2 The Actual Defect

The deterministic matcher is the correct mechanism, but much of the metadata it consumes is inferred
from legacy recipe tags rather than explicitly authored.

Every legacy recipe currently carries a flat `Template.use_cases` tag bag. The code in
`taxonomy/recipe_applicability.py::recipe_applicability` converts that bag into one primary objective
and zero or more secondary objectives using this precedence:

```text
per-recipe override
  -> first in-family selectable leaf derived through the legacy crosswalk
  -> orphan mapping
  -> broad family fallback
```

This can assign a technically valid but overly broad primary objective. For example, specialized fraud
recipes are overridden to `fraud.transaction_fraud_detection`, and specialized AML recipes are
overridden to `aml_cft.suspicious_transaction_monitoring`. Their more precise use cases may exist only
as secondary mappings.

Consequences include:

1. A specialized objective can appear to have no primary recipe even when a suitable recipe exists.
2. The most relevant recipe can rank as supporting instead of primary.
3. Broad monitoring objectives appear to own most of the library.
4. Coverage reports measure the inferred mapping rather than the recipes' real business purpose.
5. A future cap on supporting recipes could create false narrowing.
6. Adding more undifferentiated labels cannot distinguish direct applicability from incidental value.

This defect is independent of section 13. Section 13 controls whether scoped applicability executes at
all. This section controls whether the recipe-to-scope mappings used by that execution are correct.

### 14.3 Why an LLM Must Not Be the Recipe Authority

The platform should continue using an LLM to translate natural-language hypotheses and prediction
goals into the closed taxonomy. It should not send the complete recipe library to an LLM and allow the
model to produce the authoritative recipe-id set.

Direct LLM recipe selection would introduce:

1. Non-repeatable selection for identical inputs.
2. Silent omission of relevant recipes and therefore false narrowing.
3. Possible invented or stale recipe identifiers.
4. Weak explanations for why a recipe was included or excluded.
5. Token cost and latency that grow with the recipe registry.
6. A replay problem when the model or prompt changes.
7. No simple proof that every explicitly mapped recipe was considered.

An LLM may advise on presentation order or explain why deterministic candidates fit the hypothesis.
It must not silently remove an eligible recipe, promote an out-of-scope recipe, bypass catalog
grounding, or override a safety rejection.

### 14.4 Recommended Authority Boundary

Use a hybrid flow with a strict authority boundary:

```text
LLM responsibility:
  understand natural language
  propose closed-taxonomy scope
  optionally explain/rerank an already eligible result set

Human responsibility:
  confirm, correct or deliberately broaden the proposed scope

Deterministic code responsibility:
  retrieve every explicitly mapped primary/supporting recipe
  ground recipes against available catalog metadata
  apply governance and safety checks
  retain replayable inclusion/exclusion reason codes
```

For example:

```text
Request:
  Detect merchants with abnormal refund behavior

Recognition proposal:
  primary = fraud.merchant_fraud

Deterministic retrieval:
  merchant_refund_spike -> primary
  merchant_risk_anomaly -> primary
  transaction_velocity -> supporting
  new_device_geo_anomaly -> out_of_scope

Catalog grounding:
  keep only recipes whose required operands can be bound safely
```

### 14.5 Recipe Metadata Change

Keep the existing recipes and formulas, but replace inferred ownership with explicit, reviewed
applicability declarations:

```python
primary_objectives = (
    "fraud.merchant_fraud",
)

supporting_objectives = (
    "fraud.transaction_fraud_detection",
    "aml_cft.suspicious_transaction_monitoring",
)
```

The distinction is mandatory:

```text
primary objective
  = the use case the recipe is specifically designed to solve

supporting objective
  = another use case for which the recipe can provide useful evidence
```

Do not replace these fields with one flat `labels` list. A flat list cannot support reliable ranking,
coverage accounting or explanations.

During migration, the current crosswalk and override logic may generate a draft mapping for all 153
recipes. A banking SME must review that draft. After migration, legacy derivation should remain only as
an explicit compatibility fallback with telemetry; it must not silently override authored mappings.

### 14.6 Deterministic Retrieval and Ranking

The applicability evaluator should consume the explicit mappings and return a complete eligible set.
Presentation ranking should be deterministic before any optional LLM advice:

```text
exact primary objective match
  -> exact supporting objective match
  -> deliberately expanded parent/child taxonomy match
  -> explicit compatibility fallback
```

At the current registry size, scanning 153 recipes is acceptable. As the library grows, build a
versioned inverted index:

```text
objective_id -> primary recipe ids
objective_id -> supporting recipe ids
```

The index is an optimization only. The versioned explicit recipe mappings remain the source of truth.

### 14.7 Required Tests

Add tests proving:

1. The same confirmed scope and recipe-registry version always produce the same eligible recipe ids.
2. Every recipe id returned by applicability exists in the registered recipe library.
3. Every explicit primary match is returned and classified as primary.
4. Every explicit supporting match is returned unless the same recipe is already a primary match.
5. An unrelated recipe cannot be included by an advisory LLM response.
6. An optional LLM ranking failure leaves deterministic retrieval intact.
7. Grounding can reject an eligible recipe without changing its applicability classification.
8. Broad family fallback cannot become the primary mapping for a specialized recipe without an
   explicit reviewed exception.
9. Every selectable taxonomy objective has the required primary and effective recipe coverage.
10. Recognition gold cases measure false narrowing against the complete deterministic eligible set.
11. The response records mapping version, primary/supporting relationship and reason codes.
12. Section 13's scope flags are enabled and a missing scope cannot silently invoke `ALL_TEMPLATES`.

### 14.8 Acceptance Criteria

This issue is closed when:

1. Recipe applicability is explicitly authored and reviewed, not silently inferred from broad tags.
2. The LLM maps natural language to the closed taxonomy but does not own recipe inclusion.
3. Human-confirmed scope is the input to deterministic recipe retrieval.
4. Every matching primary and supporting recipe is considered before grounding.
5. Catalog and governance checks, not the LLM, decide whether a retrieved recipe is buildable.
6. Optional LLM ranking is advisory, bounded to eligible candidates and failure-neutral.
7. Inclusion, exclusion, fallback and grounding decisions are replayable from versioned inputs.

## 15. Four Active Objectives With Zero Effective Recipe Coverage

### 15.1 Problem

The current taxonomy coverage report has four active selectable leaves with neither a primary nor a
supporting recipe:

```text
credit.monitoring.obligor
fraud.merchant_fraud
treasury_alm.deposit_runoff_forecasting
treasury_alm.net_interest_margin
```

This must not be interpreted as proof that all four objectives have no reusable formula logic.
Coverage has three distinct levels:

```text
Mapped
  = a recipe is explicitly associated with the objective

Groundable
  = the selected catalog contains authoritative metadata satisfying the recipe needs

Formula-capable
  = the recipe has a typed computation that can be compiled for the external execution platform
```

The existing report primarily exposes a mapping gap. Code review shows existing formula-shaped recipes
for obligor exposure, merchant risk and deposit runoff inputs, but no complete net-interest-margin
recipe. The repair must therefore combine reviewed remapping with new recipe authoring. Adding a label
to an unrelated formula is not coverage.

### 15.2 Obligor Exposure

`group_exposure_aggregation` already operates at an `obligor_id` grain and calculates group exposure,
single-obligor share and group concentration. Its current applicability is:

```text
primary:
  portfolio_risk.concentration

secondary:
  corporate_trade.trade_finance
  credit.monitoring.limit_management
```

This leaves `credit.monitoring.obligor` uncovered even though part of the recipe is directly relevant.
The reviewed mapping should become:

```python
primary_objectives = (
    "credit.monitoring.obligor",
)

supporting_objectives = (
    "portfolio_risk.concentration",
    "credit.monitoring.limit_management",
    "corporate_trade.trade_finance",
)
```

The objective should not depend on this one multi-purpose recipe alone. Add a minimum obligor-monitoring
pack:

```text
obligor_total_exposure
obligor_limit_headroom
obligor_connected_group_exposure
obligor_credit_mitigation_coverage
```

Representative computation:

```text
obligor_total_exposure =
    drawn_exposure
  + contingent_exposure * credit_conversion_factor
```

Each recipe must declare its obligor grain, point-in-time anchor, currency handling, exposure
additivity and authoritative identifier requirements.

### 15.3 Merchant Fraud

`merchant_risk_anomaly` uses merchant-specific inputs (`merchant_id` and `mcc`) but is currently mapped
primarily to broad transaction fraud:

```text
primary:
  fraud.transaction_fraud_detection

secondary:
  fraud.card_fraud
```

Correct its applicability to:

```python
primary_objectives = (
    "fraud.merchant_fraud",
)

supporting_objectives = (
    "fraud.transaction_fraud_detection",
    "fraud.card_fraud",
)
```

`chargeback_dispute_rate` should be reviewed as a supporting merchant-fraud recipe. Add a focused
merchant-fraud pack:

```text
merchant_refund_spike
merchant_chargeback_acceleration
merchant_bustout_velocity
merchant_terminal_concentration
merchant_collusive_network
merchant_settlement_account_change
```

These recipes must use merchant/acquiring grain where applicable. A cardholder-level anomaly must not
be relabelled as merchant fraud merely because a merchant identifier is present.

### 15.4 Deposit Run-Off Forecasting

Several current deposit recipes provide runoff inputs or indicators:

```text
nmd_stickiness
maturity_ladder_runoff
early_withdrawal_break
deposit_beta
hot_money_share
rate_sensitive_concentration
```

They are currently owned by deposit stability, liquidity or concentration. Review and add
`treasury_alm.deposit_runoff_forecasting` as a supporting objective where semantically valid.

Those mappings close discovery gaps but do not create a forecast. Add primary forecasting recipes:

```text
contractual_deposit_runoff
behavioural_nmd_runoff
segment_runoff_curve
stressed_deposit_runoff
```

Representative computation:

```text
forecast_runoff(segment, time_bucket, scenario) =
    contractual_maturities
  + expected_early_breaks
  + behavioural_nmd_decay
```

The recipe contract must explicitly carry:

```text
as-of date
forecast horizon
time buckets
deposit segment
contractual maturity
behavioural decay/runoff assumptions
rate or stress scenario
currency
output grain
```

The objective is an ALM portfolio/segment/time-bucket forecast. It must not be confused with
customer-level deposit attrition.

### 15.5 Net Interest Margin

The current registry has useful adjacent inputs such as `deposit_beta`, `repricing_gap_exposure` and
generic monetary rate/flow concepts, but it has no complete NIM computation. This is a genuine formula
gap and must not be closed by relabelling an adjacent rate-risk recipe.

Add a NIM recipe pack:

```text
earning_asset_yield
funding_cost_rate
net_interest_income
net_interest_margin
deposit_ftp_margin
nim_rate_shock_projection
```

Core formulas:

```text
net_interest_income =
    interest_income - interest_expense

net_interest_margin =
    net_interest_income / average_earning_assets
```

Scenario projection:

```text
projected_nim =
    (
      projected_interest_income(rate_scenario)
      - projected_interest_expense(rate_scenario)
    )
    / projected_average_earning_assets
```

The current generic concepts are not sufficient authority to distinguish each formula role safely.
Review and, where required, add governed concepts or typed operand roles for:

```text
interest_income
interest_expense
earning_asset_balance
funding_balance
asset_yield
funding_cost_rate
ftp_rate
rate_scenario
```

Do not infer income versus expense, asset versus liability or numerator versus denominator from an LLM
description. Those are ordered, load-bearing formula roles.

### 15.6 Implementation Sequence

Implement this repair in the following order:

1. Introduce the explicit `primary_objectives` and `supporting_objectives` contract from section 14.
2. Review and remap the existing obligor, merchant-risk and deposit-runoff-related recipes.
3. Add the missing obligor, merchant-fraud, deposit-runoff and NIM recipes.
4. Add or tighten concept and operand-role vocabulary required by the new formulas.
5. Give every new recipe a typed formula, explicit grain, time semantics, units, currency and additivity.
6. Bump the taxonomy applicability, recipe-registry, operation-policy and formula-grammar versions as
   applicable.
7. Add positive and negative catalog fixtures through the real graph/evidence path.
8. Add recognition gold cases for each objective and representative banking-language variants.
9. Keep a relevant recipe visible as ungroundable when metadata is insufficient; do not fabricate
   operands to increase coverage.

The LLM may help draft candidate recipe metadata or recognize the objective. A banking SME must approve
the applicability and formula semantics, and deterministic validators must govern the result.

### 15.7 Required Tests

Add tests proving:

1. Every one of the four objective ids has at least one explicit primary recipe.
2. Existing remapped recipes remain available for their legitimate supporting objectives.
3. Obligor recipes bind an authoritative obligor grain and cannot substitute customer/facility grain.
4. Merchant-fraud recipes require merchant-side operands and cannot pass on cardholder evidence alone.
5. Deposit-runoff recipes emit portfolio/segment/time-bucket outputs rather than customer attrition.
6. Contractual and behavioural runoff components remain distinguishable and scenario-versioned.
7. NIM preserves the signs and order of income, expense and denominator operands.
8. NIM rejects a zero/unknown denominator and incompatible currency or period inputs.
9. Every formula has deterministic canonical serialization and a replayable content hash.
10. A recipe can be mapped but honestly reported as ungroundable for a catalog lacking required facts.
11. Coverage reporting publishes mapped, groundable and formula-capable counts separately.
12. Recognition gold cases retrieve every expected primary/supporting recipe without false narrowing.

### 15.8 Acceptance Criteria

The four-leaf coverage issue is closed only when:

1. Each active objective has an explicitly reviewed primary recipe.
2. Labels reflect real business applicability rather than broad family membership.
3. Each primary recipe carries a typed, formula-capable contract.
4. At least one positive governed catalog fixture grounds each objective end to end.
5. Missing catalog facts produce an honest ungroundable/validation result, not invented bindings.
6. The external execution contract receives ordered operands, grain, time, scenario and output semantics.
7. Coverage dashboards distinguish mapping, groundability and executable formula coverage.
