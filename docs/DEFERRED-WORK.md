# Deferred Work Register

**Purpose.** One place for every consciously-postponed item, so deferral is a recorded decision rather than an oversight. Nothing here is a bug we forgot; everything here is a bug/gap we decided not to fix *yet*, with the reason and the trigger to revisit.

**Standing directive (2026-07-27).** Functional requirements take priority over non-functional ones. Optimization, security hardening, crash-safety, observability, and test hygiene are deferred to the end. **Exception:** governed authority is the *feature*, not an NFR — "output policy comes from C1 governed facts, never the LLM's guess", grounding, and human-confirmed authority are never deferred. Second exception: an NFR whose damage is **irreversible while deferred** (e.g. bad rows written to a physically immutable table) gets fixed immediately.

**How to use.** Each item has a **Trigger** — the event that should make us pick it up. When starting a spec, grep this file for that subsystem and pull anything whose trigger has fired.

Legend: 🔴 functionally significant · 🟡 correctness/robustness · ⚪ hygiene/cosmetic

---

## A. Program-level NFRs — the feature-materialization program

Deferred wholesale from the parent architecture (`docs/superpowers/specs/2026-07-22-feature-materialization-pipeline-design.md`) so that Kedro/PySpark code generation — the core feature — ships first.

| Item | Origin | Why deferred | Trigger to revisit |
|---|---|---|---|
| 🟡 Iceberg atomic revisions, commit/merge/time-travel, restatement (append a revision, never overwrite) | Child-5 §7 | Storage durability. A generated pipeline can land Parquet/Hive without revision atomicity for internal testing. | First time two writers can commit to one feature table, or the first restatement request. |
| 🟡 Run state machine `REQUESTED→ACCEPTED→RUNNING→COMMITTED/FAILED/CANCELLED/STALE_INPUT` | Child-5 | Operational lifecycle, not feature behaviour. | Scheduled/unattended runs, or any run we cannot just re-launch by hand. |
| 🟡 Multi-write atomicity across data commit + run manifest + active-revision pointer + stats + callback | Child-5 | Iceberg isolation is table-local; needs real design. | Same as above. **Note:** this is the same transaction-semantics shape that made Child-1 T11 the most defect-dense task (4 review iterations) — budget for mutation testing + live probes, not a prose review. |
| 🟡 Outbox / reconciliation when either side is down | Child-2/§8 | Distributed-failure handling. | First external (non-in-process) executor. |
| 🟡 External attestation round-trip: request/result schemas, idempotency-key derivation, callback auth, duplicate/out-of-order/cancellation races, result manifest + attestation verification, frozen-input-snapshot binding | Child-6 | Whole child deferred; no external platform yet. | An executor outside this process. Also gates `DATA-CHECKED` promotion. |
| ⚪ Execution-signature batching optimization (`physical_plan_shape + landing_grain + PIT_basis + compatible_source_snapshot + cadence`) | Child-7 §9 | Pure optimization. Multi-feature co-generation (the functional ask) is NOT this — it ships in the code-gen spec. | Measured cost problem from running many features. |
| ⚪ Quarantine by bounded bisection (never auto-blame a formula); technical_failure ≠ semantic rejection | Child-5 | Failure-attribution refinement. | Batch runs where one bad formula fails a group. |
| 🟡 Profiling privacy/read-scope hardening; restricted min/max/histograms never leaving the platform; allow-listed summaries only | Child-7 §11 | Security hardening. **EDA on final feature outputs is functional and ships with code-gen** — this is the privacy gate around it. | Before any profiling output leaves the platform or reaches a shared catalog. |
| 🟡 Full Child-2 lifecycle: atomic artifact→feature-version→binding sequence, `mint_feature_version` wired to `contract_id`, status axes → `materialization_eligibility`, template-reuse policy, semantic/operational/run parameter split | Child-2 | Code-gen needs only *enough identity to attribute generated code to its formula*. | Production activation, or more than one consumer of a frozen artifact. |
| 🟡 Full `TemporalPolicyV1`: SCD effective/system time, reversal policy, late-arrival horizon, restatement policy, availability-basis external requirement | Child-4 | **Window/cutoff PIT correctness is CORE and ships with code-gen** (a look-ahead feature is *wrong*). The SCD/reversal/late-arrival refinements are not. | Bitemporal sources, or a late-arrival/reversal correctness question on real data. |
| 🟡 Delivery I — external-validation protocol (Ed25519 + RFC 8785) | pre-existing | Partner-gated fast-follow, not first release. Produce-side already staged. | A partner willing to validate. |

### A.1 Newly identified while designing Spec A (2026-07-27)

| Item | Why deferred | Trigger to revisit |
|---|---|---|
| 🔴 **Governed source-delivery SLA** — a fact stating when a source's business data is ready (T+1 / T+3), distinct from catalog-scan freshness | The prerequisite for **deriving** `availability_class` instead of declaring it. Verified absent: `AVAILABILITY_TIME` (`overlay/facts.py`) is `{column, basis, lag_hours?}` — it names which column carries knowledge time, not when a source arrives; `drift_freshness_sla` (`overlay/config.py`) measures catalog-scan freshness. Deriving availability from either would be dishonest. Deferring does **not** break correctness: look-ahead is prevented by the availability-column comparison, so a late row is simply absent from an earlier run. | Before promising consumers an availability SLA, or before enabling the `dependencies_ready` trigger. **Recoverability is built in:** every contract persists its full physical read set, so derivation can retroactively validate each declared `availability_class` and flag the ones that lied. |
| 🟡 `dependencies_ready` cadence trigger | Needs the delivery SLA above. Spec A supports `scheduled` and `manual` only. | Same as above. |
| 🟡 **Authenticated** submission into a real bank environment | Spec A ships the full submit → validate → classify → regenerate **loop** with a thin `LocalClusterSubmitter` (`spark-submit`/Livy on localhost), behind a `PipelineSubmitter` seam. What's deferred is the cross-organisation transport: request-endpoint auth, idempotency-key derivation, run acceptance/status/cancellation protocol, result attestation, reconciliation after partial failure (Child-6). Only the transport changes — the loop's value doesn't depend on it. | A cluster outside the local dev environment, or unattended/scheduled runs. |
| 🟡 Content-addressed input snapshots | Spec A's `input_snapshot_ids` = the `(schema, table, partition)` list read + the IR's `catalog_state_stamp`. That identifies *which* inputs and *which governed facts*, but is **not** a content snapshot — two runs over the same partitions after an in-place source rewrite share the same execution identity. True content versioning needs the deferred Iceberg layer. | Reproducibility/replay guarantees, or any restatement work. Also one of the reasons Spec A publishes to sandbox. |
| 🟡 Multi-partition and backfill runs | Spec A does one `business_dt` per run. | First backfill request. |
| 🟡 Multi-environment promotion | One Hadoop/Hive environment in Spec A. | A second environment. |
| 🟡 **`CurrentSnapshot` vintage mismatch has no failure code** | Spec §4.2 says a `CurrentSnapshot` whose observed vintage does not match a run's `business_dt` "refuses rather than pretending", but no member of the four closed §14 enums expresses it. Task 4 deliberately did **not** invent one. The condition arises at **run preparation** (business_dt is a run parameter), which is neither a compilation nor a publication decision — so the enum it belongs to is itself the open question. | Task 15, where run preparation is built and the condition first becomes reachable. |
| ⚪ **Spec B** — statistical profiling/EDA + UI | Split out deliberately so Spec A ships runnable feature tables first. Spec A reports via the run manifest only (row counts, validation results), not statistics. | After Spec A publishes on the cluster. Profile history is append-only from the start so drift detection can be added later without backfill. |
| ⚪ **Spec C** — `model_input` assembly | Needs published groups to assemble. | When a model needs features from more than one group. Carries the daily/monthly **as-of** alignment rule (a daily row takes the latest month-end value not later than its `business_dt` — a naive equi-join would leak future month-end data into early-month rows). |

**Not deferred, despite resembling NFRs** — recorded here so nobody "optimizes" them away: PIT correctness (a look-ahead feature is *wrong*, not unhardened) · atomic group publication (a partially-published table is a correctness failure) · the blocking validation gates (duplicate keys, missing/extra/mistyped columns, incomplete computation, project-integrity) · derived sensitivity/access/retention classification (governance is the product).

---

## B. Child-1 (TypedFormula authoring) — open findings

All 12 tasks shipped with per-task reviews; **0 Critical, 0 unresolved Important** except B-0 below. The whole-branch final review was **deliberately skipped** under the functional-first directive (each task already had a dedicated adversarial review; the marginal value was cross-task polish).

### B-0 🔴 The one open Important — §C unit-cancellation is INERT for FTR
`src/featuregen/formula/output_authority.py` (`_resolve_ratio` / `_resolve_difference`)

The generic technical CSV path does carry `unit`/`currency` through `_headers.py` →
`CanonicalRow` → `graph_node`. The FTR adapter and the real FTR file do not provide either field,
however, so FTR operands read `unit=None` and `currency=None`. `_same_dimension(None, None)` treats
that as compatible, and so:
- `DIFFERENCE` with incompatible units **never** emits `InvalidOutput`
- `RATIO` with non-cancelling units **never** emits `ExternalRequirement("UNIT_PROVISIONING_REQUIRED")`

The resolver logic is exercised when a source actually supplies dimensions, but the FTR cohort
cannot trigger it. **Two brief-mandated authority behaviours silently never fire for FTR.**
**Trigger:** before any real FTR ratio or difference where incompatible dimensions matter.
**Fix shape:** provision reviewed FTR unit/currency metadata, or version the formula policy so an
absent dimension produces an external requirement rather than changing existing formula identities
in place.

### B-1 Authority / correctness (🟡)
| # | Item | Location |
|---|---|---|
| 1 | `COUNT_*` resolver branches — incl. the `COUNT_ROWS` grain-fail-closed path — never driven through `resolve_formula_output_policy` (only `formula_additivity` covers counts) | `output_authority.py` |
| 2 | RATIO operand-numericness (§C "numeric both") not enforced; DIFFERENCE over-requires additivity (beyond the §C row — conservative, harmless) | `output_authority.py` `_resolve_ratio` / `_resolve_difference` |
| 3 | The fully-governed type path (`external_type_required=False`) is never asserted by any test — only the degrade path is exercised | `output_authority.py` |
| 4 | `AuthoringResult` has no `__post_init__` coherence guard: direct construction (bypassing `derive_disposition`) can build an incoherent result or a **lying** `candidate_formula_hash`. Today `derive_disposition` is the sole builder (repo-wide grep confirms one construction site) so it is unreachable — but it is the last residual surface of the honesty invariant. | `result.py:116` |
| 5 | `candidate_proposal` is permitted on terminal `UNSUPPORTED`/`REJECTED`/`TECHNICAL_FAILURE` (only `candidate_formula` is forbidden). Carries no authority, so it cannot launder anything — but a consumer could misread it as "reviewable". | `result.py:199-204` |
| 6 | **Accepted design property, not a defect:** a *well-formed* critic emitting only out-of-set finding codes reads as clean and can auto-RESOLVE. This is §G's mandated closed-vocabulary trade-off (unknown codes dropped, never block or clear). Revisit only if real critic runs show meaningful out-of-vocabulary findings. | `critic.py` |
| 7 | `critic_status="clean"` accompanies a technical critic, and a critic call is spent even on out-of-capability proposals. Both need a §F `not_run` axis value to fix properly — do it once, for both. | `result.py` / `authoring.py` |
| 8 | The real **C1 roles seam**: `read_operational_value` and `read_column_facts` take no `roles` parameter, so C1 authority reads are un-scoped (the author's tools and the critic's re-fetch *are* scoped). Narrow exposure — the model must name a ref its own read-scoped tools never showed it — but governed facts of a sensitivity-hidden column would then surface in `TypedFormulaV1.output`. Cross-task (T5/T6). | `operational_facts.py:317`, `column_authority.py:99` |

### B-2 Egress / security hardening (🟡 — deferred per directive)
| # | Item | Location |
|---|---|---|
| 1 | `assert_llm_safe`'s data-value-key backstop checks only **top-level** `inputs` keys, never inside `catalog_metadata.tool_trail`. `_first_pii` does recurse, so PII is caught — but an arbitrary non-PII data value nested in a tool result would not be. **The 7 tools' own metadata-only discipline is the PRIMARY control**, not a backstop; a future tool regression emitting a `rows`/`samples` key would not be caught downstream. Pre-existing SP-2 guard. | `redaction.py:220` |
| 2 | `authoring_intent.hypothesis` free text is not field-aware redacted. The `_first_pii` backstop still fails the whole call closed (→ technical), so no leak — but a PII-laden hypothesis fails the run instead of being scrubbed. | `authoring.py` / `redaction.py` |
| 3 | Trace `payload` is metadata-only by **convention**: the only DB guard is `CHECK (jsonb_typeof = 'object')` — no size bound, no key allowlist. `_jcs` rejects ints outside ±2^53. | migration 1020, `trace.py` |
| 4 | `validate_draft_formula`'s `detail`/`error` text is now omitted from the **trace** (2026-07-27 fix — the trace row is immutable, so that leak was irreversible), but it still **egresses to the provider** on the next turn's tool trail. Deferred deliberately: it is the model's own text going back to the model, it still passes the egress guard's PII backstop, and unlike a written trace row it is reversible. jsonschema messages quote the offending instance value, and `parse_ref` echoes the model's argument verbatim (`tools.py:129`). | `tools.py:129,239`, `authoring.py` |

### B-3 Provider-wire risk (🔴 watch on first live run)
`proposal_v1.schema.json`'s self-recursive `filterNode` and its many `const` keywords are hoisted into the author's turn schema. `schema_projection.provider_incompatibilities` treats `$ref`/`const` as valid shape keys and never checks ref-resolution, recursion depth, or `const` support — **so a shape can pass the static guard and still 400 on a real Anthropic call.** Fails closed (a 400 → provider failure → `output=None` → technical), so there is no leak — but it is a functional risk nobody has exercised against the wire. Fallback if it bites: `const` → single-value `enum`, and inline/bound the recursion.
**Trigger:** the first real-provider run (gate (c), below). Also re-check after the wire `aggregation` enum was relaxed for the `avg` fix.

### B-4 Trace durability residuals (⚪/🟡)
| # | Item | Location |
|---|---|---|
| 1 | A lost-ack replay of a **terminal** event can still raise from migration 1020's terminal-guard trigger, which fires before any `ON CONFLICT` arbiter check. Deliberate deferral (the outcome is honest — a run the caller sees as closed reads as closed). In-module fix: catch `RaiseException` on a terminal replay and confirm convergence by re-reading the `idempotency_key`. | `trace.py:123-125` |
| 2 | The DB enforces "nothing after a terminal" but **not** "STARTED first" nor `seq` monotonicity — both are orchestrator convention (T12 upholds them), not physics. Unlike terminal-uniqueness there is no index behind the post-terminal trigger's `EXISTS` probe, so the guard is visibility-bound. | migration 1020 |
| 3 | One fresh connection **per event** — an N-turn ReAct run costs N+1 connects (bounded by the §I iteration cap; consistent with the `_record_llm_call_durable` precedent, but materially higher volume). | `trace.py` |
| 4 | 3s `lock_timeout` is a hardcoded constant, not configurable. Reviewer judged acceptable (`SET LOCAL`, effectively one-shot per run, expiry routes into a tested path) — but it is user-visible request latency. | `trace.py:137` |
| 5 | The replay path's `ON CONFLICT DO NOTHING` treats a replay-only duplicate as convergence rather than a breach (normal path unchanged and still propagates). | `trace.py` |
| 6 | `_capability_reason` is a **third** mirror of Task 7's single-source rule with no drift-pinning test, and it disagrees with `capability.py` on a widened body union: `capability.py:55` returns `"unsupported_capability"` while `_capability_reason` → `_expressions` **raises `SchemaError`**, which would propagate out of `run_authoring` and leave the run incomplete instead of returning UNSUPPORTED. Dead today (both `pragma: no cover`). | `authoring.py:458,515-525` |
| 7 | `_C1_HARD_FAIL_STATUSES` mirrors Task 6's private `_HARD_FAIL_STATUSES`. Judged **acceptable** (importing a private name into `src` is worse; drift is impossible — a test pins it). Clean end-state: T6 publishes the set. | `authoring.py` |
| 8 | The `REVOKE UPDATE, DELETE, TRUNCATE FROM featuregen_app` block is never exercised (the role does not exist in the test cluster) — same as the 1002/1018/0910 precedent. | migration 1020 |
| 9 | The new FK makes `TRUNCATE llm_call CASCADE` impossible (it trips the event trigger). No caller does this today. | migration 1020 |

### B-5 Gold-gate power (⚪)
| # | Item |
|---|---|
| 1 | **Gate (c) has never been run** — no `ANTHROPIC_API_KEY` in this environment. Its content, `skipif` locks and pinned thresholds are verified; whether a live model clears them is unknown. **This is the only test that measures whether the LLM authors good formulas.** |
| 2 | `PROVIDER_GATE`'s `min_disposition_accuracy_rate=1.0` includes `provider_trap_absent_column`, which demands NEEDS_REVIEW. If the model declines to author over a nonexistent column it will max out turns → TECHNICAL_FAILURE → that bar fails. **Expect gate (c) red for reasons unrelated to what §J actually pins**; tune when we first run it. |
| 3 | Gate (b)'s `blocking_critic_recall == 1.0` is satisfied by a FakeLLM replaying fixture 10's hardcoded findings — a plumbing assertion wearing a quality metric's name. The gate does not collapse (gate (c) drives the real LLM-2 against the same fixture) and the docstring is honest about it, but green there proves mapping, not recall. |
| 4 | `_finish` passes `candidate_proposal=None` on UNSUPPORTED/REJECTED, so no artifact survives and fixture 07 must declare `expected_operands: []` — **operand preservation is unmeasurable across the whole UNSUPPORTED population.** |

### B-6 Test-coverage gaps (⚪)
`MAX_PREDICATES` enforced as a post-hoc sum · inconsistent enum-instance guarding in `_check_window`/`_check_expression` · boundary positives at exactly 16 predicates / 64 in-list untested · `grain.keys minItems:1` stricter in JSON schema than in `validate_semantics` · `test_wrong_schema_version`/`test_empty_logical_ref` not schema-discriminating (the semantic layer also rejects) · no `filter.kind`-discriminator negative test · `DiffBody`/`UnaryBody` canonicalization branches untested (factories build Ratio only) and no minuend/subtrahend swap test · `NOT_IN`/`OR`-flatten shared paths not independently asserted · enum serialization inconsistent (`_enum_value` vs bare `.value`) · mild duplication across `seed_resolved`/`no_value`/`conflict`/`not_operational` fixtures · `_body_expressions` in `capability.py` shadows `schema.py`'s same-named helper with a different return shape · three single-source "ok" capability tests don't call `validate_semantics` first · `"egress_blocked"` sentinel at `enrich_llm.py:690` should be a named constant beside the `STATUS_*` set · duplicate `authoring_intent_hash` call.

**Producer contract worth remembering:** literal value strings are **not** re-canonicalized by `formula_content_hash` — a non-canonical decimal/int/date string hashes differently from its canonical form.

---

### A.2 Latent fail-open found while building Spec A (2026-07-27)

| Item | Detail | Trigger |
|---|---|---|
| 🔴 `cardinality_from_token(None) → MANY_TO_ONE` | `overlay/upload/catalog_realizations.py:37-39` silently treats an absent/empty cardinality token as `N:1`. This is precisely the fail-open Spec A's join adapter **refuses** (`JOIN_CARDINALITY_UNKNOWN`), because an unknown edge may actually be `1:N` — which multiplies rows and inflates a SUM. Untouched by Spec A and harmless where it is used today. | **Before any materialization path can reach it.** If a future task routes join cardinality through this helper, the T3 refusal is bypassed and wrong numbers become possible again. Found by Task 3's implementer. |

### A.3 Shipped-code defects found while building Spec A Task 4 (2026-07-27)

| Item | Detail | Trigger |
|---|---|---|
| 🔴 **Ref case-handling is inconsistent across readers** | `entity.effective_entity` queries `WHERE object_ref = %s` (exact) while `column_authority._scalar` queries `WHERE lower(object_ref) = %s` with a lowered parameter. `build_graph` stores refs in the upload's own casing, so a **mixed-case catalog reads as "no governed entity"** through the first path while the second resolves fine. This is a **class** of bug: any caller that rebuilds a ref rather than threading the stored one is exposed. `materialize/spine.py` threads the stored ref and is safe. | Before any mixed-case catalog is ingested, or when a governed entity inexplicably reads as absent. Fix = one case convention across every `graph_node` reader. |
| 🟡 **`GRAIN.is_unique` is written but never read** | Every writer hardcodes `is_unique: True` (`ingest.py:287,2792`, `table_synth.py:150`); no projection path consumes it. An `is_unique=false` fact would be accepted, stored, and silently ignored — a latent fail-open. Spec A therefore derives uniqueness from the governed grain **set** rather than trusting the flag. | Before anything relies on `is_unique` as an authority, or when a non-unique grain must be expressible. |

### A.4 Identity properties accepted while building Task 5 (2026-07-27)

| Item | Detail | Trigger |
|---|---|---|
| 🟡 `catalog_state_stamp` uses a **catalog-wide** `drift_head_seq` | An unrelated upload to the same catalog source moves **every** requirement's identity, so projects regenerate when nothing they read has changed. Conservative rather than wrong — it never claims staleness it doesn't have — but it will cause avoidable churn. Wall-clock (`last_completed_at`) is deliberately excluded so a re-projection of an unchanged catalog does not move identity. | When regeneration churn becomes a cost, or when a per-object stamp exists. |
| 🟡 A catalog with **no watermark is recorded honestly, not refused** | There is no §14 member for "catalog state unknown", so Task 5 records the absence rather than inventing a code. Consequence: identity does not pin catalog state in that case. | If catalog-state pinning must be guaranteed, T1 needs a new code and this becomes a refusal. |

### A.5 Availability + grain-path limits found in Task 6 (2026-07-27)

| Item | Detail | Trigger |
|---|---|---|
| 🔴 **`AVAILABILITY_TIME.basis` and `lag_hours` are projected NOWHERE** | `table_fact_projection` flattens the fact to a boolean `is_as_of` flag plus a fact-event link; `graph_node` has no `basis` or `lag_hours` column. Spec §8 rule 1 (the availability gate, incl. `event_time_plus_lag`) therefore **cannot be rendered from `graph_node` at all**. Task 6 dereferences the fact payload *through the link the projection wrote* (VERIFIED status + matching `confirmed_event_id` + matching column + `validate_fact_value`), keeping authority with the shipped reader rather than re-deciding it. Two accepted consequences: a **catalog-authoritative** availability fact is unusable today (fail-closed), and expiry/drift are exactly as strong as the shipped projection — no stronger. This is the only place `materialize/` reads a table the rest of it does not. | Projecting `basis`/`lag_hours` (or an equivalent) would let the gate read from `graph_node` like everything else, and would make catalog-authoritative facts usable. Same "mechanism present but not fully wired" class as B-0. |
| 🟡 **A grain spanning two off-source tables is refused** | Spec §3's IR carries one `join_plan`, so such a grain would need two traversals with two independent fan-out verdicts. Refused as `GRAIN_PATH_NOT_GOVERNED` because the closed §14 vocabulary has no member naming the real condition. Left open rather than designed around. | A formula whose grain keys genuinely live on two different off-source tables. |
| 🟡 **A joined dimension's own availability is ungated** | §8 rule 1 gates one availability column per expression, so a dimension reached by a join contributes no availability constraint of its own in this slice. | Bitemporal or late-arriving dimension data, where the dimension's own knowledge time matters. |

### A.6 Compile-side gaps found in Task 7 (2026-07-27)

| Item | Detail | Trigger |
|---|---|---|
| 🔴 **An absent `graph_node` row authorizes, and that compounds the case bug** | `ir.py:379`: "a ref with no `graph_node` row at all is treated as untagged, i.e. authorized". Compilation resolves the *table* but never verifies a **column** ref names a real catalog node, so `…transactions.no_such_column` compiles, enters the read set and passes Gate 2; L1's `COLUMN_ABSENT` is the first check that catches it. Alone this is only late detection. **Combined with A.3's case-handling inconsistency it is worse:** a ref that fails to match its node *because of casing* also reads as untagged and authorizes — bypassing the restriction on the real column. Untagged-means-visible is correct for a node that exists; it is not correct for one that could not be found. | Fix alongside A.3. Minimum: distinguish "node exists and is untagged" from "node not found", and refuse the second. |
| 🟡 **No code for "formula grain entity ≠ population entity"** | Task 7 used `GRAIN_PATH_NOT_GOVERNED`, which does not name the real condition. §14 has no member for it. | When the message matters to a requester, or when the two conditions need different fixes. |
| 🟡 **§1.3 and §2 disagree on where the spine lives** | §1.3 passes `irs` and `spine` separately; §2 puts the spine inside the IR. Disagreement between them is unspecified, so Task 7 raises `ValueError` — which is also the only place §4's "declared once per contract" is actually enforceable, since `compile_ir` validates per feature. | Resolve when the orchestrator (T15/T17) wires the real call. |

## C. Repo / infra health

| Item | Detail | Trigger |
|---|---|---|
| 🔴 **No trustworthy full-suite CI signal** | ~82 whole-repo test failures (count is environment/ordering dependent; measured 82 at both `9aee241f` and Child-1 HEAD). Cause: cross-test DB contamination — **all pass in isolation**. Child-1 introduced **zero** (verified by `comm` on sorted FAILED lists at base vs head, empty in both directions). Nobody owns this. | Before relying on CI to gate a merge, or before the next program adds more surface. |
| ⚪ Frontend `vitest` hangs on worker-start in this environment | Changed files pass individually; CI must run the full frontend suite. | Frontend work. |

---

## D. Pre-Child-1 deferrals (earlier programs)

| Item | Status |
|---|---|
| 🔴 **LLM feature-ranking** (hybrid deterministic backbone + LLM advisory re-rank with per-feature rationale) | Requested and brainstormed, **never built**. Purely functional — reconsider early under the functional-first directive. |
| B-T12 real-two-source acceptance test | Gated on a real customer file sharing `CIF_ID`. |
| FTR glossary adapter A2 — provenance/storage | Separate spec, not written. |
| P2b — real authority change (influence promotion + decision projection + blast radius) | Gated on human gold labels / benchmark. P2a (cheap provenance + gate) was the split-out safe half. |
| Half B / `USEFULNESS-CHECKED` verification stamp | Deferred from Slice 3. |
| Key-gated enrichment eval + LLM key rotation | Open ops items from Slice 3. |
| Ingestion-review accepted minors | #21 single-fallback undercount · #12 sibling guard for `route_strategies`/`find_cross_catalog_path` · pre-existing UPPERCASE catalogs re-key once on next upload. |
| 3C.2b remainder | Remove `find_cross_catalog_path`; Phase 3D; Governed Feature Policy. |

---

## E. Open design decisions needing a human call

| # | Decision | Current behaviour |
|---|---|---|
| 1 | **Governed-join drift policy** (ingestion review #6) | Authority persists across drift today. Alternative: invalidate on drift. |
| 2 | **Schema-collision handling** (#9) | Chose fail-closed quarantine over multi-schema support. |
| 3 | **`not_run` axis value for §F** | Would fix both `critic_status="clean"`-with-technical-critic and the wasted critic call on out-of-capability proposals. Needs a §F vocabulary change. |
| 4 | **Whether C1 gets a `roles` seam** | Today C1 authority reads are un-scoped (B-1 #8). Cross-task change to T5/T6. |
