# Adversarial Plan Review — Semantic Enrichment Context + Catalog Profiles (2026-08-01)

**Plans reviewed:**
- `docs/superpowers/plans/2026-08-01-semantic-enrichment-context-consumption.md` ("semantic plan")
- `docs/superpowers/plans/2026-08-01-catalog-profiles-source-temporal-crosswalk-rebased.md` ("profile plan")

**Method.** Five independent adversarial review passes (contracts+semantic Tasks 0–3; semantic Tasks 4–12;
profile §2/§6.1–6.3/Release A; profile §5/§6.4–6.6/Releases B–C; cross-plan + product coherence), every claim
verified against a read-only worktree of `origin/main @ fa9a20b0`, plus the bridge (2026-07-29),
ingestion-richness (2026-07-31), codegen-remediation (2026-07-31) and superseded profiles (2026-07-30) plans,
the codegen adversarial review, and `docs/DEFERRED-WORK.md`. The root checkout is a stale branch and was used
only for plan documents. All `file:line` citations below are against `fa9a20b0`.

**Two corrections to earlier session notes.** (a) `field_authority.py` EXISTS — at
`src/featuregen/overlay/field_authority.py` (not `overlay/upload/`); the profile plan's §2 citation is correct,
though its bare-filename Modify lists elide the package split. (b) The bridge programme's modules ARE merged to
main (migrations 1036–1038 present) — but large parts are **unwired** (see A1).

---

## Verdict

Both plans are architecturally literate and mostly verified-true in their baseline claims — the "already
present / still absent" inventories are ~95% accurate, the functional rules match the product invariants
(AI-proposed usable without confirmation; review never implies safety; population explicit), and the corrections
to the superseded plan are almost all right. **Neither plan is executable as written.** The review found
13 cross-cutting blockers, ~45 majors, and a set of pre-existing main-branch bugs the plans unknowingly build
on. The dominant failure mode is the one the plans themselves warn against: describing existing code from
memory. Contracts are frozen against field names, vocabularies, readers and harnesses that do not exist on
main, while the things that DO exist (egress allowlists, schema registries, snapshot comparators, two-endpoint
observation stores) actively reject the planned extensions.

---

## A. Cross-cutting blockers

### A1. Release B/C exit criteria depend on subsystems that exist but have zero production callers
The materialize chain is end-to-end orphaned: `compile_ir` (`materialize/ir.py:229`), `authorize_compilation`
(`ir.py:540`), `render_project` (`render/project.py:1095`), `prepare_run` (`runprep.py:732`), `submit`
(`submit.py:131`), `select_publisher` (`publish.py:540`) — each has no caller outside its own module/tests.
The analysis execution path is unreachable: `run_analysis` (`data_agent/analysis.py:329`) has no `src/` caller
and `_execution_inputs_or_none` unconditionally returns `None` (`api/routes/analysis.py:199`). The V2
observation/admission pipeline is test-only: `record_relationship_observation` (`data_agent/store.py:171`) and
`evaluate_bridge_admission` (`bridge_admission.py:321`) have no `src/` callers; the one production executor
call site discards the V2 object (`data_agent/relationship.py:314-317`). All 33 findings of the codegen
adversarial review remain open on `fa9a20b0` (`git diff a2203bc9..HEAD` — zero files changed under
`materialize/`). Profile §2 lists these as "Already present—reuse"; §3's ownership table treats `materialize/`
as healthy. **Fix:** name `2026-07-31-codegen-review-remediation.md` as a hard predecessor of Release C (and of
semantic Task 8's end-to-end acceptance), and re-scope every "observable behavior" exit criterion in Releases
B/C to the wiring that actually exists.

### A2. The "existing sanitization seam" is a closed whitelist that fail-closes on every planned field
Three independent seams, all blocking:
- `sanitize_feature_context` traverses `columns[*]`/`table_context[*]` against closed key sets
  (`enrich_llm.py:213-222`); any unclassified key returns the block tuple (`:307`, `:337-338`) → an
  `EGRESS_BLOCKED` event and **no dispatch** (`:940-945`). Every field semantic Task 8 adds (concept ancestry,
  namespace/issuer, party role, grain/time facts, links, missing-context codes) is unclassified. `_structural_ok`
  also rejects non-str values (`:277-278`), so tuples like `concept_path` fail even under an allowed key.
- `_fact_wrapper_ok` requires `authority in ("governed","hint")` (`enrich_llm.py:227-236`) — the plan's mandated
  `llm_proposed` label is structurally rejected. The plan's "authority visible" property is unrepresentable in
  the shape the guard admits.
- Batch path: `_ITEM_META_ALLOWED` (`enrich_llm.py:1061-1083`), `_COLUMN_PROFILE_KEYS` (`:1097-1101`),
  `_ROSTER_ENTRY_KEYS` = {column, operational_type, declared_type} (`:1169-1176`); a violating item is excluded
  and **terminal for the run** (`enrich_batch.py:229-233`) — the exact "unexplained zero-output stage" Task 10
  bans.
Conversely the single-call path never visits unknown TOP-LEVEL keys (`_redact_free_text_meta` intersects with a
closed allowlist, `enrich_llm.py:168`), so a new key egresses **unscanned** — wrong in both directions.
`enrich_llm.py` is absent from semantic Task 1's and Task 8's file lists. **Fix:** each purpose adapter needs an
explicit allowlist/wrapper extension task in `enrich_llm.py`, with golden egress tests per new key.

### A3. Schema-registry version traps produce silent total outages, twice
The registry comment states the failure verbatim: "Registering 2 but requesting 3 makes `schema_for(id,3)`
return None, structured output unenforced, and the response fail repair: feature generation returns NOTHING
with the flag on" (`enrich_llm.py:769-773`). (a) Semantic Task 8's "flag-on uses contract version 4": only
versions 2 and 3 are registered (`enrich_llm.py:773-775`); bumping `_feature_schema_version()`
(`feature_assist.py:209-212`) without a new registered schema body = silent empty feature generation. (b)
Profile Task 4's "extend the existing Pass-B result": `overlay_table_synth_batch` v2 is a byte-alias of v1 with
`additionalProperties: false` at every level (`enrich_llm.py:560-577`, `:752-761`); new fields require a real
v3 body + prompt version bump (`table_synth.py:362-363`, `:509-547`). Neither plan lists the registration edit.

### A4. `SemanticValueV1.authority` is a lossy re-encoding of the real authority model, and three contracts bind to it
The plan's flat 5-vocabulary (`source_attested | human | governed | deterministic | llm_proposed`) claims to
"reuse existing terms" (semantic:216-217). Reality: `EvidenceProducer` has EIGHT members (`overlay/evidence.py:23-30`)
× `AssertionStrength` (PROPOSED/SUPPORTED/ATTESTED/CONFIRMED) × a separate `EvidenceLifecycle`, plus an
unrelated `OperationalColumnFacts.authority ∈ {governed, hint}` (`column_authority.py:52`). `parser`,
`profiler`, `taxonomy`, `structural_connector`, `legacy` have no target; `SOURCE`+`PROPOSED` (a live policy
input, `field_policies.py:47`) is unrepresentable; `STALE`/`REJECTED` evidence has no status. Profile §6.2 and
§6.4 (`PolicyProvenanceV1`) inherit the same vocabulary by reference. This is the "another precedence engine"
semantic Rule 1 forbids. **Fix:** carry `(producer, strength, lifecycle)` as-is, or define the projection
table explicitly with a lossless escape field.

### A5. The relationship/observation context contracts cannot represent the data model that exists
- `RelationshipContextV1` is a flat single-direction record; one available link on main carries **0..N
  directional realizations**, each with its own lifecycle/safety/cardinality (`entity_map.py:96-122`,
  `load_current_bridge_realizations`). A link with A→B and B→A realizations is unrepresentable — the builder
  must silently pick one, which is the plan's own "invert a relationship direction" must-die mutation.
- Seven pinned identity fields have zero repo hits: `definition_revision_id`, `execution_revision_id`,
  `assessment_revision_id` (real name: `candidate_revision_id`, `bridge_assessment.py:424`),
  `applicability_scope_hash` (real: `scope_id`, no stored hash, `bridge_realization.py:206-232`),
  `leg_plan_hashes`, `leg_realization_revision_ids`, `mapping_dataset_ref`. No "shared lifecycle reader"
  exposing assessment+realization+execution in one read exists; the only composer is `entity_map._link_view`
  (`entity_map.py:171-185`), which the plan forbids duplicating. No execution-revision concept exists at all.
- `ObservationContextV1` mismatches the real store on ~9 fields: no `observation_kind`, no single `direction`
  (the store is inherently two-sided, `relationship_observation.py:327-328`), `evidence_basis` collapses two
  orthogonal CHECK-constrained axes (`method` exact/approximate × `row_coverage` full/sampled/partial,
  `1038_relationship_observation_v2.sql:17-21`) and invents `governed_key`/`source_constraint` with no source;
  `binding_revision_ids` as a tuple destroys the left/right side assignment the plan's own mutation test
  protects; no `lifecycle_status`/`expires_at` columns exist. Task 12 "freezes" an interface main already
  implements — with the wrong shape.
- `availability: discoverable|sandbox_only|executable|unavailable` folds safety into availability
  (`LinkAvailability` has two members, `bridge_assessment.py:463-465`; safety/review/lifecycle are deliberately
  independent axes per bridge Decision G) — and it is ambiguous between the pure predicate
  (`eligible_for_production`, `bridge_realization.py:376-384`) and the revalidating reader
  (`bridge_store.py:886-909`); choosing the pure one displays "executable" for a moved binding, violating the
  plan's own release bar.
**Fix:** rewrite `RelationshipContextV1` as link + tuple-of-directional-realizations mirroring the real
readers; make `ObservationContextV1` a faithful projection of `RelationshipObservationV2`; drop the invented
identity fields or add them where they belong (Release C defines definition/execution revisions — see D4).

### A6. No reachable path to load-bearing `authority_role`/`temporal_storage_model`; the new edit surfaces have no authorization story
- Source-attested is impossible: `_SOURCE_FIELDS` (`ingest.py:921-927`) carries no temporal/authority field.
- Deterministic-profiler is impossible: the superseded plan's temporal probes (Task 4B) were dropped in the
  rebase with no replacement and no §12 deferral entry.
- Human-confirmed is blocked: the generic command requires `human_editable=True` — true only for
  `unit`/`currency` (`field_policies.py:191-199`; `field_correction.py:250-256`) — and `require_confirmer`
  resolves to the raw `platform-admin` claim (`api/routes/assets.py:97-98`, `api/deps.py:81-91`). The
  "dedicated existing confirmation path" profile Task 1 cites does not exist (each operational field today has
  a bespoke command or none). The "ordinary uploader may submit a visible proposal" has no counterpart:
  `data_owner` holds only `catalog:read/write` (`identity/permissions.py:48-50`) and cannot call
  `propose_override`.
- `set_advisory` is net-new, writes HUMAN/**CONFIRMED** directly (a strength `propose_override` deliberately
  does not grant, `field_correction.py:410-422`), bypasses `proposer_ne_confirmer`/`uploader_ne_confirmer`
  (`overlay/authority.py:208-235`), and names no role. Its "purely advisory" scope as defined would also cover
  `definition`/`concept`, silently removing four-eyes from fields that have it today.
- Net effect: profile Task 7's rule "temporal policy must agree with a load-bearing profile model or remain
  non-operational" makes Release B **inert for upload-only catalogs** unless the Release-B policy itself is the
  operational declaration — which the plan gestures at (Task 3) but never commits to. **Fix:** decide explicitly:
  either `human_editable=True` + named role for the two new fields, or declare the §6.5 policy the only
  operational path and say Task 1's evidence work is display-only.

### A7. Snapshot pinning as specified silently drops fields, then refuses every execution
`build_metadata_snapshot` filters unknown fields with no error (`feature_metadata_snapshot.py:426`,
`_KNOWN_FIELDS` `:49-59`); every item is hardcoded `item_kind="column_field"` (`:388-391`); `item_hash`
**excludes** `item_kind` (`:372-382`) so same-payload items of different kinds collide and get deduped
(`:433-435`, `ON CONFLICT DO NOTHING`); and `compare_snapshot_to_current` re-derives every row as a
column-field item (`:528-530`) so any new kind reports `SNAPSHOT_ITEM_DRIFT` forever. Task 8's "refuse stale
pins" would refuse ALL executions once new kinds land; the silent-skip path makes a partial implementation look
green. Also the two plans disagree on the pin list: semantic Task 8 pins six (incl. physical binding revision);
profile Task 9 defines five item kinds but its own identity list one line earlier names six.

### A8. Two-leg crosswalk compilation is new planner architecture, not "composition of existing steps"
`expression_ir.py:946-969` is an either/or: one same-catalog traversal OR exactly one cross-catalog
realization; the refusal is documented in-code ("a grain assembled from two independent joins would need two…
which no single plan can state", `expression_ir.py:929-934`); `plan_cross_catalog_join` returns exactly one
element (`joins.py:390-395`). `ColumnPairV1` refuses non-`public` schemas (`bridge_realization.py:93-95`) —
a mapping table outside `public` is unrepresentable — while same-catalog `plan_join` uses a different address
grammar (`expression_ir.py:1100-1102`). Gate 2 (`ir.authorize_compilation`, `ir.py:540-594`) checks the raw
`sensitivity` column, not `visible_requires` (migration 1032; `read_scope.py:58` adopted by 26 files, none in
`materialize/`), never unions `step.predicates` (`ir.py:456-463`), and addresses non-cross-catalog endpoints
via the expression's single catalog (`ir.py:447-455`) — mis-authorizing a second leg in another catalog.
Also: the two-endpoint observation store cannot express composed cross-leg fan-out, atomic two-leg as-of, or
partition/predicate pinning with columns (A5); `max_left_matches_per_right_row` gates nothing today
(`store.py:117-123`) so "refuse reverse direction independently" has measurement but no verdict path.

### A9. The evaluation/release gates are unrunnable as specified
- Semantic Task 10 requires a same-model thin-vs-rich comparison + token/call cost + retrieval lift **before**
  Gate A, with no STOP — a live LLM run rule 10 forbids without approval; its jointly-scheduled counterpart
  (profile Task 6) states the opposite explicitly. One of the two must change.
- Profile Task 6's "replayed structured results" is impossible for Pass B: `table_synth.py` never touches
  `structured_results` (store's only producers are the two critics); Pass-B outputs are recomputed or lost.
- The mutation harness both plans cite ("must-die sentinel", "must-survive no-op") does not exist; bridge
  Task 12 owned building it and is the one unlanded bridge task. Neither plan allocates it.
- "Literal baseline test count" is not reproducible: DEFERRED-WORK §C records ~82 environment/ordering-dependent
  whole-repo failures from cross-test DB contamination. The gate must be scoped to named focused suites.

### A10. Migration-ledger reality is worse than the plans state
Duplicate numeric prefixes on main: **0973, 0974, 1034, 1036, 1037, 1038, 1040**. The runner applies `*.sql` in
lexical filename order, ledgered by full name + checksum (`db/migrations.py:260-317`) — tolerant, but (a) a
fresh DB and the live DB apply the interleaved duplicates in different relative orders (fresh-vs-live
divergence), (b) "record the migration head" must record the applied **name set**, not a number, and (c) "next"
= 1044 is **already reserved** by the codegen-remediation plan (`2026-07-31-codegen-review-remediation.md`:
"Next free migration number is 1044"). Both plans' "allocate at execution time" also permits semantic Task 2
and profile Task 2 to allocate in parallel — the exact double-allocation already in the ledger. **Fix:** a
shared reservation doc (the bridge plan's `verified-interfaces` pattern) covering all three in-flight plans.

### A11. No content-hash canonicalization is specified, and the repo has two incompatible schemes
Scheme 1: RFC 8785 JCS via `materialize/canonical.py:33 materialize_hash` (used by `physical.py:235`).
Scheme 2: `json.dumps(sort_keys=True)`+sha256 (`field_evidence.py:38-46`; used by `bridge_realization.py:320`,
`structured_results.py:90`, `feature_metadata_snapshot.py:372`). ~20 inline variants differ on `ensure_ascii`/
`default`/`separators`. Both plans' hashes (`content_hash`, `profile_hash`, plan identity) leave the choice to
the implementer — semantic Task 1's "identical bytes from both builders" proof then depends on two implementers
guessing alike. Related: `AnalysisExecutionIRV1.plan_hash` is a raw `"|".join`+sha256 with **no version token**
(`data_agent/analysis.py:175-192`), and "AnalysisExecutionIRV2" is referenced but its V1→V2 delta is never
defined; nothing persists an analysis plan today at all.

### A12. The flag matrix is undefined and the rollback story is broken
- No task in either plan ever enables `FEATUREGEN_DATASET_PROFILES`; semantic Task 11A enables only
  `FEATUREGEN_FEATURE_CONTEXT`. Release A's UI/search/graph surfaces are built and stay dark.
- `FEATUREGEN_FEATURE_CONTEXT` is absent from `deploy/kind/k8s/20-backend.yaml`, `.env.example`, `.env.demo` —
  off in kind today; flag-off returns an **empty** `table_context` (`feature_assist.py:424-425`), so profile
  Task 5's feature-consumption work no-ops in the default state and Task 6's flag-off check passes vacuously.
- Task 8's v4 bump makes the flag a rollback to **v1** (thin menu), not to today's shipped v3 — no switch
  restores current behavior.
- `FEATUREGEN_CROSSWALK_EXECUTION=1` + `FEATUREGEN_SOURCE_TEMPORAL_SELECTION=0` reproduces profile Task 13's
  own must-die mutation (uniqueness measured before time filtering); the inter-flag dependency is never gated.
- There is no flag registry; existing flags use two different truthy conventions (`=="1"` vs
  `{"1","true","yes","on"}`).

### A13. The signed activation interlock is never mentioned
`FEATUREGEN_INTENT_LIVE_CROSS_CATALOG` + Ed25519 `live_activation` (startup artifact check `api/app.py:52`,
gate verification `planner/signing.py:87`, `live_activation.py:196,211`, migration 1002, routes
`api/routes/gate.py`) is the standing mechanism: every customer-visible cross-catalog feature sits behind a
governed activation. Semantic Task 7 (cross-catalog links in Asset Detail) and profile Release C (crosswalk
execution + UI) add exactly such surfaces; grep of both plans finds zero mentions of the interlock.

---

## B. Pre-existing main-branch bugs the plans build on (found during review)

1. **`logical_ref_of` mis-parses TABLE refs** — 2-part refs (`public.orders`) parse as (table=`public`,
   column=`orders`) → phantom logical ref (`column_authority.py:66-77`). Callers: `field_correction.py:196,272`,
   `asset_detail.py:694`. A field decision at a table anchor computes CAS on the phantom ref, writes evidence
   there, and re-projection UPDATEs 0 rows — a silent no-op. Profile Task 3's table-profile PUT sits directly
   on this path. (Independently confirmed.)
2. **Table nodes are world-visible in search** — `visible_requires = {}` for table nodes
   (`overlay/upload/catalogs.py:47-49`; migration 1032 derives only from sensitivity, which `build_graph` never
   sets on tables, `graph.py:246-252`). Task 5's "searchable only when the caller can see the table anchor" is
   currently a no-op; profile prose on table nodes becomes matchable by any `catalog:read` caller. A derived
   table scope (EXISTS visible column) is unplanned new work.
3. **Gate 2 under-authorizes governed-restricted columns** — compile-time authorization compares
   `allowed_sensitivities` to the raw `sensitivity` column (`ir.py:523-535`); governed `restricted` columns
   carry `sensitivity=NULL` and pass. Predates the plans; Release C extends this gate.
4. **Table display projections are wiped on re-upload unless Pass B runs** — `build_graph` deletes all nodes
   (`graph.py:240-241`); the only unconditional table-ref reprojection is inside the Pass-B block
   (`ingest.py:2524-2530`) gated on `table_synth_enabled()` (default off). Evidence survives; display columns
   and facets go blank.
5. **`record_gap` race / `resolve_gap` non-idempotent** — SELECT-then-INSERT with no `ON CONFLICT`
   (`learning.py:181-196`); `resolve_gap` mints a fresh id per call (`:238-245`). Task 8's "retries are
   idempotent" is true only sequentially.
6. **Learning gaps store has no producer** — `record_gap` is reachable only via `run_analysis`, which has no
   caller; `GET /learning/gaps` reads a table nothing populates. Routing new refusals there without wiring
   `run_analysis` is metadata rot (profile correction 12 forbids exactly this).
7. **`_vocab_fingerprint` is declaration-order-sensitive** — `json.dumps([...])` over an unsorted list
   (`enrich.py:86-92`); reordering registry entries churns replay identity with no meaning change (the critic's
   fingerprint sorts; this one doesn't).
8. **Entity Map links are deliberately un-scoped** (`entity_map.py:20-27`) — semantic Task 6's "all surfaces
   return the same current value" is already false by design on read scope; the plan must state the exception
   or change the design.

---

## C. Semantic plan — remaining majors by task

**Task 1 (bundle).**
- No roles/read-scope parameter on either builder; `read_column_facts`/`read_operational_value` are un-scoped
  (DEFERRED-WORK B-1 #8) and the bundle is exposed over HTTP (Task 7) and to the data agent (Task 9) —
  a restricted column's name/semantics can enter another column's context. Widest blast radius of the known
  deferral; must be closed, not inherited.
- `bundle_from_store` composed of per-field readers is an N×M fan-out; `read_operational_value` runs
  `check_projection_readiness` on **every call** (`operational_facts.py:444-451`); per-link realization+
  dependency queries (`entity_map.py:152-168`). This is the `_load_columns` 157-scans defect class again —
  batching must be a stated requirement.
- `missing_context` vocabulary is consumed and hash-load-bearing from Task 1 but only defined in Task 5;
  profile Task 1 consumes it too, and profile adds an undefined fourth vocabulary (`unresolved_reason`).
- Builder byte-identity breaks for non-`public` schemas: graph refs are public-flattened (`ingest.py:560`;
  `column_authority.py:57-65`) while upload sidecars carry the real schema — `object_ref`/`table_ref` are
  exactly the shared fields that differ.
- `concept_path` undefined for `unclassified` (a sentinel, not a registry member, `concepts.py:22,1095-1097`);
  is_a cycle check is order-broken (`seen.add` before membership check, `concepts.py:1071-1073`) so self/mutual
  loops validate today.
**Task 2 (entity/issuer).**
- Retargeting `counterparty_id.entity_link` → `customer` **re-keys governed bridge fact keys** (`fact_key`
  hashes `entity_id`, `bridge_candidates.py:220-221`), orphaning confirmation streams; breaks three pinned
  tests (`test_bridge_namespace_pairing.py:186-248`), silently shrinks `known_entities()` (clears
  `target_entity='counterparty'` recognizer values, `taxonomy/dimensions.py:102-109`), and reverses a decision
  the ingestion-richness plan landed deliberately. Historical reads do NOT flip via the registry alone: the
  persisted `graph_node.entity` wins (`bridge_grounding.py:480-484`) and `axis_projection` only fills blanks
  (`axis_projection.py:167-168`) — a backfill + search-doc rebuild is required and unplanned. Task 2 needs a
  migration/reconciliation plan and a bridge-programme handoff (it edits `bridge_candidates.py`, a file the
  plan assigns to the bridge programme; `bridge_grounding.py`'s independent namespace verdict is not in its
  file list).
- `identifier_scope.py` would be a third namespace-conclusion surface beside `Concept.namespace` (blocking key)
  and the governed `identifier_namespace` fact (`bridge_grounding.py:485-487`, hard-conflict path `:648-653`);
  issuer must be folded into `assess_grounded_identifier_link` or the truth table is bypassable.
- `LEGACY_CONCEPT_ALIASES` duplicates the existing `_LEGACY_ALIASES` mechanism (`concepts.py:1100-1114`) —
  extend, don't parallel.
- Tasks 2 and 3 each force a 100% concept-cache miss (`_CONCEPT_CACHE_VERSION` embeds the fingerprint,
  `enrich.py:102`) = full paid re-enrichment; neither exit says so.
**Task 3 (replay identity).**
- The fingerprint field list exceeds what is rendered (only `{name, group, hint}` egress today,
  `concepts.py:1107-1114`), contradicting its own "not-rendered fields don't invalidate" property test until
  Task 4 widens the payload — the two checkboxes are unsatisfiable in the stated order. Sequence the fingerprint
  change WITH the payload change.
**Task 4 (LLM context).**
- Sibling roster makes per-column cache/evidence identity per-TABLE: `concept_cache_key` and `field_input_hash`
  share `_concept_metadata` bytes (`enrich.py:132-134`, `:582-592`) — one column's reclassification stales and
  rewrites every sibling's evidence, permanently.
- "Preserve batch bounds/deadlines" is contradictory while tripling item bytes: per-task token/item budgets
  (`enrich_config.py:27-31`), `estimate_tokens` measures item metadata (`enrich_batch.py:94-115`), 32-call/240s
  ceilings → systematically higher truncation, not equivalence.
- Deterministic contradictions aren't computable from the wide-table roster ({column, operational_type,
  declared_type} only, `table_synth.py:387-395`) and glossary catalogs have `operational_type="unknown"`
  uniformly — name the signal per contradiction or they misfire on every FTR table.
- Witness `pstd_date` appears nowhere in the repo (1 hit, in a plan doc) — the gold set must synthesize it.
- The critic-only-for-identifiers premise and "deterministic contradictions first" are already implemented
  (`enrich.py:1131-1134`; `concept_critic.py:329-363`) — the new work is the monetary/temporal/label rule set,
  which has no cited rules.
**Task 5 (adjudication).** Stage outcomes (selected/unchanged/…) are not stage states — the 0996 CHECK
constraint enforces a closed set (`stage_report.py:40-44`); they belong in stage `detail`, and `stage_report.py`
is not in the file list. The subject/current pointer is genuinely new but should copy the existing
immutable-revision+CAS pattern (1033/1038); `llm_call` provenance linkage already exists.
**Task 6 (supersession).** `_write_llm_field_evidence` is documented "SUPERSEDE-AND-REWRITE, unconditionally…
no unchanged-detection" (`enrich.py:630-636`) — same-value reruns mint new evidence IDs for five of six LLM
fields, so "same current value AND evidence ID" requires changing a writer the task doesn't name.
Projection-lag detection exists only for the feature path (`check_projection_readiness`) — extending it to
asset detail/graph/search is real work, correctly scoped but unbudgeted.
**Task 7 (context graph).** `lineage_graph` returns a boolean `truncated`, no per-kind counts (`lineage.py:99`),
and `_prune_to_neighbourhood` drops nodes with no accounting — but `lineage.py` is listed reuse-only. A separate
`/context` route breaks the dossier's single-transaction `consistency_token`/ETag contract
(`asset_detail.py:30-33`) and duplicates the existing Relationships section (`asset_detail.py:336-546`);
strongly consider serving the Context tab as a dossier section instead.
**Task 8 (feature-gen).** Byte budget: `FEATURE_CONTEXT_BYTE_BUDGET=60_000` with `ContextTooLarge` raised when
mandatory columns alone exceed it (`feature_assist.py:308,402-405`) — richer per-column bytes convert real
111/126-column catalogs into whole-request rejects; re-budget or paginate. The end-to-end Kedro acceptance
harness exists only as opt-in `l0_gate.py` (not `test_*`, needs `FEATUREGEN_L0_PYTHON`) — the acceptance must
state its environment. `DatasetNeedV1` requires an `execution_tier` semantic Task 8 never supplies.
**Task 9 (retrieval).** The plan inverts the legs: grain/time is leg 1 BY DESIGN with the inversion documented
as the defect ("Getting that ordering backwards…", `retrieval.py:16-19,164-176`); renumber to expansion-as-leg-3
additions. `intent.py` has no versioned input contract to "modify" (unversioned `IntentCandidates`), the
closed-candidate validator rejects refs outside the offered set (`intent.py:216-244`), and metadata-placement
changes have a recorded repair-exhaustion history (`intent.py:355-360`).
**Task 11.** Gate A cannot set the flag without adding it to the deploy surface (absent from all manifests);
no migration-aware smoke harness exists; Gate B's witness list drops ~8 of ingestion-richness Task 6's
acceptance targets (party_role coverage, currency 6/6, stamps 2/2, decoy closure, byte-identical human facts) —
absorbing the run without the acceptance loses them.

---

## D. Profile plan — remaining majors by release

**Release A.**
- Search: a "derived data role" facet is inexpressible — facets are literal `graph_node` columns
  (`search.py:15-27,125-141,204-211`); either project a `data_role` column (contradicting correction 4's
  spirit — resolve explicitly) or drop the facet. Table `definition` never enters the search doc (hardcoded
  `""` slot for tables, `graph.py:127-128`) — FTS change required, not a read-time join.
- Technical catalogs have NO table narrative source (only glossary table-term records write table evidence;
  `_write_technical_source_evidence` has no table branch, `ingest.py:1228-1265,1751`) — for them
  `business_context` is the only table text; say so.
- `EffectiveProfileFieldV1` has no vocabulary for "nobody said anything" (resolver skips evidence-less fields,
  `field_resolution.py:394-403`) and every RECOMMENDATION field returns `influence_not_operational` by
  construction (`field_authority.py:296-297`) — six of nine fields would render failure-shaped "unresolved"
  forever, violating the no-"blocked" product rule. It also restates `FieldResolution` field-for-field
  (`field_authority.py:196-204`) — declare it a typed re-wrapper.
- Task 2's "profile reconciliation/audit script" does not exist (Create, not Modify; precedent:
  `stamp_reconcile.py`). Task 1's `crosswalk` alias lives in `table_vocab._ROLE_ALIASES` (not listed); touching
  `TABLE_ROLE_ENUM` instead re-versions the Pass-B prompt.
- The ingest source lock is held through LLM enrichment (`ingest.py:1890-1898`) — a profile PUT can block for a
  full Pass-A/B run; the catalogs list is scoped by visible COLUMNS (`overlay/upload/catalogs.py:50-59`) so a
  narrative pointer can outlive a visible catalog (existence oracle). `grain_fact_ref`/`availability_fact_ref`
  have three candidate identities on main (graph event ids / overlay fact_key / confirmed_event_id) — pick one;
  it's inside `profile_hash`.
- §3 needlessly serializes Tasks 1–3 behind the bundle freeze (they don't consume it), while the REAL gate is
  understated: the two plans state different gates for the same handoff (semantic:77 "Tasks 0-3 freeze" vs
  profile:127 "after `SemanticContextBundleV1` is frozen" = after semantic Task 1) — under the looser reading,
  profile Tasks 1–3 run concurrently with semantic Task 2's edits to `concepts.py` and parallel migration
  allocation. Make the gate task completion, explicitly.
**Release B.**
- §6.5 names `data_agent.dimensions.AttributionPolicyV1` — the class is `DimensionAttributionPolicyV1`
  (`dimensions.py:70`); worse, `CURRENT_RECORD` maps to a basis that is declared and **refused**
  (`_SUPPORTED_BASES=(REPORT_CUTOFF,)`, `dimensions.py:65,83-87`) and `LATEST_SNAPSHOT_AS_OF` has no
  analysis-path implementation (the only latest-as-of logic is `materialize/spine.py:163-196`, unimported by
  analysis) — two of three selection kinds are new engine work presented as adapter emission.
- `resolve_table`'s derived branch returns an **unrecorded** in-memory binding (`binding_store.py:207-216`);
  `record_binding` is an upsert that writes no revision and has zero `src/` callers, while the revision writer
  lives in `physical.py:247` — and the codegen review records the two paths fork `physical_id`. Task 7's
  one-line "make record_binding persist the revision" is a reconciliation decision across two divergent models,
  and without it `selected_binding_revision_id` violates the observation FK.
- "Sandbox may rank a proposed authority value" has no hook: zero `sandbox` hits in `analysis/`;
  `ExecutionTier` is imported nowhere there; the single tier call is hardcoded `eligible_for_production`
  (`analysis/execution.py:205`). New capability, unbudgeted.
- Refusal codes require extending three closed vocabularies (`UNRESOLVED_CODES` `intent.py:88-99` + schema
  enum + `clarify.py` rendering; `GAP_CODES` `learning.py:59-71`; `REFUSAL_TO_GAP` which silently drops
  unmapped codes `learning.py:209-211`), and "SCD overlap" as a refusal contradicts the existing deliberate
  exclusion of `ATTRIBUTION_OVERLAPPING_RECORDS` from learning (`learning.py:93-100`) — re-argue, don't assume.
- Population enforcement is NOT in `grounding.py` (zero `population` hits) — it's spread across intent/assembly/
  execution/clarify, and `execution.py:172` guards with `if plan.population_table_ref:` so an undeclared
  population accepts any caller-supplied spine (pinned by `test_plan_to_execution.py:272`) — DoD #8 is not
  currently true at the execution bridge.
- Task 9's revalidation precedent is `ir.authorize_execution_realizations` (`ir.py:327-340`), not `runprep`;
  snapshot-tie refusal has no analysis-path producer; CAS pointer would be the fifth hand-rolled implementation
  (name the template — `bridge_store.py:610-630` is the strictest); policy routes: body-carried
  `expected_pointer_version` is the convention (`governance.py:136-139`), avoid `contract.py:181`'s optional-
  gate fail-open.
- `JoinLegPinV1.dependency_snapshot_ids` pluralizes a per-realization singular (`bridge_store.py:84-99`,
  `bridge_realization.py:252`); the only plural precedent has different (cross-segment set) semantics
  (`multisource_contracts.py:289`).
**Release C.** (Beyond A1/A5/A8.) Sandbox observation persistence (superseded Decision M — probe results
persist to the 1038 store) was dropped in the rebase without a deferral entry, removing the graduation
mechanism Release C's own sequencing depends on; Task 13's mutation list dropped its guard. Route collision:
`PUT …/{object_ref:path}/profile` nests a literal under a greedy path converter already claimed by the asset
GET (`assets.py:59`) — ambiguous for refs ending in `/profile`.

---

## E. Cross-plan consistency and product findings

1. **One value, three names:** `dataset_profile_hash` (semantic) vs `profile_hash` vs `selected_profile_hash`
   (profile). Profile Task 5 writes into the semantic plan's field using the other name. Name one canonical.
2. **`RelationshipKind` has no owner:** both plans define the same four members (semantic:159 as reused —
   false, zero repo hits; profile §6.6 as new in Release-C Task 10, last in the order, while semantic Task 1 is
   first). Assign the definition to semantic Task 1 and make profile Task 10 import it.
3. **Leg pins contradict:** semantic `leg_plan_hashes`+`leg_realization_revision_ids` vs profile Task 11's
   explicit "a bare hash is not enough" and five-field `JoinLegPinV1`. The wrong shape gets built first.
4. **Crosswalk-only revision pins:** `definition_revision_id`/`execution_revision_id` exist only after Release C;
   semantic Task 7 displays them "always". Mark them kind-dependent.
5. **Task-number refs:** all correct except semantic:77 "Tasks 1-3" vs its own execution order "Tasks 0-3".
6. **Superseded-plan drops with no §12 trail:** OPERATIONAL/AUDIT serving purposes (rationale "audit requires
   the system of record" — a bank-auditability promise), owner/steward, OpenMetadata table-description import
   (whose absence is a *documented live data loss*, `connectors/openmetadata.py:11`), the zero-cost
   `table_role` search facet, typed `dependency_kind` vocabulary + its invalidation safety rule, sandbox
   observation persistence, `open_gaps` demand ranking. Each needs a deferral entry or reinstatement.
7. **`catalog_profile_revision_id` semantics unstated in the rebase** (superseded plan said: authoring
   provenance, effective reads resolve the current pointer). If it's the current pointer, every narrative edit
   re-keys every dataset `profile_hash` and invalidates every pinned snapshot. Load-bearing; restate it.
8. **Pinned decisions still don't pin content** (DEFERRED-WORK A.1/content-snapshots): `profile_hash` +
   `binding_revision_id` don't survive an in-place source rewrite. One honest sentence needed so "replay"
   claims aren't over-read.
9. **Both plans' AI-usability posture is correct and consistent** (no confirm-as-availability gate anywhere) —
   but `unresolved_reason` as one free string cannot express the required "nobody decided / needs a data check /
   structurally unsuitable" split at the exact surface where it matters (Profile section).

---

## F. What was verified accurate (selected)

- Baseline SHAs real and adjacent on main; semantic plan's "already contains" list verified item-by-item
  (Pass A/B incl. wide-table two-phase, critic+structured store, dossier, Entity Map, namespace-grouped
  candidates, type attestation, feature-context v3 seam). Profile §2 "still absent" list fully accurate — no
  parallel implementations found.
- All six §6.6 bridge types exist with exactly the claimed names; `ExecutionTier` reuse correct;
  `binding_revision_id` is a deterministic content hash (CHECK-pinned); logical-only `IdentifierEndpointV1`
  well-formed; correction 8 (don't re-key bridges) and correction 9 (predicates are exactly three) verified.
- Rule 6 SCD half-open + overlap-refuse already true (`dimensions.py:113-115`, `assert_no_dimension_overlap`);
  correction 5's no-inference-from-`primary_entity` already satisfied; freshness removal matches DEFERRED-WORK
  A.1 verbatim; population-spine left joins guaranteed in renderer and SQL compiler; worked-acceptance
  fixture ingredients exist (zero-transaction customer, PIT boundaries, Hive pilot) minus crosswalk/snapshot
  variants; `DirectionalCardinalityVerdictV1` supports per-direction refusal; the runtime `JOIN_AMPLIFICATION`
  gate is written (`nodes_join_gate.py:131-146`) — though unreachable until A1 is fixed.
- The concept-critic already runs deterministic conflicts first and only for identifiers; five-field menu,
  contract v3, and "v4 is next" all confirmed; Task-0 file/test lists 100% present; the
  `verify_catalog_richness.sql` defects the semantic plan claims are real and precisely characterized.
- Bridge-plan ownership is real: Tasks 0–10 landed on main (Task 7 pending live Hive run; Tasks 11–12
  partial — including the mutation harness, see A9).

---

## G. Recommended amendments before execution (priority order)

1. Add a joint "Task 0.5 — verified-interfaces reconciliation": shared reservation doc for migration numbers
   (1044+ after codegen's claim), canonical hash scheme, canonical owner for `RelationshipKind`/
   `profile_hash`/`JoinLegPinV1`/authority vocabulary, and the real gate (task-completion) between the plans.
2. Rewrite `RelationshipContextV1`/`ObservationContextV1` as faithful projections of the readers that exist
   (link + directional-realizations tuple; V2 observation fields verbatim). Drop or relocate the seven phantom
   identity fields.
3. Add explicit egress/schema-registry tasks: allowlist extensions in `enrich_llm.py`, real schema bodies for
   feature-context v4 and table-synth v3, golden egress tests per new key.
4. Decide the authority/temporal confirmation path (A6) and name roles for every new PUT surface; scope
   `set_advisory` to the NEW fields only.
5. Insert the codegen-remediation plan as a named predecessor of Release C and of semantic Task 8's
   end-to-end acceptance; re-scope Release B/C exit criteria to reachable paths.
6. Fix the snapshot builder contract (error on unknown fields, `item_kind` in `item_hash`, kind-aware
   comparator) as a prerequisite of any new pin; unify the six-vs-five pin list.
7. Add a live-LLM approval STOP inside semantic Task 10 (or split the live comparison into Gate B) and
   allocate the mutation harness someone must build; scope test-count gates to named focused suites.
8. Fix the two pre-existing bugs the plans build on before their tasks land: `logical_ref_of` table refs and
   table-node visibility scope in search.
9. Define the flag matrix: who enables `FEATUREGEN_DATASET_PROFILES`, what v4-off rolls back to, the
   CROSSWALK⇒SOURCE_TEMPORAL dependency, and add `FEATUREGEN_FEATURE_CONTEXT` to the deploy manifests.
10. Re-add deferral entries (or reinstate) for everything the rebase silently dropped (§E6), and restate
    `catalog_profile_revision_id` semantics.
