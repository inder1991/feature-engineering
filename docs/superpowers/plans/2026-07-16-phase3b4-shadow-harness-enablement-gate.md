# Phase 3B.4 — Shadow Harness + Objective 3C Enablement Gate Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`). **D3 (dual input hashes + snapshot fix) + D8 (statistics + detached-signed gate) are the highest-scrutiny tasks.** Shadow/telemetry-gated; nothing enforces.
>
> **v2 folds a 20-finding plan review + a REBASE onto current `origin/main` (`2e1e5d8`).** The branch was stale (based on `b9131ea`); migrations now go to `0997` on main, so the migration is **`0998`**. Other fixes: compile-off traffic can't pass as `not_applicable`; the planner hash must hash *discovery's actual* PG read (not the ctx snapshot); `planner_input_hash` is persisted; a fallback-write failure is caught internally (never rolls back the manifest); the loss signal is durable manifest-reconciliation, not a process-local counter; full CHECK/JSON-integrity DDL; the trust-rooted detached signature; and reordered tasks so each commit stays green.

**Goal:** Persist each 3B.3c contract classification (manifest + run + plan, WORM), fingerprint it for replay, cause-label the population, and produce the objective, detached-signed **conjunctive gate** governing 3C enablement.

**Tech Stack:** Python 3.11, PostgreSQL (append-only/WORM), pytest (`db` fixture), `cryptography 49.0.0` (already a dep) / stdlib `hmac`. `uv run pytest/ruff/mypy`. **Spec:** `docs/superpowers/specs/2026-07-16-phase3b4-shadow-harness-enablement-gate-design.md` (v3).

## Global Constraints (every task)

- **Shadow / telemetry-gated / behaviour-neutral.** A NEW `FEATUREGEN_INTENT_SHADOW_TELEMETRY` flag (separate from the compile flag) gates persistence. Both off → **zero writes, response byte-identical**. Flags read ONLY in the route (no `os.environ` in `planner/`). Full `tests/featuregen` + `tests/db` + `tests/featuregen/db` green.
- **Append-only / WORM / integrity.** All tables `REVOKE UPDATE, DELETE, TRUNCATE FROM featuregen_app` (mirror `0974`); composite PK/FK; **CHECK on EVERY enum column** (incl. path/contract/declaration statuses + tier); **count consistency CHECKs** (`compiled_count + skipped_count = path_resolved_eligible`, nonnegative; `incomplete_reason` NULL unless `compile_status='incomplete'`); idempotent inserts (`ON CONFLICT` + divergent-duplicate validation → raise); **payload hash + `jsonb_typeof=object` CHECK on every JSON column**; canonical sorted-key JSON; key on `generation_run_id`.
- **Capture integrity is provable + non-circular.** The manifest (expected set) is written FIRST, before `resolve_catalog_scope`, WITHOUT `catalog_scope_id`, on its own commit-surviving write. A pre-loop failure AND a persistence failure are caught INSIDE `run_shadow_planner` (return normally) so the route savepoint never rolls back the manifest. The durable loss signal is **manifest↔results reconciliation** (DB-native), never a process-local counter.
- **Hashes are INPUTS-ONLY and hash the DATA ACTUALLY READ.** Discovery reloads columns from PG independently of the context (`candidates.py:41`) — the plan must hash discovery's real inputs (D3). `planner_input_hash` (full universe → selection stability) is PERSISTED on `run_result`; `contract_input_hash` (selected plan → verdict stability) on the observation; declarations/verdicts are OUTPUTS, hashed separately.
- **The gate is conjunctive** (every sub-gate; no averaging), **detached-signed with an EXTERNAL trust root** (a keyed HMAC/ed25519 signer; the public key/keyring comes from config/secrets, NEVER embedded in the artifact), **no signed-exclusion override in v1**, **human cannot override a FAILED machine gate**, **nonzero exit on failure**.
- **Convention:** frozen dataclasses; lowercase-snake StrEnum; ruff (scope to touched files); no import cycle (`planner/*` ⊄ route). Branch `feature/phase3b4-shadow-harness` (rebased on `2e1e5d8`); harness default trailer.

## Reused interfaces (verified against `2e1e5d8`)
- Migrations: `db/migrations/NNNN_*.sql`, runner auto-discovers; **current max = `0997_graph_structural_constraints` → next = `0998`**. WORM/CHECK/`DO $$ … REVOKE … $$` from `0974`. Per-migration test pattern: `tests/featuregen/db/test_migration_0997.py`; suite `tests/db/test_migrations.py`.
- Shadow entry: `run_shadow_planner(conn, *, eligible_recipe_ids, target_entity, roles=(), run_id, now, compile_contracts=False, templates=None)`; route call in `contract.py` inside `if body.catalog_source is None and scope.target_entity is not None:` — `applicability.eligible_ids` + `generation_run_id` + versions + `identity.role_claims` + `now` in scope; compile flag read here.
- Discovery snapshot: `plan.py:121` calls `discover_ingredient_candidates(conn, template, src, roles=roles)` — its own `_load_columns` (`candidates.py:41`), NOT `ctx.columns_by_catalog`. **F5 fix required** (below).
- Compile ctx: `build_compiler_context(conn, scope, roles, now) -> CompilerContext(columns_by_catalog, catalog_fingerprint_at_start, bridge_fingerprint_at_start, agg_declarations, …)`; `ctx.agg_declarations` (`declarations.py:105,521`) IS a verdict input → must be in `contract_input_hash` (F13). `_Col.additivity/is_as_of/entity/sensitivity/concept/is_grain/data_type`.
- Stamp sites: `CatalogStateStampV1(…)` built at `scope.py:42` AND `declarations.py:786` (revalidate_freshness) + used in `audit_envelope` — D3 must touch all (F14). `BoundingMetricsV1` (`contracts.py:507`, truncation fields) is on the result → persist + gate (F8).
- Budget: `budget.remaining > 0 and compile_ctx.now < budget.deadline` (`plan.py:206`) — inert wall-time (F12/F17). Signing precedent: `security/audit.py` (`hmac`, key from `FEATUREGEN_AUDIT_HMAC_KEY` via `get_settings()`, `verify_chain`) — the external-trust-root model for F10.

## File Structure

| File | Task |
|---|---|
| `db/migrations/0998_planner_shadow_store.sql` (CREATE) | D1 |
| `planner/contracts.py` (MODIFY — new reason codes incl. `aggregation_ordering_column_missing`, `compile_disabled`; `ReplayFreshness.incompatible`; enrich `CatalogStateStampV1`; real `ROLE_RESOLUTION_VERSION`) | D1 |
| `planner/shadow_store.py` (CREATE — store contracts, two-phase writer, reconcile) | D1/D2 |
| `planner/shadow.py`, `api/routes/contract.py` (MODIFY — telemetry flag, manifest, internal catch) | D2 |
| `planner/candidates.py`, `plan.py`, `fingerprint.py` (MODIFY/CREATE — discovery-from-ctx + dual hashes) | D3 |
| `planner/scope.py`, `declarations.py` (MODIFY — stamp enrichment sites) | D3 |
| `planner/replay.py` (CREATE) | D4 |
| `planner/cause.py` + `shadow_review.py` (CREATE — taxonomy + Gate-2b review-artifact contract) | D5 |
| `planner/declarations.py`, `shadow.py` (MODIFY — monotonic budget, take_latest) | D6 |
| `planner/contract_gold.py` + `contract_eval.py` + `strata.py` (CREATE) | D7 |
| `planner/shadow_report.py` + `signing.py` (CREATE) | D8 |
| Tests | `tests/featuregen/db/test_migration_0998.py` + `test_shadow_*.py` + a PG e2e |

---

### Task D1: Migration 0998 + contract/enum additions + store contracts + two-phase writer

**Files:** Create `db/migrations/0998_planner_shadow_store.sql`, `planner/shadow_store.py`, `tests/featuregen/db/test_migration_0998.py`; Modify `contracts.py`; Test `test_shadow_store.py`.

Contracts/schema come FIRST so downstream tasks stay green (F16). Adds in `contracts.py`: reason code `aggregation_ordering_column_missing` (NOT `compile_disabled` — that is a `CompileStatus`, not a `ReasonCode`; LOW-1); `ReplayFreshness.incompatible`; the store enums (`PlannerOutcome`/`CompileStatus`{`complete`,`incomplete`,`not_applicable`,`compile_disabled`}/`IncompleteReason`{`budget_count`,`budget_time`,`error`}/`CaptureStatus`); enrich `CatalogStateStampV1` (+`compiler_input_fingerprint`, `projection_checkpoint`, both defaulting so existing constructors still compile); **the real `ROLE_RESOLUTION_VERSION` change lands HERE (D1 only — not D3); it ripples into `scope_id`/`planner_input_hash`, verified green (no test pins a literal hash).**

- [ ] **Step 1: confirm `0998`** — `git ls-tree -r --name-only origin/main -- src/featuregen/db/migrations/ | grep -oE '[0-9]{4}' | sort -n | tail -1` → +1.
- [ ] **Step 2: the migration** — three `CREATE TABLE IF NOT EXISTS` with FULL constraints (F11/F12):
  - `planner_shadow_dispatch(generation_run_id text PRIMARY KEY, eligible_recipe_ids text[] NOT NULL, recipe_hash text NOT NULL, expected_count int NOT NULL CHECK (expected_count >= 0), invocation_predicate text NOT NULL, compile_flag bool NOT NULL, telemetry_flag bool NOT NULL, applicability_version text NOT NULL, producer_commit text NOT NULL, compiler_versions jsonb NOT NULL CHECK (jsonb_typeof(compiler_versions)='object'), compiler_versions_hash text NOT NULL, payload_schema_version text NOT NULL, created_at timestamptz NOT NULL)`.
  - `planner_shadow_run_result(generation_run_id text NOT NULL REFERENCES planner_shadow_dispatch ON DELETE CASCADE, recipe_id text NOT NULL, catalog_scope_id text, planner_input_hash text, planner_outcome text NOT NULL CHECK (planner_outcome IN ('compiled','no_physical_plan','internal_error','no_authorized_catalog','template_not_found','preloop_failure')), compile_status text NOT NULL CHECK (compile_status IN ('complete','incomplete','not_applicable','compile_disabled')), incomplete_reason text CHECK (incomplete_reason IN ('budget_count','budget_time','error')), path_resolved_eligible int NOT NULL CHECK (path_resolved_eligible >= 0), compiled_count int NOT NULL CHECK (compiled_count >= 0), skipped_count int NOT NULL CHECK (skipped_count >= 0), capture_status text NOT NULL CHECK (capture_status IN ('persisted','persistence_partial')), selected_contract_physical_plan_id text, selected_contract_id text, contract_result_status text, bounding jsonb NOT NULL CHECK (jsonb_typeof(bounding)='object'), payload_schema_version text NOT NULL, created_at timestamptz NOT NULL, PRIMARY KEY (generation_run_id, recipe_id), CHECK (compiled_count + skipped_count = path_resolved_eligible), CHECK ((incomplete_reason IS NULL) = (compile_status <> 'incomplete')))`.
  - `planner_shadow_plan_observation(generation_run_id text NOT NULL, recipe_id text NOT NULL, physical_plan_id text NOT NULL, path_resolution_status text NOT NULL, is_compiled bool NOT NULL, contract_id text, contract_input_hash text, contract_resolution_status text, declaration_status text, contract_primary_reason_code text, contract_reason_codes text[] NOT NULL DEFAULT '{}', bridge_count int NOT NULL CHECK (bridge_count >= 0), tier text NOT NULL, preference_rank int NOT NULL, declarations jsonb, declarations_output_hash text, replay_stamp jsonb, payload_schema_version text NOT NULL, created_at timestamptz NOT NULL, PRIMARY KEY (generation_run_id, recipe_id, physical_plan_id), FOREIGN KEY (generation_run_id, recipe_id) REFERENCES planner_shadow_run_result ON DELETE CASCADE, CHECK (is_compiled = (contract_input_hash IS NOT NULL)), CHECK (is_compiled = (replay_stamp IS NOT NULL)), CHECK (is_compiled OR contract_id IS NULL))`. **Compile-only fields NULLABLE for tier-1/rejected/compile-off candidates (F3); `is_compiled` is the cross-field guard.** `is_selected` is DERIVED (read-time join), never a column.
  - **ENUM + JSON CHECKs (HIGH-1 — the plan's "CHECK on every enum column" invariant; transcribe the exact StrEnum values from `contracts.py`, nullable cols as `col IS NULL OR col IN (...)`):** on `plan_observation` add `CHECK (path_resolution_status IN ('ingredient_binding_only','source_to_target_resolved','source_to_target_rejected'))`, `CHECK (contract_resolution_status IS NULL OR contract_resolution_status IN ('resolved','not_compiled','unresolved_ingredient_connectivity','unresolved_aggregation_declaration','unresolved_temporal_declaration','unresolved_safety_evaluation','safety_rejected','unresolved_freshness'))`, `CHECK (declaration_status IS NULL OR declaration_status IN ('not_compiled','resolved','unresolved_ingredient_connectivity','unresolved_aggregation_declaration','unresolved_temporal_declaration','unresolved_safety_evaluation','safety_rejected'))`, `CHECK (tier IN ('tier_1_single_catalog','tier_2_one_bridge','tier_3_multi_bridge'))`, `CHECK (declarations IS NULL OR jsonb_typeof(declarations)='object')`, `CHECK (replay_stamp IS NULL OR jsonb_typeof(replay_stamp)='object')`. On `run_result` add `CHECK (contract_result_status IS NULL OR contract_result_status IN (<the 8 ContractResolutionStatus values>))`. (Re-confirm the value sets against `contracts.py` at implement time — a drifted enum must update the CHECK.)
  - Indexes on `(generation_run_id)`, `(recipe_id)`, `(contract_input_hash)`; the WORM `DO $$ … REVOKE UPDATE, DELETE, TRUNCATE ON <each> FROM featuregen_app … $$`.
- [ ] **Step 3: `test_migration_0998.py`** (mirror `test_migration_0997.py`) — the migration applies; the tables/constraints exist; a WORM check (assert the REVOKE grants are absent for a non-superuser role, or — under the superuser test cluster — that the `DO` block ran and the CHECK constraints reject bad rows). CHECK-violating inserts are each rejected: a count mismatch (`compiled_count + skipped_count ≠ path_resolved_eligible`); `is_compiled=true` with NULL `contract_input_hash`; `incomplete_reason` set when `compile_status≠'incomplete'`; **an out-of-domain enum** (a bogus `path_resolution_status`/`tier`/`contract_resolution_status`); a non-object JSON payload.
- [ ] **Step 4: `shadow_store.py`** — enums + row dataclasses; `_canonical_json`/`_payload_hash`; `write_dispatch`; **`write_run_and_plans(conn, run_result, observations) -> CaptureStatus`** the two-phase protocol (atomic; on failure roll back + minimal-parent `persistence_partial` insert on a fresh savepoint; if THAT raises, re-raise for the caller to catch — F6/F11); idempotent (`ON CONFLICT DO NOTHING` + divergent-duplicate validation read → raise); `reconcile(conn, run_id) -> ReconcileResultV1`.
- [ ] **Step 5: gates + commit** (`feat(3b4): migration 0998 + contract/enum additions + shadow store + two-phase writer (task d1)`).

---

### Task D2: Telemetry flag + manifest + capture wiring (internal catch)

**Files:** Modify `planner/shadow.py`, `api/routes/contract.py`; Test `test_shadow_capture.py` + route test.

- [ ] Tests: telemetry off → zero rows, response byte-identical; **telemetry on + compile OFF + a run WITH path-resolved candidates → `compile_status='compile_disabled'`** (NOT `not_applicable` — F2), so Gate 1 can fail it; a run with NO path-resolved candidate → `not_applicable`; **pre-loop failure** (monkeypatch `resolve_catalog_scope` to raise) → manifest persists + `preloop_failure` rows, `run_shadow_planner` returns normally, route savepoint commits; **a `write_run_and_plans` re-raise is caught INTERNALLY** (monkeypatch the fallback to raise) → the manifest is NOT rolled back, reconciliation detects the missing result (F6); `template_not_found` → a row; reconcile: expected_count == rows.
- [ ] Implement: `run_shadow_planner(..., persist: bool = False)` — **`persist` DEFAULTS off (MEDIUM-2)** so the 4 existing callers (`test_shadow.py:26/53/76/97`) stay green and the telemetry-off path is byte-identical. When `persist`: write the dispatch manifest FIRST (no `catalog_scope_id`); nested savepoint around scope/context caught internally (`preloop_failure`, return normally); per recipe map the result → `RunResultRowV1` (+ observations); `compile_status='compile_disabled'` when `not compile_contracts and path_resolved_eligible > 0`; wrap `write_run_and_plans` in a try/except that emits a structured log + relies on manifest reconciliation (never re-propagates). `contract.py`: read `FEATUREGEN_INTENT_SHADOW_TELEMETRY`, pass `persist=`.
  - **Total `PlanResolutionStatus → PlannerOutcome` mapping (MEDIUM-1 — the source enum has 8 members, the CHECK admits 6; an unmapped status → CHECK violation → silent loss):** `resolved`/`resolved_with_ambiguity`/`partially_resolved`/`unresolved`/`bounded_out`/`safety_rejected` → `compiled` (they DID plan; `compile_status`/`bounding` carry the nuance); `not_applicable` → `no_authorized_catalog` (no plan) — but distinguish `no_physical_plan` (had a catalog, no path-resolved plan) from `no_authorized_catalog` (empty scope) at the mapper by inspecting the result/scope; `internal_error` → `internal_error`; the `template_not_found` skip and the caught pre-loop failure are mapped by `run_shadow_planner` directly (`template_not_found`/`preloop_failure`). The mapping is a TOTAL function over `PlanResolutionStatus` (a test asserts every member maps).
- [ ] gates + commit (`feat(3b4): telemetry flag + dispatch manifest + internal-catch capture wiring (task d2)`).

---

### Task D3: Discovery-from-ctx + dual inputs-only hashes + stamp enrichment (algorithmic core)

**Files:** Modify `planner/candidates.py`, `plan.py`, `scope.py`, `declarations.py`; Create `planner/fingerprint.py`; Test `test_fingerprint.py`.

**F5 fix:** thread `ctx.columns_by_catalog` into discovery so the hashed universe == the data discovery actually uses (or have discovery RETURN the loaded columns and hash those). Prefer: `discover_ingredient_candidates(conn, template, catalog_source, *, roles, columns=None)` — when `columns` (from ctx) is passed, use it instead of a fresh `_load_columns`; `plan_bindings` passes `ctx.columns_by_catalog[src]` when compiling. Then the hash and the verdict see the SAME snapshot.

- [ ] Tests: additivity/is_as_of/sensitivity change on a bound column → `contract_input_hash` changes; a new candidate column for a need → `planner_input_hash` changes, old selected plan's `contract_input_hash` unchanged (F5); an OUTPUT/declaration change under fixed inputs → input hashes unchanged, `declarations_output_hash` changes (F4); `agg_declarations` entry change → `contract_input_hash` changes (F13); determinism; real `ROLE_RESOLUTION_VERSION`; the discovery-uses-ctx-columns equivalence (same snapshot).
- [ ] Implement: `planner_input_hash(ctx, template, scope)` over the full `_Col` universe + realizations + scope-filtered bridges + roles + versions; `contract_input_hash(ctx, plan, template)` over the selected plan's read-set `_Col`s + used realizations/bridges + **the canonical physical path/segments + recipe content + representative params + the relevant `agg_declarations` entries + target_entity + rule/config versions** (F13); `declarations_output_hash(plan)` (outputs, separate). Enrich the stamp at BOTH `scope.py:42` and `declarations.py:786` + `audit_envelope` (F14); persist `planner_input_hash` on `run_result` (F4). Real `ROLE_RESOLUTION_VERSION` + producer-commit/config hash.
- [ ] gates + commit (`feat(3b4): discovery-from-ctx + dual inputs-only hashes + stamp enrichment (task d3)`).

---

### Task D4: `ReplayFreshness` (pure comparator + impure adapter)

**Files:** Create `planner/replay.py`; Test `test_replay.py`. (`ReplayFreshness.incompatible` was added in D1.)

**Produces:** `read_current_evidence(conn, stored_stamp, role_claims, used_realizations, used_bridges, versions) -> CurrentEvidenceV1` (IMPURE, snapshot-consistent, receives the STORED plan refs/roles so it can recompute the same-scoped fingerprints — F15); `compare(stored, current) -> ReplayFreshness` (PURE); `replay_freshness(conn, stored)`.
- [ ] Tests: match → `current`; additivity change (compiler_input_fingerprint) → `drifted`; unrelated checkpoint advancement → NOT drifted (lag invariant `>= head_seq`); out-of-scope bridge change → NOT drifted (scope-filtered); version mismatch → `incompatible` (short-circuits before drift); missing stamp / `checkpoint < head_seq` → `unverifiable`; `unverifiable`/`incompatible` never `current`.
- [ ] gates + commit (`feat(3b4): ReplayFreshness — pure comparator + impure snapshot adapter (task d4)`).

---

### Task D5: Two-layer cause taxonomy + Gate-2b review-artifact contract

**Files:** Create `planner/cause.py`, `planner/shadow_review.py`; Test `test_cause.py`.

- [ ] Tests: **Layer-A map exhaustive over the WHOLE `ReasonCode` registry** (static test iterates every member incl. the D1-added `aggregation_ordering_column_missing`; unmapped → `operationally_unmeasured` and the test FAILS); `safety_rejected`/topology → their categories; `operationally_unmeasured` ≠ `unknown`; the **Gate-2b review artifact** (`shadow_review.py`): a schema for the deduplicated `(reason, evidence_shape)` rows + expert label + reviewer identity + version + a detached signature (reuse D8's signer) — with a content hash so D8 can verify "every distinct observed shape is labelled."
- [ ] Implement the versioned static `RESOLUTION_CATEGORY_MAP` + `assert_map_exhaustive()` + the Layer-B contextual classifier keyed by `(reason, evidence_shape)`, and the `ReviewArtifactV1` contract (dedup key, labels, reviewer, version, signature slot).
- [ ] gates + commit (`feat(3b4): two-layer cause taxonomy + gate-2b review-artifact contract (task d5)`).

---

### Task D6: Defect folds — monotonic budget + compile_status + take_latest

**Files:** Modify `planner/plan.py`, `shadow.py`, `declarations.py`, `shadow_store.py` (the store mapper consumes the new compile fields — F16); Modify `cause.py` is NOT needed (the reason code was added in D1). Test `test_declarations.py`/`test_plan.py`/`test_shadow_capture.py`.

- [ ] Tests: **real elapsed-time timeout** via an injectable monotonic clock (distinct from the deterministic `now`) → `compile_status='incomplete'`/`incomplete_reason='budget_time'`; budget-incomplete runs EXCLUDED from deterministic-verdict comparisons; `compile_status='complete'` iff every PATH-RESOLVED candidate compiled (eligibility = `source_to_target_resolved`); the store mapper records the eligible/compiled/skipped counts; **`take_latest` with an ordering column NOT available at the aggregation hop OR aggregated away by a prior hop → `aggregation_ordering_column_missing`** (F14 — `anchor_binding is not None` alone is insufficient); available+surviving → sound.
- [ ] Implement: inject `monotonic()` into `CompileBudget` (deadline uses it; NEVER enters a hash/verdict — but note it DOES change the observed planning result, so it's operational, and incomplete executions are excluded from identity comparisons — F17); `plan_bindings` records `compile_status`/`incomplete_reason`/counts; the `_validate_stage` take_latest guard uses connectivity `placement` to require the ordering column at/before the stage hop + survival through prior grouping + adds it to the stage read-set/safety.
- [ ] gates + commit (`feat(3b4): defect folds — monotonic budget + compile_status counts + take_latest stage-local (task d6)`).

---

### Task D7: Curated gold set + `contract_eval` + fixed strata

**Files:** Create `planner/contract_gold.py`, `planner/contract_eval.py`, `planner/strata.py`; Test `test_contract_eval.py`.

- [ ] Tests: exact-match verdict+cause per case; invalid-but-`resolved` → FAILURE; the **fixed stratum registry** (`strata.py`: a versioned, deterministic, non-overlapping `stratum_of(observation) -> StratumId` over (tier × family × primary-dimension) — F18); the sampler dedups repeated `contract_input_hash` (clustered traffic); a rare stratum (< per_stratum distinct shapes) is flagged; **double-compile from a frozen fixture → identical**; empty comparison → the stability check FAILS.
- [ ] Implement the versioned/hashed gold corpus (seeded adversarial fixtures) + `evaluate` + the deterministic seeded stratified sampler over the fixed strata + the double-compile procedure. `GOLD_SET_HASH` module constant.
- [ ] gates + commit (`feat(3b4): gold set + contract_eval + fixed stratum registry (task d7)`).

---

### Task D8: Population report + conjunctive gate + detached-signed artifact

**Files:** Create `planner/shadow_report.py`, `planner/signing.py`; Test `test_shadow_report.py` + a PG e2e test.

**F10 signing (external trust root, ASYMMETRIC — MEDIUM-4):** genuine "the evaluator cannot sign its own output" (spec §10.7) requires **asymmetric** signing — a symmetric HMAC (the `security/audit.py` model) lets any key-holder, including the evaluator, forge a signature, so detachment would be merely procedural. Use **ed25519** (via the available `cryptography` dep): the PRIVATE key is held by a separate signing authority (out of the evaluator's process); only the PUBLIC key/keyring is a config input (`get_settings()`), NEVER embedded in the artifact. `sign_report(digest, private_key)` runs in the signer; `verify_report(path, trusted_public_key)` (the evaluator + CI) recomputes the canonical digest + verifies against the config-supplied trusted public key. Specify: algorithm (ed25519), canonical report bytes, key id, signer policy, a detached signature SIDECAR file, nonzero exit on any verify failure. Follow `security/audit.py` only for the config-key-resolution pattern (fail-closed if unset), not the symmetric primitive.

- [ ] Tests: the report's exact numerator/denominator (§9: selected + `compile_status='complete'` + `source_to_target_resolved`; one obs per (run,recipe)); multi-reason counting (headline by-primary, breakdown by-each); **conjunctive gate — any sub-gate fails → GateResult fails** (no averaging); **Gate 1 fails on ANY of: incomplete/`compile_disabled` eligible recipe, `persistence_partial`, child-count inconsistency, `internal_error`, `preloop_failure`, or planner truncation/bounding (F8) — NO signed-exclusion escape in v1 (F19)**; zero `operationally_unmeasured`(2a machine) + a signed Gate-2b review artifact with zero `classifier_defect`/`unknown` over every distinct observed shape (F9); the **statistical bound** — future-traffic binomial Clopper-Pearson (NO finite-population correction), zero-failure over ~300 distinct shapes → ≤1% one-sided 95%, per fixed stratum, rare stratum FAILS; Gate 5 uses D7's double-compile (empty → fail); **a human cannot override a FAILED machine gate**; **signed-report/sidecar tampering OR a wrong trust key → `verify_report` False + nonzero exit** (F10); the artifact records commit/gold-hash/policy-hash/versions/window/sample-ids/signer/producer-cohort.
- [ ] **PG e2e (F20):** one PostgreSQL-backed test route→manifest→run→plan→report→gate (telemetry on) proving the full chain.
- [ ] **Behaviour-neutral + full verification:** `uv run pytest tests/featuregen/ tests/db/ tests/featuregen/db/ -q` green (incl. the migration suites); both flags off → no writes, byte-identical response.
- [ ] gates + commit (`feat(3b4): population report + conjunctive detached-signed enablement gate (task d8)`).

---

## Exit criteria mapping

| Finding(s) | Task |
|---|---|
| F1 migration 0998 (rebased) | D1 |
| F2 compile_disabled; F8 bounding+integrity gate; F11/F12 DDL CHECK/JSON; F3 nullable compile-only + is_compiled | D1, D2, D8 |
| F6 internal catch; F7 durable reconciliation loss signal | D2 |
| F4 persist planner_input_hash; F5 discovery-from-ctx; F13 full hash material; F14 stamp sites | D3 |
| F15 adapter refs + incompatible | D4 |
| F9 Gate-2b review artifact; F16 map exhaustiveness incl new codes | D5 |
| F16 store mapper + reason code ordering; F17 monotonic wording | D1, D6 |
| F18 fixed strata | D7 |
| F10 trust-rooted signature; F19 no signed-exclusion; F20 migration suites + PG e2e | D8 |

## Self-Review

**Spec coverage + review coverage:** all 20 findings mapped (table). Migration verified against `origin/main` (`0998`), not the stale branch. ✅
**Placeholder scan:** D1 carries full DDL + the two-phase writer; D2–D8 carry exact signatures + the mandatory tests. D3 (snapshot fix + dual hashes) and D8 (statistics + trust-rooted signing) are the deepest-review tasks — flagged. ✅
**Ordering (F16):** contracts + reason codes + store schema land in D1 so D5's exhaustive map and D6's compile fields have their definitions; D6 explicitly edits `shadow_store.py`'s mapper + capture tests. Each task commit is internally green. ✅
**Executor notes:** (1) rebase is done — the branch is on `2e1e5d8`; confirm `0998` at D1 Step 1 regardless. (2) The manifest write must survive scope/persistence failures (internal catch, return normally). (3) Discovery must hash the SAME columns it uses (D3 F5) — thread `ctx` columns. (4) The signature key is EXTERNAL (config/secrets), never embedded; the verifier takes a trusted key. (5) No signed-exclusion override in v1 — Gate 1 is hard-zero on operational incompleteness.
