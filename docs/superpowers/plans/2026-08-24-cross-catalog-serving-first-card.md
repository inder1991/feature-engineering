# Cross-Catalog Serving — "First Served Card" Implementation Plan (REV 12, executable)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Rev 12 is SELF-CONTAINED — it replaces Revs 1-11. TWELVE review rounds (2026-08-24) absorbed; every falsifiable finding verified at origin/main `463498ed` before adoption. **Gate: T0 and P1 are dispatchable; Phase implementation follows the round-12 execution order below.** Parent spec: `2026-08-23-cross-catalog-program-rev5.md` §Stage-2 amended by P1. Stage-1 rigor; NO merge/push/deploy/flag-flip without explicit user go. Migration numbers appear in NO task until T0 assigns the single authoritative 1130-1139 mapping — then every task and the reservation table update in ONE commit.

**Goal:** From a submitted hypothesis to a sealed Kedro/PySpark preview: an AI-proposed customer↔transaction link becomes an exact, reproducible, guarded cross-catalog calculation — with every step carrying the identity the user confirmed and every action state the canonical service's verdict.

## Owner's serving policy + the capability matrix (binding)

| Condition | Formula | Preview |
|---|---|---|
| AI-proposed link, complete ordered mappings | Allow | Continue (per rows below) |
| Missing directional mapping | Allow | Block (`DIRECTIONAL_MAPPING_INCOMPLETE`) |
| Unknown cardinality + guard policy | Allow | Provisional (guards compiled in) |
| Known N:1 | Allow | Full |
| Known M:N, final grain | Allow (source-grain intermediate only) | Block (`ALLOCATION_POLICY_REQUIRED`) |
| Missing temporal policy | Allow | Block (`TEMPORAL_JOIN_POLICY_MISSING`) |
| Duplicate transaction identity | Allow | Render guard; fixture/run REFUSES (`TRANSACTION_IDENTITY_NOT_UNIQUE`) |

Six-action availability THIS scope: AUTHOR_FORMULA available · GENERATE_PREVIEW available when pins + renderer exist · EXECUTE_SANDBOX unavailable (`SANDBOX_EXECUTION_NOT_RELEASED`) · PUBLISH_SANDBOX unavailable (no verified sandbox output) · MATERIALIZE_PRODUCTION unavailable (`PRODUCTION_MATERIALIZATION_NOT_RELEASED`) · PUBLISH_PRODUCTION unavailable (`PRODUCTION_PUBLICATION_NOT_RELEASED`). Deployment capability is a SERVER-OWNED fact folded by the SAME six-action service; routes, dashboard rails, and buttons consume ONE verdict; a route never enqueues what the dashboard calls unavailable. Human review of a link changes display provenance only. The UI never asks a user to type join columns.

## Rulings (R1-R10 carried from rounds 1-11; R11-R14 bind round-12's product decisions)

- **R1 — one authority, six typed fact loaders** (facts only, never verdicts): AUTHOR_FORMULA → 1103 subject key · GENERATE_PREVIEW → build-set revision (content identity = ordered member bindings + generation configuration) · EXECUTE_SANDBOX → sealed artifact · PUBLISH_SANDBOX → `verified_output_revision_id` · MATERIALIZE_PRODUCTION → sealed artifact · PUBLISH_PRODUCTION → exact production output revision. Legacy ladder keeps save_idea+create_contract; other rungs adapter/retire (canonical wins; `RECIPE_REVIEW_NOT_CURRENT` proven row). All `ActionRequestV1` call sites migrate. Pre-resource cards: "potentially available after selection."
- **R2 — execution context never touches logical identity.**
- **R3 — physical adoption: append-only, user-confirmed, environment-scoped** — root + CAS head per `(selection_revision_id, execution_context_revision_id)`; POST names `physical_plan_revision_id`; partial-unique root; one successor per predecessor; semantic-only content_hash; build members' adoptions match the build environment.
- **R4 — no runtime observations without a runtime.** Preview emits guards; CI proves guards fire (artifact-bound fixture results ONLY — no queue/dashboard/terminalization theater); operational observations arrive with the future governed sandbox worker.
- **R5 — synchronous identity, asynchronous telemetry.** Serving transaction persists logical plan + binding + activation facts synchronously; telemetry rides the NEW result outbox (worker persists verbatim, never replans); 1121 keeps its legacy lane; shadow excluded from live denominators; demand deduped by `demand_identity_hash`.
- **R6 — reuse platform codes.** `DIRECTIONAL_CARDINALITY_UNPROVEN` (existing, W/W/W/W/B/B) is the cardinality reason; `CARDINALITY_EVIDENCE_REQUIRED` demand-category only; `REALIZATION_ATTACHMENT_DEFECT` ops alert.
- **R7 — one authoring subject, ever** (the 1103 identity; promotion evidence keyed by it + `formula_draft_plan_binding`; closed class "LLM deterministic intent awaiting formula"; conceptual patterns stay blocked).
- **R8 — narrowed supersession; B0a/B0b OWN the remediation's preserved work** (server-derived scope, target lineage, frozen method identity).
- **R9 — logical identity = feature meaning only** (canonical definition content hash/revision; operation; typed operand bindings + governed semantic revisions; ordered output grain; SELECTED parameter pairs; logical relationship path; formula policies; R14's logical temporal semantics). Hypothesis, planning-request hash, chooser revision, menus, display → provenance pins. ONE chooser owner.
- **R10 — temporal safety split by identity layer** (superseded in detail by R14 + the contracts below).
- **R11 — AI-proposed links create provisional realizations, server-side.** An available AI-proposed identifier link may be converted by the SERVER into a SANDBOX-scoped provisional directional realization containing: ordered endpoint column pairs; frozen source-binding revisions; environment; `purpose=feature_generation`; `execution_tier=SANDBOX`; `cardinality=UNKNOWN` unless proven; `safety_status=UNASSESSED`; the exact bridge dependency. No human confirmation (provenance only). Missing column mappings still block preview; unknown cardinality → provisional with guards.
- **R12 — joined customer attributes use an EXPLICIT joined-attribute predicate (Formula V4).** Supersedes round-11's sealed-plan-predicate ruling (recorded reversal): the two mechanisms have different semantics and the operator wins — never a pre-filtered spine substitute, never an in-task choice.
- **R13 — duplicate transactions REFUSE by default.** COUNT_ROWS requires governed transaction identity; violation → `TRANSACTION_IDENTITY_NOT_UNIQUE` → guard fails the fixture/run → NO feature values. Deduplication is outside this increment; COUNT_DISTINCT is never a substitute; no "counted once or refused" language anywhere.
- **R14 — temporal meaning is LOGICAL.** `as_of_event_time` vs `as_of_cutoff`, and `as_known_at_cutoff` vs `latest_available`, change feature meaning → logical identity. Column bindings → physical identity. **First-journey ruling: customer state EFFECTIVE AT CUTOFF; customer knowledge AS KNOWN AT CUTOFF; latest-correction FORBIDDEN** (`KnowledgeTimeBasisV2`'s own leakage doctrine).

## Temporal contracts (round-12: two types; B3c REUSES existing authorities — `DatasetTemporalPolicyRevisionV1`, `DatasetRowSelectionV1`, `KnowledgeTimeBasisV2`, `AsOfIntervalRequirementV1` — never a second temporal language)

```
LogicalTemporalJoinSemanticsV1 (IN logical_digest): effective_time_basis, knowledge_time_basis,
  driving_time_role, interval inclusivity, unmatched-row meaning, static-link meaning.
PhysicalTemporalJoinBindingV1 (IN physical_digest): dataset_temporal_policy_revision_id,
  effective_from/effective_to column refs, availability/knowledge-time column ref, cutoff
  parameter ref, source-binding revision, tie-break refs.
```

Mandatory tests: customer state changes after cutoff; backdated record inserted after cutoff; overlapping validity intervals; two records valid at one cutoff; current-only source used for historical generation (refuses); `latest_available` refused for training/backtesting.

## Identity chain (round-12: lifecycle-specific, extending CompilationIdentityV2 + generate_v2's rendered-file hashing)

```
card                logical_digest
formula binding     formula_binding_digest        = H(logical_digest | formula_content_hash | formula_method_identity)
physical adoption   member_execution_input_digest = H(formula_binding_digest | physical_digest | member_output_contract)
compilation         member_compile_digest         = H(member_execution_input_digest | ir_hash | policy_occurrence_bindings)
build               build_compilation_digest      = H(target_and_spine_revision | ORDERED member_compile_digests | generation_configuration_digest)
rendered artifact   project_digest                = H(actual rendered files)
sealed artifact     sealed_artifact_identity      = H(build_compilation_digest | render_profile_digest | project_digest)

MemberOutputContractV1 (single owner; these fields REMOVED from GenerationConfigurationV1):
  output feature name, output column name, empty-window value, not-applicable value,
  null-input behavior, physical type, decimal scale, rounding policy, overflow policy.
JoinValidationPolicyRevisionV1: closed enums as rounds 6-10; semantic-only content_hash;
  snapshot-SELECTION rule in policy, concrete snapshots in (future) observations; FAN-OUT LAW:
  final-grain aggregate + max_matches > 1 → ALLOCATION_POLICY_REQUIRED unless a typed operator
  proves pre-aggregation/dedup/allocation.
```

Staleness law: proposed→confirmed / strength / display text → no rekey. Withdrawn/revoked → refuse (matching plan variant becomes unavailable). Superseding realization → refuse old pin; adoption is the path. Join columns/predicates/cardinality/temporal SEMANTICS → new identity (logical for meaning, physical for realization). Environment → new physical revision. Same selected parameters via different hypotheses → same logical identity.

## §V verified facts (twelve rounds, cumulative; T0 re-verifies ALL)
V1 `active_bridges` proposed+confirmed; `ActiveBridgeV1` thin; `members[0]` collapse (`bridge_projection.py:42-56,141,165,168`). V2 G3-at-compile; lens consumes compiled results; G2 masking + 82-operand worklist (`governed_lens.py:36-67,580,648`; `need_metadata.py:84,108`; `declarations.py:716`). V3 realization readers PRODUCTION-hardcoded; `record_realization_revision` (`bridge_store.py:549`) has NO production producer — R11/A4c closes it; sandbox-scoped rows required (`bridge_store.py:125,776,782,795-800,900`). V4 serving unreachable (`contract.py:776`; `gate1.py:1161` vs `:1308`). V5 generation lane blind to plans (`selection_formula_binding.py:32,41-42`; `restore_formula_v3.py:91`; `admission_v2.py:94-100`; `joins.py:920`; 1072:83); drafts precede selection. V6 renderer `{schema}.{table}` keys vs `catalog::schema.table` wiring (`render/project.py:348-350`; `wiring.py:199`). V7 1120 CHECK closed; 1121 applied+immutable. V8 two decision authorities disagree live (`activation_policy.py:37,152` vs `action_dispositions.py:128,136`); caller-assembled facts diverge; `action_available` "policy-available" vs the run rail's separate folds (`action_authorization.py:79`; `runs/projection.py:280`). V9 grain law (`posted_debit_amount` ACCOUNT grain; B10 UOA refusal; sole reviewed expectation; count recipe bare; demotion seam; blueprint COUNT_ROWS). V10 `FEATUREGEN_MATERIALIZE_INVENTORY` off; activation flag + signed gate; `PLAN_CONTRACT_VERSION` in vector; chooser address includes hypothesis; two chooser modules. V11 remediation NO-GO Tasks 4/5/8 → B0a/B0b; roles client-supplied BOTH routes (`build_sets.py:166→363`; jobs route + coordinator); registry: 1121-1129 theirs (1121 dead), 1117 unknown, ours 1130-1139 at T0. V12 the real lane: `feature_runs.py:134 prepare_run_code` → coordinator `_build` blind to new identities; `evaluate_publish_sandbox(verified_output_revision_id)`. V13 formula language sealed: `SelectionKind` = {TRANSACTION_DIRECTION, ELIGIBILITY} (`schema_v3.py:80`); single-source filter law (`expression_ir.py:939-945`); joined expressions refused (`build_operator_graph_v2.py:146`) — R12/Formula V4 is the sanctioned opening. V14 temporal authorities exist (`temporal_policy.py:162`; `boundary_v2.py:182` — LATEST_AVAILABLE named leakage); `CompilationIdentityV2` carries formula/IR hashes (`boundary_v2.py:621`); generate_v2 hashes actually-rendered files (:153).

---

# EXECUTION ORDER (round-12; each numbered item = one-or-more SDD dispatches, TDD, own commits)

**1. T0 — verification + the ONE migration mapping.** Re-verify V1-V14 + all twelve rounds' citations at then-current origin/main; `ActionRequestV1` call-site inventory; the jobs-route roles field; principal-resolver module path; 1117 owner; assign the authoritative 1130-1139 content→number mapping and update every task + the reservation table in ONE commit; record R8's split + R12's reversal in the ledger. *No feature code.*

**2. P1 — parent amendment.** Stage-2 gate split (implementation now / activation behind signed evidence); S2-P4 = the round-12 identity chain; S2-P6 split (source compatibility blocks EXECUTE_SANDBOX, never GENERATE_PREVIEW); R8 split recorded in both docs. *(Dispatchable with T0.)*

**3. Identity + temporal contracts (code-only):** the three plan layers (R9 logical incl. `LogicalTemporalJoinSemanticsV1`; physical incl. `PhysicalTemporalJoinBindingV1`; render profile + `GenerationConfigurationV1` + `MemberOutputContractV1`), canonicalization, the digest chain, R14's first-journey temporal ruling as defaults, the six temporal tests. (Absorbs old A4b/B3c-contract halves.)

**4. A4c — the provisional-realization producer (R11; was B2a).** Input: AI-proposed bridge fact, ordered endpoint members, execution context, current physical source bindings, intended traversal direction. Output: `BridgeJoinRealizationRevisionV1` + `BridgeRealizationCurrentV1` + dependency rows, applicability {purpose=feature_generation, environment, tier=SANDBOX}, cardinality UNKNOWN unless proven, safety UNASSESSED. The service: resolves every endpoint member; preserves composite-key order; refuses missing/ambiguous mappings; refuses hidden/unreadable endpoint columns; binds exact source revisions; records the AI-proposed evidence; reuses the EXISTING realization store + CAS pointer; idempotent on semantic content. Surface: `POST /selections/{id}/candidate-execution-plans` may server-create the provisional realization before returning candidates. Tests: complete mapping → provisional-previewable plan; incomplete → refuse; produced realization never enters the production executable reader.

**5. Persistence + relational bindings:** discovery substrate (A1 fact-loader framework + author-formula loader + adapters + registrations incl. the three NOT_RELEASED codes; A2 projection extension; A3 execution context; A4 snapshot; A5 logical-resolution planner change with declared identity impact + regenerated pins; A6 G2 serving gate + account-grain fixtures; A6b count recipe per R13; A7 demand vocabulary) then B0a (roles closed both routes; principal/data-scope revision, its own migration; frozen-claims-authorize/current-claims-gate recheck) → B1 (identity persistence) → B2 (total binding chain, relational agreement — legacy hashes as provenance pins; adoption + join-policy stores) → B0b (lineage/method constraints) → B2b (adoption + join-policy workflow APIs/UI).

**6. Formula V4 — the joined-attribute language (R12; replaces B3d's mechanism).** `JoinedAttributeSelectionV1` {relationship_role, joined_entity_role, attribute_role, semantic_operator, parameter_ref | semantic_value, logical_temporal_semantics_ref} — the journey's instance: {customer, onboarding_date, age_at_cutoff_lte, new_customer_days}. Full increment: `TypedFormulaProposalV4`, V4 author prompt + response schema, parser, canonicalizer + content hash, validation, critic, output authority, admission, replay, frozen author identity, method identity, evaluation corpus cases, compiler, operator graph, PySpark renderer. The new recipe `posted_debit_amount_for_eligible_accounts`-class features declare it; `posted_debit_amount` stays sealed.

**7. Compiler/IR/renderer:** the physical pipeline — transaction scan → transaction PIT filter → direction/eligibility filters → transaction uniqueness guard (R13) → current/previous window aggregates → customer scan → customer PIT/knowledge-time selection → governed temporal N:1 join → joined customer predicate → final calculation → customer spine landing. `expression.join_plan.steps` non-empty is no longer an automatic refusal (the OPENED path is exactly this typed shape; everything else still refuses); B4's provisional guards; C1's catalog-qualified dialect everywhere + wrong-catalog-spine refusal + RENDERER_VERSION bump.

**8. Run/direct coordinator integration (B3 + B3b):** authoring consumes the logical digest (request content, provider audit, evidence pins, validation; legacy drafts refused); `PinnedResolvedFeatureInputV3` through restore→admission→compile (pinned sole authority); the coordinator resolves logical binding + environment-matching adoption + render profile + generation configuration per member, builds only from combined bindings, mints the ordered digest chain; the direct route uses the SAME service (owner-ratified); remaining R1 loaders complete; B5 promotion machinery (R7); B6 artifact-bound fixture evidence; B7 the ONE chooser promoted.

**9. Decision-service capability alignment:** deployment-capability facts folded by the six-action service (the availability table above); every route/rail/button consumes the one verdict; EXECUTE_SANDBOX refuses before queueing.

**10. Serving merge + card UI (C2 + C3 + C4):** C2 serving under one activation verdict, synchronous identity persistence, serving ceiling, first-serve ranking, version-vector bump same commit, byte-identity inactive. C3 — `semantic_feature_id = logical_digest`; `served_plan_variant_id = H(logical_digest | ordered logical relationship path)`; physical plans DO NOT enter card identity; merge rules: same logical_digest (engine+governed) → ONE card with provenance + plan variants; two relationship paths → one card, two execution options; different parameters or temporal semantics → different features; evidence wording → no rekey; withdrawn bridge → that plan variant unavailable; two formulas reading the same columns NEVER collapse; covers DTOs, persistence, draft, confirmation, ranking (slot dedup by logical_digest, disclosure pins). C4 — the four-section home, provenance, guard list, execution-truth states, production not-released copy, backend-driven states only, accessibility.

**11. Journeys (public APIs, CI fixture validation only; every journey starts at the hypothesis/run endpoint — NEVER seeded via test helpers):**
- **THE DECISIVE JOURNEY (round-12; replaces D3b):** Hypothesis: *"Find newly onboarded CIB customers whose outgoing payments increased sharply during the last 30 days."* Customer input `cib::public.bo_cib_customer` {cust_num, onboarding_dt, valid_from, valid_to, knowledge_ts}; transaction input `ftr::public.comp_financial_tran_repos_dly` {cif_id, tran_id, amount, direction, pstd_date}; AI-proposed link `cust_num ↔ cif_id`. Expected feature, for customers onboarded within 180 days as of cutoff: `SUM(debit amount, last 30d) − SUM(debit amount, previous 30d)`. Flow: submit hypothesis → cross-catalog recommendation → select → server creates the provisional realization (A4c — no test-helper seeding) → confirm physical plan + policies → author Formula V4 → build set → generate preview → inspect the sealed Kedro/PySpark artifact. Assertions: both catalogs on the card; "AI proposed" provenance; logical identity survives card→selection→formula; physical identity starts at adoption; formula + IR hashes inside compilation identity; two catalog-qualified datasets in the rendered files; customer selected as-known-at-cutoff; post-cutoff correction excluded; duplicate `tran_id` fails by name; unknown cardinality emits the guard; known N:1 produces the expected values; run route and direct route return the SAME canonical artifact; actual project bytes match `project_digest`.
- D1 no realization→(now) A4c produces one — the journey asserts the pre-A4c blocker AND the post-A4c continuation; D2 unknown-cardinality provisional (A6b count recipe; guard pass/fail); D3 known N:1 full preview + two-member order-reversal digest pin; D4 M:N final-grain block + source-grain intermediate; D5 value journeys split count/monetary with pinned values and named refusals (R13's refusal on the duplicate fixture; FX/enrichment refusals); D6 temporal leak fixtures (the six R14 cases); D7 LLM-origin promotion journey.

**12. Operator activation (explicit user go):** migrations backend-first; telemetry + worker; inventory + mappings + sandbox realization seeding via A4c; satisfaction projection; targeted-cohort activation ONLY after P1's amended gate; SME thresholds + wave-1 gate BROAD; post-sandbox-worker empirical-quality milestone (coverage, null rate, stability, leakage, redundancy, incremental performance, fairness/drift).

# Not in scope (chartered): deduplication policies + survivor operators (R13); M:N allocation policies; full dimension-enrichment beyond the R12 joined-attribute predicate; full S2-P6; public sandbox execution + operational join observations; federated execution; propose-bridge surface; promotion beyond R7's class.

# Execution
SDD; T0+P1 first (dispatchable now); ledger `.superpowers/sdd/<plan-basename>/progress.md`; final whole-branch review (most capable model); every deploy/flag = explicit user go.
