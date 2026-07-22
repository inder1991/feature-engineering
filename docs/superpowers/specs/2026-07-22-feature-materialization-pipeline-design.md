# Feature Materialization Pipeline — Design Spec

**Goal:** Turn a governed, design-checked feature definition (from the existing feature-generation catalog) into a production PySpark/Kedro pipeline that computes it — and many sibling features — at scale, lands the results in per-grain feature tables keyed by `(entity…, business_dt)`, profiles them, and pushes the stats back into catalog search.

**One line:** The catalog decides *what* a feature is; this system decides *how it is computed and where it lands*.

**Tech stack:** Kedro (nodes/pipelines/catalog/hooks), PySpark (compute), HDFS Parquet (raw→feature layers), Hive (model-input / consumption layer). This is a **new module**, separate from the FastAPI/Postgres catalog app; it integrates with the catalog through two narrow seams (the Feature Spec in, the feature stats out).

---

## Global Constraints

- **Grain of every feature = `(entity key(s) …, business_dt)`** — one row per entity per snapshot date. The lookback window (e.g. 90d) is a computation input, NOT part of the grain.
- **`business_dt`** (a.k.a. inference date) is mandatory on every feature and every final table. It is the point-in-time the feature was computed *as of*; all windows are trailing and end at `business_dt` (no look-ahead / no leakage).
- **Layers (Kedro data-engineering convention):** `raw → intermediate → primary → feature → model_input`. `raw`…`feature` = HDFS Parquet; `model_input` = Hive (or pluggable).
- **One final table per grain**, not one global table. A feature routes to its grain's table by its spec.
- **Batching:** features that share `(grain, cadence)` are computed in ONE Spark pass. This is the core efficiency requirement ("multiple features at once").
- **Auditability (bank requirement):** the engine is spec-driven (Option C) but can *render* the exact PySpark/SQL it runs for any feature on demand — no per-feature source files to maintain, full transparency for audit.
- **Point-in-time correctness:** trailing windows only; a feature computed for `business_dt = D` may read only rows with `tran_date ≤ D`.

---

## Why (motivation)

The feature-generation catalog stops at a design-checked *definition*: a name, `derives_from` columns, a *named* aggregation string, and a prose description. Nothing computes it. There is no structured computation spec (the catalog code itself flags this: *"naming-based detection is inherently incomplete — the real fix is structured aggregation metadata"*). This system supplies the missing structure and the compute platform, so a data scientist who selects a feature gets a real, scheduled, audited column — not a one-off notebook.

## Scope / non-goals

**In scope:** the Feature Spec contract; the spec-structuring step (prose → structured); the Kedro/PySpark engine; per-grain tables; domain pipelines + feature groups + schedule metadata; run-metric hooks; feature profiling (EDA) + write-back to search; code rendering.

**Out of scope (this spec):** model training/scoring; a real-time/online feature store (this is batch/offline); the orchestrator itself (we emit a group→schedule map that Airflow/cron consumes; we don't build the scheduler); backfill tooling beyond a `business_dt` range parameter.

---

## Architecture (Option C — spec-driven engine with code rendering)

```
Catalog (existing)                    Feature Materialization (new)
─────────────────                     ─────────────────────────────
design-checked feature   ──spec──▶   1. Typed formula + authoring pipeline
                                          (LLM-1 author → structural-validate → LLM-2 critic →
                                           governance compiler → RESOLVED/NEEDS_REVIEW/REJECTED → freeze)
definition                            2. Domain pipeline (Kedro)
                                          raw → intermediate → primary → feature → model_input
                                          (features batched by grain+cadence into shared Spark passes)
                                      3. Per-grain feature tables  (cif / cif+product / account …)
                                      4. Profiling (EDA) node ──stats──▶  back into Catalog search
                                      5. Hooks: run logs, runtime, row counts, per-node metrics
                                      6. Code renderer: spec → exact PySpark/SQL (audit view)
```

### Component 1 — The Structured Feature Spec

The machine-readable contract every downstream component consumes. YAML, one per feature, version-stamped. Example (`cross_border_value_ratio_90d`):

```yaml
feature: cross_border_value_ratio_90d
domain: aml
entity: [cif_id]                 # grain keys (excluding business_dt, which is always implied)
as_of: tran_date                 # the point-in-time column in the source
window: {length: 90, unit: day, type: trailing}
source: ftr.public.comp_financial_tran_repos_dly
params:
  home_country: 'AE'             # domain param — cross-border = counter_party_bank_cntry != home_country
filters:
  is_cross_border: "counter_party_bank_cntry != {home_country}"
operation:
  type: ratio
  numerator:   {agg: sum, column: tran_amt_aed, where: is_cross_border}
  denominator: {agg: sum, column: tran_amt_aed}
cadence: daily                   # scheduling: daily | weekly | monthly
priority: 1
status: design-checked
spec_version: 1
```

The spec above is the **deployment wrapper** (domain, cadence, priority, routing). Its heart is the **typed formula** — the computation contract — authored by the pipeline below. Domain params the definition can't know (`home_country`, thresholds) live in a per-domain config, not the feature.

#### The typed formula (what the LLM authors — NOT SQL/PySpark)

```json
{
  "operation": "ratio",
  "grain": {"entity": "customer", "keys": ["ftr:public.comp_financial_tran_repos_dly.cif_id"]},
  "window": {"type": "trailing", "length": 90, "unit": "day"},
  "time_operand": "ftr:public.comp_financial_tran_repos_dly.tran_date",
  "numerator":   {"aggregation": "sum", "operand": "ftr:public.comp_financial_tran_repos_dly.tran_amt_aed",
                  "filter": {"operation": "not_equal",
                             "left": "ftr:public.comp_financial_tran_repos_dly.counter_party_bank_cntry",
                             "right_parameter": "home_country"}},
  "denominator": {"aggregation": "sum", "operand": "ftr:public.comp_financial_tran_repos_dly.tran_amt_aed"},
  "zero_denominator": "null"
}
```

The formula names **exact `catalog:schema.table.column` operands** and typed operations only — never raw SQL/PySpark. The runtime compiler turns it into Spark; the LLM never writes execution code.

#### The formula-authoring pipeline (LLM authors, deterministic governs)

**Guiding principle:** the second LLM is a **critic, not a validator**. Two LLMs can confidently agree on the same wrong formula. Final authority is deterministic checks + targeted human review — never "two models agreed."

```
Feature intent (free-form) OR recipe definition
        ↓
LLM 1 — Formula AUTHOR  (ReAct loop; READ/VALIDATE tools only)
        ↓
Deterministic STRUCTURAL validation   (schema, operation vocabulary, column identity, operand completeness)
        ↓
LLM 2 — Independent semantic CRITIC   (structured findings only, not a rewrite)
        ↓
Deterministic GOVERNANCE COMPILER     (types/units/currency · grain/cardinality · verified joins/lineage ·
                                       time anchor/leakage · null & divide-by-zero · authorization/freshness)
        ↓
RESOLVED  /  NEEDS_REVIEW  /  REJECTED
        ↓  (RESOLVED)
Freeze + version the formula  →  compile to PySpark  →  execute (LLM-free at runtime)
```

**LLM 1 — author (ReAct, restricted tools).** Iterates until `validate_draft_formula` reports no structural omissions. Tools are strictly read/validate; it holds **no** tool that approves, executes, or mutates a governance fact:
`search_columns` · `get_column_metadata` · `get_governed_grain` · `get_time_anchor` · `get_verified_lineage` · `list_supported_operations` · `validate_draft_formula`.

**Deterministic structural validation** (between the two LLMs, so the critic reviews a structurally-sound draft): operation is in the supported vocabulary; every operand is a real `catalog:schema.table.column`; the operation's operands are complete (a ratio has numerator AND denominator; a windowed op has a `time_operand`).

**LLM 2 — independent critic.** Genuinely independent, or it's theatre: separate prompt + context construction; a **different model tier/family** where possible; it is **not shown LLM 1's reasoning**; it returns **structured findings** (not a rewritten formula); it must compare **every** business requirement in the intent against the formula's operands (missing operand? numerator/denominator direction correct? filter matches intent?). A finding routes the feature to `NEEDS_REVIEW`, it does not silently rewrite.

**Deterministic governance compiler** = the existing gauntlet + two-axis gate: types/units/currency consistency, grain & cardinality, VERIFIED joins & lineage (`resolve_fact` / `approved_join`), time-anchor & leakage (trailing-only), null / divide-by-zero behavior (`zero_denominator`), authorization & freshness. Its verdict — `RESOLVED / NEEDS_REVIEW / REJECTED` — is the final authority.

#### Recipe vs free-form (author once, run forever)

- **Recipe features:** the formula is authored **once at recipe-authoring time**, validated + approved, then **frozen and versioned**. It is NOT regenerated per run — runtime just compiles the frozen formula. (A recipe whose logic is already deterministic can skip LLM 1 and enter at structural validation.)
- **Free-form features:** the identical pipeline runs when the user proposes the idea. On `RESOLVED`, the formula can be **promoted into a reusable recipe** — so the recipe library grows itself from validated free-form work.

**This is where the scale comes from:** LLMs author thousands of candidate formulas; deterministic checks auto-clear the straightforward ones; humans review only disagreements, ambiguous mappings, novel operations, and high-risk features; approved formulas become reusable frozen assets; and **runtime execution stays deterministic and LLM-free.**

### Component 2 — Grain model & per-grain tables

A feature's `entity` decides its grain and therefore its target table. Every table is keyed by `(entity…, business_dt)`:

| grain | table (model_input, Hive) | keys |
|---|---|---|
| customer | `aml_customer_features` | `cif_id, business_dt` |
| customer × product | `aml_customer_product_features` | `cif_id, product_id, business_dt` |
| account | `aml_account_features` | `foracid, business_dt` |

Routing is automatic: the engine reads `entity` from each spec and appends the feature as a column in the matching grain's table. New grains create new tables; no manual wiring.

### Component 3 — Domain pipelines, feature groups, scheduling

- **Domain = a Kedro modular pipeline** (`aml`, `fraud`, `credit_risk`). Owns its raw→…→model_input layers and its feature groups.
- **Feature group = `(domain, grain, cadence)`** — the batching + scheduling unit. All customer-grain, daily AML features compute in ONE Spark pass over the transaction table. `priority` orders groups against each other but does not split a group.
- The pipeline emits a **group→schedule map** (`{group_id: {cadence, priority, grain, table}}`) that an external orchestrator consumes. We do not build the scheduler.

### Component 4 — Nodes & layers (kept simple)

Per domain, a small, fixed set of node types (pure `DataFrame → DataFrame` functions):

1. `build_intermediate` — type/clean the raw source (parse `tran_date`, cast amounts), derive shared flags. → `int.*` Parquet.
2. `build_primary` — canonical per-event fact at the source grain with shared derivations. → `prm.*` Parquet.
3. `compute_feature_group` — the batched engine: given the primary fact + a group's specs + a `business_dt`, compute ALL of the group's features in one windowed pass. → `fea.<group>` Parquet.
4. `assemble_model_input` — union/join the group's features into the per-grain `model_input` Hive table for `business_dt`.
5. `profile_features` — the EDA node (Component 6).

`compute_feature_group` is the only "smart" node: it interprets specs (window, filter, aggregation, operation) into Spark window/aggregate expressions. Everything else is plumbing.

### Component 5 — Hooks (run metrics)

Kedro hooks (`before_node_run` / `after_node_run` / `on_pipeline_error`) capture, per run and per node: `run_id`, `business_dt`, `group_id`, `rows_in/out`, `runtime_s`, `spark_stages`, `status`, and any error. Written to a `pipeline_run_metrics` table (Parquet/Hive) and structured logs. This is the observability the catalog side lacked.

### Component 6 — Profiling (EDA) + write-back to search

After a group's `model_input` table lands, `profile_features` computes per-feature stats: `count, null_rate, min, max, mean, p50, p95, distinct, histogram, last_refreshed, business_dt`. Written to a `feature_stats` table and **pushed back into the catalog's search/metadata surface**, so browsing a feature shows a distribution sparkline, min/max, and freshness *before* the DS runs anything — closing the loop to where they discovered it.

### Component 7 — Code rendering (audit)

A pure function `render(spec) → PySpark string` (and a SQL variant) produces the exact code the engine runs for a feature. Used for audit/review; never executed from the string (the engine runs the interpreted plan directly). Guarantees "show your work" without N maintained files.

---

## Data flow (worked example: `cross_border_value_ratio_90d`, `business_dt = 2026-07-22`)

1. `raw.ftr_transactions` (Parquet) → 2. `int.transactions_typed` (dates parsed, amounts cast, `is_cross_border` derived) → 3. `prm.customer_txn_facts` (canonical per-txn at cif grain) → 4. `compute_feature_group('aml.customer.daily', business_dt)` computes this feature **and every other customer-grain daily AML feature** in one 90d-window pass → `fea.aml_customer_daily` → 5. `assemble_model_input` → `aml_customer_features` (Hive), the feature as one column keyed by `(cif_id, business_dt)` → 6. `profile_features` writes its distribution/min/max/freshness → catalog search.

Rendered PySpark (audit view) for this feature:
```python
w90 = Window.partitionBy("cif_id").orderBy(F.col("tran_date").cast("long")).rangeBetween(-90*86400, 0)
df = (txn
  .withColumn("is_cross_border", F.col("counter_party_bank_cntry") != F.lit("AE"))
  .withColumn("xb_amt", F.when(F.col("is_cross_border"), F.col("tran_amt_aed")).otherwise(0.0))
  .withColumn("num_90d", F.sum("xb_amt").over(w90))
  .withColumn("den_90d", F.sum("tran_amt_aed").over(w90))
  .withColumn("cross_border_value_ratio_90d",
              F.when(F.col("den_90d") > 0, F.col("num_90d")/F.col("den_90d"))))
```

---

## Key decisions

1. **`business_dt` density = full periodic snapshot per entity** (configurable cadence), not active-only. Simple, predictable, complete history for retraining; it is the biggest cost lever, so it is a single config (`snapshot_scope: all | active`) that can be flipped per domain.
2. **Batch by `(grain, cadence)` first, priority second.** Two features sharing grain+cadence MUST share a pass; priority only orders groups.
3. **Option C (spec-driven + rendered code)**, not per-feature generated files — efficiency + auditability.
4. **Formula authoring = LLM author (LLM 1) + independent LLM critic (LLM 2) + deterministic validators/compiler as final authority.** The critic is a critic, NOT a validator — "two models agreed" is never accepted as correctness. LLM 1 has read/validate-only tools; runtime is LLM-free. Accepted formulas are frozen + versioned; validated free-form formulas are promotable into reusable recipes.
5. **One table per grain**, routed by the spec's `entity`.
6. **Offline/batch only** — no online store in this spec.

## Error handling

- A spec that references a column not in the source, or an unsupported operation → the group fails that feature (recorded in metrics with a reason), and continues the other features in the group (one bad spec must not sink the batch).
- A Spark job failure → `on_pipeline_error` records the run as failed with the stage; the `model_input` write is transactional per `business_dt` partition (a failed run leaves no partial partition).
- Missing source data for a `business_dt` → the group is skipped for that date with a recorded reason, not a silent empty write.

## Testing

- **Spec interpretation:** unit tests that `compute_feature_group` turns a spec into the correct Spark expression (golden expected values on a tiny fixture DataFrame), including window boundaries (no leakage: a row at `business_dt` sees only `tran_date ≤ business_dt`).
- **Rendering:** `render(spec)` output parses and, run on the fixture, produces identical values to the interpreted engine (render ≡ execute).
- **Routing:** a spec's `entity` lands it in the correct grain table.
- **Hooks/metrics:** a run emits the expected metric rows.
- **Profiling:** stats match hand-computed values on the fixture.
- Local runs use Spark local mode + small Parquet fixtures; no cluster needed for tests.

## Phasing (for the implementation plan)

- **Phase 1 (MVP — prove the authoring workflow + one feature end-to-end):**
  1. Typed-formula dataclass + `sum/count/ratio` + trailing window; deterministic structural validation.
  2. LLM 1 (author, restricted ReAct tools over existing catalog reads) generates the formula.
  3. LLM 2 (independent critic) critiques it → structured findings.
  4. Deterministic governance compiler decides `RESOLVED / NEEDS_REVIEW / REJECTED` (reuse the gauntlet + two-axis gate).
  5. **Gold check:** compare the generated formula against a curated *expected* formula for the first few gold features (B-Gate-1-style harness).
  6. Materialize ONLY after all required checks pass: `raw→…→model_input`, `compute_feature_group`, one Hive `aml_customer_features` table, basic hooks — proving `cross_border_value_ratio_90d` end-to-end on a Spark-local fixture.
  7. **Freeze** the accepted formula as an immutable, versioned asset.
- **Phase 2:** code renderer + render≡execute test; profiling node + `feature_stats`; run-metrics table.
- **Phase 3:** multi-grain routing (customer×product, account); recipe-authoring flow (author-once + freeze) and free-form→recipe promotion; group→schedule map emission.
- **Phase 4:** write-back of stats into catalog search; additional operations (zscore, delta, proportion); `snapshot_scope: active`.
