# Feature Materialization Pipeline — PARENT ARCHITECTURE (rev 3)

> **This is the parent architecture, NOT an implementation-ready spec.** It names the mechanisms and fixes the cross-cutting contradictions; the *precise* contracts (formal grammar, temporal policy, compiler I/O, run state machine, storage schema) live in the **child specs** (see *Program Decomposition*). No single implementation plan is authorized against this document — each child spec gets its own spec → plan → build cycle.


**Goal:** Turn a governed, design-checked feature definition into an **immutable, versioned formula artifact** and a production PySpark pipeline that computes it (and its compatible siblings) point-in-time-correctly at scale, lands results in **atomically-versioned** per-grain tables keyed by `(entity…, business_dt)`, and profiles them under read-scope back into a feature-search projection.

**One line:** The catalog decides *what* a feature is; this system authors the *how* (a governed typed formula), freezes it as an immutable artifact, and executes it deterministically and LLM-free.

**Tech stack:** Kedro (nodes/pipelines/hooks), PySpark (compute), **Apache Iceberg** (the reference commit/merge/time-travel/catalog contract — chosen, not hedged; Delta/Hudi are future adapters behind a tested capability interface, `[F16]`) for feature/model_input layers, HDFS Parquet for raw/intermediate/primary. New module; integrates with the catalog through an **explicit protocol** (§8), not a loose seam.

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
  →  §2 GOVERNANCE COMPILER (formula→planner adapter → typed IR → governed checks)
  →  §3b status axes → materialization_eligibility  →  §4 human policy  →  §3 FREEZE (artifact/version/binding)
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

## §3 Immutable identity — three separated entities, four hashes `[F1][F2 rev]`

rev-2's single "artifact contains feature_version_id" was **circular** — `mint_feature_version()` (`feature_versions.py:44`) generates the id internally and REQUIRES `required_artifact_refs` + `content_hash` in the same insert, so the artifact cannot contain the version id. Separate three entities:

- **`FormulaDefinitionArtifact`** — content-addressed by `formula_content_hash`; carries the canonical `TypedFormulaV1` + formula-schema/operation-policy/compiler versions. **No `feature_version_id`, no feature/contract id.** Two identical formulas across different features share this artifact.
- **`FeatureVersion`** (existing `feature_versions`, write-once) — points to `contract_id` **and** the `FormulaDefinitionArtifact` (via `required_artifact_refs`). *Prerequisite:* migration 1011's `contract_id` is DDL-only today — `mint_feature_version` must be wired to write/load it (a child-spec task).
- **`MaterializationBinding`** — points to `feature_version_id` + environment + semantic/operational parameter values + target + cadence.

Four distinct hashes (rev-2 conflated them):

| hash | over |
|---|---|
| `formula_content_hash` | canonical `TypedFormulaV1` only |
| `formula_binding_hash` | formula + contract/version/provenance |
| `deployment_binding_hash` | feature version + environment + **semantic** parameter values |
| `execution_hash` | deployment binding + `business_dt` + input snapshot ids + compiler |

The exact transaction/outbox sequence that creates artifact → feature version → binding atomically is a **child-spec (Program #2) contract**.

## §3b Status axes (do NOT collapse into one verdict) `[F3]`

`RESOLVED / NEEDS_REVIEW / REJECTED` wrongly folds independent axes (and `NEEDS_REVIEW` is a *workflow* state, not a validation verdict). Track them separately and derive eligibility deterministically:

`structural_status` · `critic_status` · `planner_status` · `contract_resolution_status` · `external_validation_status` · `technical_status` · `human_review_status` → **`materialization_eligibility`** (a pure function of the above; the ONLY gate execution reads). The exact axis enums + the derivation live in child-spec Program #2.

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

## Program Decomposition — child specs (each its own spec → plan → build)

This parent is not implemented directly. Seven child specs turn its named mechanisms into precise contracts. Order is dependency-driven; the first is safe (no execution).

| # | Child spec | Scope / the precise contracts it must define | Carries findings |
|---|---|---|---|
| 1 | **TypedFormulaV1 authoring** | Formal `TypedFormulaV1` schema + closed grammar (operation/aggregation enums, ordered operands, boolean filter AST + nesting limits, typed literals/params, decimal/overflow, null/empty-window/÷0, window inclusivity, output type/unit/currency/additivity, grain roles, multi-source operands, complexity bounds, canonical serialization + hashing); the 7 LLM-1 tools as a **catalog-authoring API** (read scope, metadata-only egress, prompt-injection handling, model/prompt/schema versions, bounded ReAct iterations, token/cost budget, raw-response capture, deterministic replay); independent critic; gold-set gate. **No execution.** | 4, 13 |
| 2 | **Formula authority, identity & lifecycle** | The three separated entities + four hashes (§3); the atomic artifact→feature-version→binding create sequence + outbox; wiring `mint_feature_version` to `contract_id`; the status axes → `materialization_eligibility` derivation (§3b); the append-only human-confirmation decision event + separation of duties (author ≠ auto-resolver ≠ activator); template-reuse policy (reuse only when every role binds through accepted authority within the template's approved policy); the **semantic/operational/run parameter split**. | 1, 2, 3, 10, 11, 14 |
| 3 | **Formula → planner adapter & IR** | `TypedFormulaV1 → FormulaPlannerIntentV1 → governed physical paths → FormulaExecutionIRV1`; C1-authoritative reads for every `logical_ref`; **exact operand preservation assertion** (all computation + structural + filter/grain/time/grouping refs pinned and checked post-plan); fail-closed on fork / hash_mismatch / projection_unavailable / not_operational / drift. One IR consumed by executor AND renderer. | 6, 14 |
| 4 | **`TemporalPolicyV1` & PIT computation** | Versioned temporal policy: event_time_ref, availability_time_ref/basis, source tz, business-day cutoff, window start/end inclusivity, SCD effective/system-time, reversal policy, late-arrival horizon, restatement policy; `business_dt` → exact cutoff instant; entity-date spine → one row per grain; **fail into a typed external requirement** if no trustworthy availability basis/snapshot. | 5 |
| 5 | **Materialization protocol, Iceberg schema & restatement** | Iceberg physical schema (does a row carry `materialization_revision`; uniqueness check; active-revision selection); the run **state machine** `REQUESTED→ACCEPTED→RUNNING→COMMITTED / FAILED│CANCELLED│STALE_INPUT`; multi-write atomicity across data commit + run manifest + active-revision pointer + stats + callback (Iceberg isolation is table-local); **quarantine by bounded bisection / isolated rerun** (never auto-blame a formula), technical_failure ≠ semantic rejection. | 7(sm), 8, 9, 12 |
| 6 | **External attestation & requirement round-trip** | The versioned request/result schemas; idempotency-key derivation; actor/service authority; artifact+deployment hash verification; callback auth; duplicate/out-of-order/cancellation-race handling; result manifest + attestation verification; reconciliation after partial failure; **frozen-input-snapshot binding at the executor** (drift check alone doesn't close the dispatch→exec race); external-requirement-result ingestion → DATA-CHECKED promotion. | 7(schema), 8 |
| 7 | **Batching, profiling & feature search** | Execution-signature compatibility (plan+grain+PIT-basis+snapshot+cadence); per-formula independent precompile; the read-scoped, classification-defaulted **feature-stats** contract (restricted min/max/histograms never leave the platform; catalog gets allow-listed summaries only) + the dedicated **feature-search projection** (separate from `graph_node` search). | 11, 15 |

**Start point:** Child #1 (authoring shadow) — no execution, no storage, no external surface; it proves the LLM-author→critic→structural-validate→gold-gate loop in isolation, and it's the input everything else consumes.
