# Feature Materialization Pipeline — Design Spec (rev 2)

**Goal:** Turn a governed, design-checked feature definition into an **immutable, versioned formula artifact** and a production PySpark pipeline that computes it (and its compatible siblings) point-in-time-correctly at scale, lands results in **atomically-versioned** per-grain tables keyed by `(entity…, business_dt)`, and profiles them under read-scope back into a feature-search projection.

**One line:** The catalog decides *what* a feature is; this system authors the *how* (a governed typed formula), freezes it as an immutable artifact, and executes it deterministically and LLM-free.

**Tech stack:** Kedro (nodes/pipelines/hooks), PySpark (compute), an **atomic table format — Apache Iceberg** (snapshot isolation, partition-level commits, time-travel; Delta/Hudi acceptable) for feature/model_input layers, HDFS Parquet for raw/intermediate/primary. New module; integrates with the catalog through an **explicit protocol** (§8), not a loose seam.

> **rev-2 note.** This revision makes first-class what rev-1 left implied: the immutable materialization identity (§3), the formula→planner adapter and single IR (§2), the point-in-time model (§5), validation-vs-production dispatch (§6), atomic storage + restatement (§7), and the external protocol (§8). Findings 1-15 from review are each resolved and cited inline as `[Fn]`.

---

## Global Constraints

- **Canonical operand identity is the `logical_ref`: `source::schema.table[.column]`** — the `::` scheme separator is load-bearing and round-trippable (`object_ref.py:23`). Formulas MUST use it verbatim; no alternate identity format. `[F2]`
- **Grain = `(entity key(s) …, business_dt)`**, exactly one row per entity per snapshot date. The window is a compute input, not grain. Every materialization node MUST reduce to one row per grain key. `[F5]`
- **Point-in-time = knowledge time, not just event time.** A feature for `business_dt = D` may read only facts *known as of D* (availability/ingestion ≤ D), not merely `tran_date ≤ D`. `[F5]`
- **Governance status is server-derived and drift-aware — never a field in the formula/spec.** The formula YAML/JSON carries NO `status`. Dispatch reads the current contract status at run time. `[F6]`
- **Runtime is deterministic and LLM-free.** LLMs author formulas offline; execution compiles a frozen artifact.
- **Every production value is attributable** to `(feature_version_id, business_dt, materialization_revision, run_id, input_snapshot_ids, formula_hash, compiler_version)`. Late corrections **append a new revision**; historical values are never silently rewritten. `[F7]`

---

## §1 Typed formula + authoring pipeline (LLM authors, deterministic governs)

**Principle:** the second LLM is a **critic, not a validator** — two models agreeing is not correctness. Final authority is deterministic checks + a human policy (§4).

The **typed formula** names exact `logical_ref` operands and typed operations only — never SQL/PySpark:

```json
{
  "operation": "ratio",
  "grain": {"entity": "customer", "keys": ["ftr::public.comp_financial_tran_repos_dly.cif_id"]},
  "window": {"type": "trailing", "length": 90, "unit": "day"},
  "time_operand": "ftr::public.comp_financial_tran_repos_dly.tran_date",
  "numerator":   {"aggregation": "sum", "operand": "ftr::public.comp_financial_tran_repos_dly.tran_amt_aed",
                  "filter": {"operation": "not_equal",
                             "left": "ftr::public.comp_financial_tran_repos_dly.counter_party_bank_cntry",
                             "right_parameter": "home_country"}},
  "denominator": {"aggregation": "sum", "operand": "ftr::public.comp_financial_tran_repos_dly.tran_amt_aed"},
  "zero_denominator": "null",
  "parameters": ["home_country"]
}
```

Note: operands use `::`; parameters are **declared but not valued** here (values live in a versioned deployment binding, §10). No `status`, no bare columns, no free-text filter. `[F2][F6][F9][F10]`

**Pipeline** (unchanged shape from rev-1, corrected authority):

```
intent (free-form) OR recipe  →  LLM-1 AUTHOR (ReAct; read/validate tools only)
  →  deterministic STRUCTURAL validation  (operation vocab · every operand a real logical_ref · operand completeness)
  →  LLM-2 INDEPENDENT CRITIC (structured findings; different tier; no shared context; no rewrite)
  →  §2 GOVERNANCE COMPILER (formula→planner adapter → typed IR → governed verdict)
  →  RESOLVED / NEEDS_REVIEW / REJECTED  →  §4 human policy  →  §3 FREEZE as immutable artifact
```

LLM-1 tools (read/validate only; never approve/execute/mutate governance): `search_columns`, `get_column_metadata`, `get_governed_grain`, `get_time_anchor`, `get_verified_lineage`, `list_supported_operations`, `validate_draft_formula`. These are **seven catalog-integration surfaces** — the integration is a defined protocol (§8), not "two seams". `[F8]`

Recipe: authored once, frozen (§3), never regenerated at runtime. Free-form: same pipeline at proposal; on approval, promotable into a recipe.

## §2 Formula → planner adapter + single compiler IR (the hard component, first-class) `[F3][F14]`

Today `compile_contract` consumes `BindingPlanV1 + Template` and multi-source consumes `MultiSourcePlannerIntentV1`; **none consumes a formula AST / filter tree / numerator / denominator / zero-denominator**. So this is a NEW, explicit adapter, not "reuse":

```
TypedFormulaV1  →  FormulaPlannerIntentV1  →  (existing governed binding + physical-plan machinery)  →  FormulaExecutionIRV1
```

- `FormulaPlannerIntentV1` translates the formula's operands/operations into the planner's binding intent, **preserving exact logical_refs** so governance (resolve_fact, approved_join, lineage) applies unchanged.
- `FormulaExecutionIRV1` is the **single compiled IR**. Both the Spark executor and the code renderer consume it — we do NOT independently implement "interpret" and "render". We persist the **IR hash + the Spark logical plan**; the rendered text is *equivalent audit output*, not the literal executed code. `[F14]`
- The governance compiler over the IR performs the existing gauntlet checks (types/units/currency, grain/cardinality, VERIFIED joins/lineage, time-anchor/leakage, null & divide-by-zero, authorization, freshness) and emits `RESOLVED / NEEDS_REVIEW / REJECTED`.

## §3 Immutable materialization identity `[F1]`

The frozen formula is an **immutable artifact referenced from `feature_versions.required_artifact_refs`** (`0060_aggregates_lifecycle.sql`, write-once trigger) — NOT a separate YAML versioning system. The artifact binds:

`feature_id` · `feature_version_id` · `contract_id` + contract version · metadata snapshot/fingerprint · planner declaration + physical-plan IDs · formula-schema + operation-policy + compiler versions · **formula content hash** · **deployment-configuration hash**.

Two identical formulas → same content hash → same artifact; any change (operand, param binding, compiler version) → new immutable version. This is what "freeze + version" means, concretely.

## §4 Human-confirmation policy (LLM agreement never authorizes production) `[F4]`

The human approved a feature *idea/prose contract*, not necessarily this exact numerator/denominator/filter/temporal interpretation. Confirmation is the governance gate (`contract/govern.py`). One explicit policy per feature class:

- **Novel free-form formula → human confirms** the exact typed formula before freeze.
- **Approved reusable formula template → deterministic bindings may reuse it** without re-confirmation.
- **Low-risk formulas → auto-freeze ONLY under an approved auto-resolution policy** (declared risk tier + operation on an allow-list).
- **High-risk / critic-disputed / novel-operation → always human review.**

`RESOLVED` from the compiler routes into this policy; it does not go straight to freeze.

## §5 Point-in-time model `[F5]`

Correctness requires modelling knowledge time, not just event time:

- **Event time vs availability/knowledge time** — the feature sees a row only if its *availability/ingestion* timestamp ≤ `business_dt`, not merely `tran_date ≤ business_dt`.
- **Posting/ingestion timestamps, source timezone + business-day cutoff, backdated txns & reversals, SCD2 joins, a late-arrival restatement horizon.**
- **An entity × business_date spine** drives the output: the compute LEFT-JOINs windowed aggregates onto the spine and **reduces to exactly one row per `(entity…, business_dt)`** (rev-1's rolling-per-row example was wrong).

## §6 Validation vs production execution (separate) `[F6]`

A DESIGN-CHECKED / NEEDS_EXTERNAL_VALIDATION formula may run only to *earn evidence*, never to land in production:

- **Validation run:** current contract + requirements → isolated output → external evidence.
- **Production run:** ACTIVE feature version + required stamp → production store.

Dispatch reads the **current drift-aware contract status server-side immediately before dispatch** (drift recheck), not any status baked into an artifact. `[F8]`

## §7 Storage, retry, restatement `[F7][F13]`

Plain Parquet has no atomic partition replacement → adopt **Apache Iceberg** (snapshot isolation, atomic commits, time-travel). Every commit records: `feature_version_id, business_dt, materialization_revision, run_id, attempt, input_snapshot_ids, formula_hash, compiler_hash, commit_status, superseded_revision`. Late-data corrections **append an auditable new `materialization_revision`**; old values remain for reproducibility. Tables are **versioned physical group tables/snapshots with an active wide VIEW** — a new formula version never overwrites a live column's semantics while old models depend on it. `[F13]`

## §8 External-platform protocol `[F8]`

The catalog ↔ materialization contract is explicit: authenticated materialization-request endpoint/event · idempotency key · immutable-artifact retrieval + hash verification · run acceptance/status/failure protocol · retry/timeout/cancellation rules · frozen input snapshots · result manifest + authenticated attestation · outbox/reconciliation when either side is down · external-requirement-result ingestion · **drift recheck immediately before dispatch**. (This is also where the seven LLM-1 tool surfaces are governed.)

## §9 Batching by execution signature (not by grain/cadence) `[F11][F12]`

Features sharing `(domain, grain, cadence)` may still differ in source, joins, temporal basis, or window — unsafe to co-batch. Batch by an **execution signature**: `physical_plan_shape + landing_grain + PIT_basis + compatible_source_snapshot + cadence`. Each formula is **precompiled independently**; structurally-invalid formulas are excluded *before* the shared Spark plan is built. A runtime Spark failure fails the whole group transaction; the retry **quarantines the failing formula** and rebuilds the group. `[F12]`

## §10 Deployment binding + parameters `[F9][F10]`

The immutable formula declares parameters (`home_country`, thresholds) but does not value them. A **versioned deployment/run binding** supplies values and **contributes to the run hash** — a parameter change is a new run identity and can change feature values. Cadence, resources, and target routing live in this binding, not in the formula. `[F9][F10]`

## §11 Profiling under privacy/authority `[F15]`

Histograms/min/max/distinct can leak restricted data. Feature stats are **versioned, classification-tagged, read-scoped, and run-tied**. Since `search()` only covers `graph_node` (`search.py:133`), feature discovery gets its **own feature-search projection** (not shoehorned into graph_node) that respects read scope.

---

## Layers & worked example

`raw → intermediate → primary → feature → model_input`; raw/int/prm = HDFS Parquet, feature/model_input = Iceberg. For `cross_border_value_ratio_90d` at `business_dt=D`: spine(cif × D) LEFT-JOIN a 90d-trailing, availability-filtered aggregate over `prm.customer_txn_facts`, one row per `(cif_id, D)`, committed as an Iceberg revision, attributable to its frozen formula artifact.

## Error handling

Invalid formula → excluded pre-batch, recorded with reason (§9). Spark failure → group transaction fails, retry quarantines the culprit (§9). Missing source for a `business_dt` → skipped with reason, no partial commit (§7). Drift at dispatch → held, not run (§6/§8).

## Testing

Formula→IR golden tests (window boundaries prove no look-ahead AND availability ≤ D); render ≡ execute over the same IR; routing lands a feature in the correct grain table; gold-formula comparison vs curated expected formula for first features (B-Gate-1-style); Iceberg commit/restatement appends a revision (never overwrites); profiling read-scope respected. Spark local mode + tiny fixtures.

## Phasing (revised)

1. **Formula-authoring shadow** — LLM author + independent critic + typed AST + deterministic structural validation + gold-set scoring. **No execution.**
2. **Governance compiler** — formula→planner adapter (§2), exact operand preservation, authoritative metadata reads, typed execution IR (§2), immutable artifact identity (§3), human-confirmation policy (§4).
3. **Materialization walking skeleton** — one frozen formula, entity-date spine + PIT model (§5), atomic versioned Iceberg output + retry/restatement (§7).
4. **External round trip** — authenticated request, run manifest, attestation, requirement-result ingestion, DATA-CHECKED promotion (§6/§8).
5. **Batching & scale** — execution-signature compatibility (§9), multiple features, multiple grains.
6. **Profiling & product** — privacy-controlled stats + feature-search projection (§11).
