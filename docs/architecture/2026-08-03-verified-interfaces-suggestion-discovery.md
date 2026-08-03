# Verified Interfaces — Suggested Feature Semantic Discovery (Task 0F Freeze)

Date: 2026-08-03
Code authority: branch `worktree-suggested-feature-semantic-discovery` at `30f8442b`
(= `origin/main f3424c36` of 2026-08-03 + one docs-only commit; plan-review baseline `fa9a20b0`
is an ancestor). Worktree clean at freeze time.
Status: **BINDING for the 2026-08-01 suggested-feature-semantic-discovery plan, Releases A–C.**
Companion to `2026-08-01-verified-interfaces-semantic-profiles.md` (the shared ledger), which
cross-links here in its §12. This document is a companion rather than an inline ledger section
because its frozen content is roughly twice the size of the entire existing ledger and would have
buried the cross-plan sections every other stream depends on.

Every symbol, count and signature below was re-verified against the code at the commit above, by
symbol — not from the plan's memory. Downstream Release-A tasks (0S, 0C, 1, 2A, 2, 3) treat this
document as authoritative. If code and this document disagree, stop and amend both.

---

## 0F-1. Baseline resolution <a id="baseline"></a>

- Implementation branch: `worktree-suggested-feature-semantic-discovery`, HEAD `30f8442b`
  ("docs: carry the suggested-feature discovery plan and verified-interfaces ledger onto the
  implementation branch"). Parent `f3424c36` = `origin/main` as of 2026-08-03.
- `git status --porcelain` empty: **no dirty state**.
- Plan-reviewed baseline `fa9a20b0` verified an ancestor (`git merge-base --is-ancestor`).
- **P4 v1 stop-condition satisfied**: `docs/superpowers/plans/2026-07-27-p4-suggested-features-v1.md`
  and its implementation (`src/featuregen/overlay/upload/suggestions.py`,
  `src/featuregen/api/routes/suggestions.py`, `frontend/src/screens/SuggestedFeaturesScreen.tsx`,
  52 focused tests) are all present. Recorded; not stopping.

### Migration head and reservations

- Head by lexical filename: `1044_run_event_ordering.sql`. 127 migration files total in
  `src/featuregen/db/migrations/`.
- Duplicate numeric prefixes exist at **0973, 0974, 1034, 1036, 1037, 1038, 1040** (verified by
  listing) — baseline/live state must be recorded as applied filename+checksum set, exactly as the
  shared ledger §5 states.
- Shared ledger §5 reservations verified against the repo: **1044 is now TAKEN on main**
  (`1044_run_event_ordering.sql` exists — the codegen-remediation reservation was consumed as
  reserved). **1045–1050 remain reserved** by the semantic (1045–1046), profile (1047–1049) and
  tournament (1050) plans. This plan owns none of them.
- **Release A of this plan allocates no migration.** Its registries are in-repo authored modules and
  its reads are on-demand. Any later Release-B persistence task must allocate **≥ 1051** by amending
  the shared ledger §5 table first — never by picking "the next number" locally, and never creating
  another duplicate prefix.

---

## 0F-2. Recomputed registry counts <a id="counts"></a>

All recomputed at `30f8442b` via `uv run python` against the live registries (commands in the Task
0F report). Counts describe this baseline, not a product invariant.

| Quantity | Measured | Plan cited | Match |
| --- | --- | --- | --- |
| Templates (`templates.ALL_TEMPLATES`) | **157** (157 distinct `id`s) | 157 | yes |
| Distinct `Template.family` values | **121** | 121 | yes |
| Distinct legacy `Template.use_cases` strings | **107** | 107 | yes |
| Templates with `primary_objective` | **14** (the same 14 also carry `supporting_objectives`; 8 distinct primary IDs) | 14 | yes |
| Templates with no canonical objective ("unmapped") | **143** | — | recorded |
| Distinct authored `aggregation` labels | **156** | "~152" (docstring) | updated |
| Use-case taxonomy nodes (`USE_CASE_REGISTRY`) | **123** | — | recorded |
| Selectable use-case leaves (`selectable_leaves()`) | **88** | — | recorded |
| Non-selectable nodes | **1** (`financial_crime`, domain parent) | — | recorded |
| Intentionally-empty leaves | **13** | — | recorded |
| `LEGACY_TAG_CROSSWALK` entries | **107** — exactly the 107 template tags, zero missing, zero orphans | — | recorded |
| Crosswalk by status | mapped **98** / merged **2** / deprecated **7** | — | recorded |
| Crosswalk by dimension | use_case **92**, modelling_context 8, measure 2, typology 1, product_context 1, journey_stage 1, business_outcome 1, metadata 1 | — | recorded |
| Concepts (`CONCEPT_REGISTRY`) | **324** | — | recorded |
| Distinct `Concept.entity_link` values | **40** | — | recorded |
| Independently grantable visibility classes | **3** (`confidential`, `pii`, `restricted`) → lattice bound **8** | 3 / bounded | yes |

The 107-tag crosswalk (`overlay/upload/taxonomy/legacy_crosswalk.py`) is the reviewed migration
evidence the plan requires: it already routes every legacy tag to a governed home and validates at
import against `USE_CASE_REGISTRY` and `dimensions.is_known`. Task 1 consumes it; it does not
re-do the archaeology, and it does not silently promote a `merged`/`deprecated` tag.

---

## 0F-3. Verified symbol inventory <a id="symbols"></a>

Confirmed by reading the symbol, not by line-number or tuple-length assumption.

### Engine and gauntlet

- `gate1._template_candidates` — `src/featuregen/overlay/upload/contract/gate1.py:208`.
  Signature (keyword-only after `conn`):

  ```python
  def _template_candidates(conn, *, catalog_source: str, roles, target_ref: str | None, now,
                           templates: Sequence[Template] = ALL_TEMPLATES,
                           fresh_within: timedelta = timedelta(hours=24),
                           table: str | None = None,
                           also_tables: Sequence[str] = (),
                           ) -> tuple[list[FeatureIdea], list[dict],
                                      frozenset[str], dict[str, tuple[str, ...]], dict[str, str],
                                      dict[str, tuple[str, ...]],
                                      dict[str, RecipeGroundingContextV1],
                                      dict[str, tuple[str, ...]]]
  ```

  Return order (8-tuple): `(ideas, rejections, grounded_ids, rejected_ids→codes,
  binding_by_id→BindingQuality.value, incomplete_ids→reason_codes,
  contexts by recipe_candidate_key, keys_by_recipe)`.
- `suggestions.suggest_features_for_table(conn, *, catalog_source, table, roles=(),
  max_hops=None) -> dict` — `overlay/upload/suggestions.py:58`. Read-only; widens grounding by
  `join_path.clearing_neighbourhood`; filters to ideas that bind the anchor (`_binds`); groups by
  `_entity_of` (grounding context's source-entity binding → `concept(...).entity_link`).
- `feature_assist.FeatureIdea` — `overlay/upload/feature_assist.py:493`, frozen slots dataclass.
  Fields (order verified): `name, description, derives_from, aggregation, grain_table,
  derives_pairs=(), verification="DESIGN-CHECKED", critic_note="", rationale="",
  operation_kind="", measure_refs=(), grain_ref=None, time_ref=None, window=None,
  grouping_refs=(), validation_status="DESIGN_CHECKED", requirements=(), plan_envelope=None,
  origin="llm", path_authority="single_or_llm", generation_source="llm_freeform", recipe_id=None,
  candidate_status="", input_role_bindings=(), external_requirement_previews=(),
  metadata_snapshot_id=None, metadata_input_fingerprint=None, binding_fact_keys=(),
  planner_applicability="not_applicable_nonrecipe", physical_plan_id=None,
  planner_declaration_id=None, operand_roles=()`.
- `feature_assist._validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within, *,
  roles=(), operand_roles=()) -> (FeatureIdea|None, Rejection|None)` — line 670. The tri-state
  gauntlet. Its decision points are enumerated in [0F-7](#trace).
- `templates.GroundedFeature.grain_table` derivation (`templates.py:565-570`): the **source-entity
  binding's table** when one bound, else the **first bound column's table**, else `None`. Every
  grain table is therefore the table of a bound operand — load-bearing for the V1 reconstruction
  proof in [0F-11](#v1proof).
- `recipe_grounding_context.RecipeGroundingContextV1` — fields `recipe_candidate_key, recipe_id,
  source_entity_need_role, source_entity_role_resolution, need_bindings, semantic_parameters,
  semantic_parameter_binding_hash, template_definition, template_content_hash,
  canonicalization_version="recipe-grounding-v1"`. `template_content_hash` = sha256 over
  `{"version": "canonical-recipe-v1", "template": <every Template field, needs canonicalized>}`;
  `recipe_candidate_key` = sha256 over the `recipe-candidate-v1` payload (recipe_id,
  template_content_hash, semantic_parameter_binding_hash, aggregation, ordered
  `[role, logical_ref]` bindings, binding_resolution_hash). Exhaustiveness is import-checked
  (`assert_canonical_recipe_exhaustive`).

### Join machinery

- `join_path.JoinOutcome` (line 48): `kind ∈ {OPERATIONAL, UNVERIFIED, NO_PATH, DENIED}` with
  `steps: tuple[JoinStep, ...]`, `endpoints`, `fact_keys`. `JoinStep` carries `from_ref, to_ref,
  cardinality, approved_join_fact_key, approved_join_status, authority` — the ordered selected
  path already exists at the decision point.
- `join_path.clearing_neighbourhood(conn, catalog_source, from_table, *, roles=(), max_hops=None,
  max_tables=None, max_columns=None) -> JoinNeighbourhood` (line 283). Bounds (module constants):
  `MAX_HOPS_DEFAULT=1`, `MAX_NEIGHBOUR_TABLES=20`, `MAX_COLUMNS_CONSIDERED=300`,
  `MAX_HOPS_CEILING=3`; `limit_reason ∈ {"table_cap", "column_budget", None}`.
- `JoinNeighbourhood.as_metadata()` — the five-field wire block `{tables_considered,
  tables_available, truncated, max_hops, limit_reason}`. **This is the shape the plan calls
  `JoinNeighbourhoodV1`**; see deviation D2.

### Requirements, rejections, quality

- `Requirement` (`feature_assist.py:120`): `code, operand: (catalog_source, object_ref),
  detail="", schema_version="v1", params: tuple[tuple[str, object], ...] = ()`. Closed
  `REQUIREMENT_CODES` = {TYPE_IS_NUMERIC, GRAIN_IS_UNIQUE, TEMPORAL_IS_POPULATED,
  TEMPORAL_LAG_BOUNDED, JOIN_CONNECTIVITY, UNIT_CONSISTENT, CURRENCY_CONSISTENT,
  ADDITIVITY_SUPPORTS_OPERATION}. Wire shape = `contract/_serial.requirements_to_json`:
  `{code, operand:[catalog, ref], detail}` + additive `params` / non-default `schema_version`.
  **This is the shape the plan calls `RequirementV1`**; see deviation D1.
- `VALIDATION_STATES = ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")`.
- `RejectCode` (`feature_assist.py:76`) — closed vocabulary. Reachable on the suggestions path
  (`target_ref=None, now=None`): UNGROUNDED, AMBIGUOUS_CATALOG, UNKNOWN_COLUMN, NON_NUMERIC,
  ADDITIVITY, MIXED_UNITS, MIXED_CURRENCY, NO_POINT_IN_TIME, NO_JOIN_PATH, JOIN_DENIED.
  LEAKAGE and STALE require an intent/clock and cannot fire here.
- `taxonomy/ranking_signals.BindingQuality` = {exact, strong, acceptable, ambiguous};
  `binding_quality(gf)` derives worst-wins from binding resolutions + grounding notes.

### Read scope and identity

- `overlay/upload/read_scope.py`: `SENSITIVITY_ROLES = {"pii": "pii_reader",
  "restricted": "restricted_reader"}`; `RESTRICTION_ROLES = {"confidential":
  "confidential_reader", "restricted": "restricted_reader"}`;
  `allowed_classes(roles) -> sorted deduped list`; `VISIBILITY_PREDICATE =
  "visible_requires <@ %s"`; `allowed_sensitivities` is a back-compat alias. `prohibited` is
  deliberately ungrantable.
- `identity/permissions.py`: functional roles (catalog_viewer, data_owner, feature_engineer,
  access_admin, audit_reader, platform_admin) are a **separate axis** from the three data-
  sensitivity reader roles. Routes check permissions, never role strings.
- Route: `GET /catalog/{catalog_source}/tables/{table}/suggestions`
  (`api/routes/suggestions.py:59`), gated `require_catalog_read`, threads
  `roles=identity.role_claims`, `max_hops` query param bounded `ge=1, le=MAX_HOPS_CEILING` (422
  above), `SET LOCAL statement_timeout = 30_000` ms (`SUGGESTIONS_STATEMENT_TIMEOUT_MS`).

### Evidence axes and hashing

- `overlay/evidence.py`: `EvidenceProducer` = {source, structural_connector, parser, llm,
  profiler, taxonomy, human, legacy}; `AssertionStrength` = {proposed, supported, attested,
  confirmed}; `EvidenceLifecycle` = {active, stale, rejected, superseded}. These are the only
  evidence-axis vocabularies; no five-value authority enum exists or may be created.
- `src/featuregen/canonical.py` **does not exist** on this baseline. `materialize_hash` exists at
  `src/featuregen/materialize/canonical.py:46`. The Task 0S gate in the shared ledger §3
  (extract `jcs_sha256`, add `contract_hash_v1`, delegate `materialize_hash` byte-identically) is
  therefore real, unstarted work and remains a prerequisite of Task 1.
- `RelationshipKind`, `SemanticContextBundleV1`, `DatasetSemanticProfileV1` **do not exist in
  code** — owned by the unlanded semantic/profile plans. See deviations D3/D9.

### Frontend V1 wire contract (`frontend/src/api.ts`)

`TableSuggestions` = `{catalog_source, table, table_known, summary: {suggested, clean_ready,
needs_review, entities}, groups: SuggestionGroup[], rejections: SuggestionRejection[],
neighbourhood: JoinNeighbourhood}`; `FeatureSuggestion` = `{name, description, recipe,
recipe_parts, validation_status, requirements, uses, binding_quality, grain_table}`;
`SuggestionGroup` = `{entity_ref, entity_label, suggestions}`; `SuggestionRejection` =
`{name, reason, code}`. `SuggestionCard` is defined in `SuggestedFeaturesScreen.tsx:325` and
reused by the column dossier (`AssetDetailScreen.tsx:728`), which filters **client-side**
(`matching` memo, line 697). React keys are `key={s.name}` — the rendered name is the identity,
exactly the verified gap 7. `DESIGN_CHECKED` renders as `'clean & ready'`
(`SuggestedFeaturesScreen.tsx:43`) — verified gap 3. No generation-source badge exists — gap 4.

---

## 0F-4. Shared primitives Task 0S must land <a id="task0s"></a>

Frozen so Tasks 1–3 import rather than copy:

1. `src/featuregen/canonical.py` with `jcs_sha256(payload)` and
   `contract_hash_v1(contract_name, contract_version, payload)`, exactly per shared ledger §3;
   `materialize/canonical.materialize_hash` delegates byte-identically to `jcs_sha256` (no new
   envelope for existing materialization payloads; existing `field_evidence.canonical_hash`
   values untouched).
2. `EvidenceAuthorityV1` at **`src/featuregen/contracts/evidence_axes.py`** (new module in the
   existing `featuregen.contracts` package), importing the three enums from
   `featuregen.overlay.evidence`:

   ```python
   @dataclass(frozen=True, slots=True)
   class EvidenceAuthorityV1:
       producer: EvidenceProducer
       strength: AssertionStrength
       lifecycle: EvidenceLifecycle
       producer_ref: str | None
       evidence_id: str | None
   ```

   Cross-plan rule: when semantic Task 1 lands `SemanticValueV1`, it **imports this class** —
   one definition, per the shared ledger's one-owner rule. (The ledger's §2 sketch and this
   definition are field-identical; whichever plan lands first owns the file.)
3. Hash canonicalization rule for evidence tuples inside any semantic hash: only
   `(producer, strength, lifecycle)` plus value content enter; `producer_ref`/`evidence_id` are
   occurrence provenance and are excluded. Replaying identical evidence under a new event ID
   changes no revision.

---

## 0F-5. Frozen contract: discovery metadata registry <a id="discovery-registry"></a>

Owner module (new, Task 1): `src/featuregen/overlay/upload/taxonomy/discovery_metadata.py` —
an in-repo authored/versioned registry beside its siblings (`use_cases.py`,
`legacy_crosswalk.py`), import-validated like them. **No database table in Release A.**

Dataclasses exactly as the plan's "Contracts to Freeze" section:
`DiscoveryControlledAssignmentV1 {controlled_id, basis, evidence: tuple[EvidenceAuthorityV1, ...],
operational_influence}`, `DiscoveryTextAssignmentV1 {value, basis, evidence,
operational_influence}`, `TemplateDiscoveryMetadataV1 {template_id, feature_category,
business_domains, canonical_use_cases, keywords, business_value, disposition}`.

Frozen vocabularies:

- `basis` (discovery assignments): `template_authored | human | llm_proposed`.
- `disposition`: `complete | partial | unclassified | needs_sme` — coverage summary only, never a
  substitute for per-value `basis`/evidence.
- `operational_influence`: `None` for every Release-A discovery value ("discovery hint only in
  v1"); the field exists so a later governed promotion has somewhere to land without a schema
  change.

Import validation must prove (each a test): every entry names exactly one template id present in
`ALL_TEMPLATES`; no orphan/duplicate entries; `feature_category.controlled_id` exists in the
feature-category registry; each `business_domains[].controlled_id` resolves in the shared domain
registry (unpopulatable at this baseline — see D9 — so the tuple is empty until a resolver
exists); each `canonical_use_cases[].controlled_id` is in `selectable_leaves()` of
`USE_CASE_REGISTRY` (88 at baseline); keyword/prose bounds enforced; a template with no entry is
explicit `unclassified` and searchable as such. The registry duplicates **nothing** from
`Template` (`intent`, `family`, `stage`, `eligibility`, `near_label`, needs, params stay in
`templates.py`).

Legacy migration rule (unchanged from plan): the 107 legacy tags are consumed only through
`LEGACY_TAG_CROSSWALK` as a reviewed manifest; a mapping becomes current only after the canonical
validator passes; the validator never relabels `llm_proposed` as `human`. An audited LLM pass may
propose mappings for all 157 templates; its outputs carry `basis=llm_proposed` and are usable for
discovery with visible provenance and no operational authority.

`discovery_metadata_revision_id` :=
`contract_hash_v1("template-discovery-metadata", "1", canonical-entry-payload)` where the payload
is the entry's controlled IDs, text values, per-value basis and canonicalized evidence axes
([0F-4](#task0s) rule 3), sorted. Registry-wide:
`discovery_registry_content_hash := contract_hash_v1("template-discovery-registry", "1",
sorted [template_id, entry-revision] pairs)` — a rebuild/fencing input, never suggestion identity.

---

## 0F-6. Frozen contract: attributed values and axis owners <a id="axes"></a>

`AttributedLabelV1` and `AttributedTextV1` exactly as the plan text (fields `id/display_name` vs
`value`, `basis`, `evidence`, `operational_influence`, `source_refs`). `basis` vocabulary for
attributed values: `template_authored | catalog_resolved | human | llm_proposed`.
`AttributedLabelV1` is **only** for controlled registry IDs; free-text catalog domain/entity
wording travels as `AttributedTextV1` and never becomes a facet key. One value may carry source,
LLM and human evidence simultaneously; collapsing to one "best authority" is forbidden.
Owner module for both dataclasses: `featuregen.contracts.evidence_axes` (with
`EvidenceAuthorityV1`, Task 0S) so backend and projection import one definition.

The five axes and their canonical owners (the plan's rule 1; no second ontology):

| Axis | Canonical owner at this baseline | Facet-eligible in Release A? |
| --- | --- | --- |
| `feature_category` | NEW registry, `overlay/upload/taxonomy/feature_categories.py` (Task 1; coarse computation classes: ratio, trend, recency, frequency, concentration, …) | yes (controlled) |
| `recipe_family` | existing `Template.family` (121 values) wrapped by NEW `overlay/upload/taxonomy/recipe_families.py` stable ID/display registry (Task 1) — family strings themselves stay in `templates.py` | yes (wrapped IDs) |
| `business_domain` | shared semantic/ontology registry owned by the 2026-08-01 semantic plan; **no controlled resolver exists in code today** (catalog `domain` is free enrichment text) | **no** — `AttributedTextV1` search text only (D9) |
| `use_case` | existing `overlay/upload/taxonomy/use_cases.py` `USE_CASE_REGISTRY`, selectable leaves only (88) | yes |
| `entity` | existing `Concept.entity_link` vocabulary (`overlay/upload/concepts.py`, 40 values) + `taxonomy/entity_registry.py` relationships; controlled-ID upgrade owned by the semantic plan | yes (entity_link IDs); unresolved wording stays `AttributedTextV1` |

Registry version pins live in `overlay/upload/taxonomy/versions.py`
(`APPLICABILITY_MAPPING_VERSION="2.0.0"`, `RECIPE_REGISTRY_VERSION="2.0.0"`,
`CONCEPT_REGISTRY_VERSION="concepts@2"`); new registries add their versions there.

---

## 0F-7. Frozen contract: `GroundingDecisionTraceV1` and its producer seam <a id="trace"></a>

Dataclasses (`SuggestionDependencyClass`, `GroundingDependencyPinV1`,
`SuggestionRelationshipDependencyV1`, `GroundingDecisionTraceV1`) exactly as the plan's
"Grounding and validation trace" section, with two baseline-forced precisions:

- `requirements: tuple[Requirement, ...]` uses the **existing** `feature_assist.Requirement`
  value object (D1); wire emission via `requirements_to_json`.
- `relationship_kind` is typed `str` drawn from the frozen vocabulary
  `{direct_equality, crosswalk, transformed, semantic_only}` until the shared `RelationshipKind`
  StrEnum lands with semantic Task 1 (D3); values are frozen now, the Python type upgrades then.

`candidate_key` := the existing `recipe_candidate_key`
(`recipe-candidate-v1`, `recipe_grounding_context.py:151`) — already deterministic per grounded
candidate and already returned by the engine.

`trace_content_hash` := `contract_hash_v1("grounding-decision-trace", "1", payload)` where the
payload is: `candidate_key`, `ordered_operand_roles`, the ordered logical relationship path
(kind, direction, endpoint refs, `realization_content_hash` per leg), `validation_status`,
canonicalized requirements, sorted `(dependency_class, dependency_kind, dependency_key,
content_hash)` pins, `validation_rule_content_hashes`, `read_scope_rule_content_hashes`.
**Excluded**: `current_revision_id` pins, evidence occurrence IDs, timestamps, build observations
— those persist as scope/build provenance for currentness comparison only.

### `ordered_relationship_path` is a leg SET (amended 2026-08-03, Task 2A review)

The plan's wording ("the ordered logical relationship path") reads as ONE chain. It is one chain
only when a candidate reaches ONE neighbour table. The gauntlet classifies **one path per
cross-table operand**, so the field holds the **deduplicated union of the legs those paths
selected, in first-traversal (operand-binding) order**. For a candidate grained on `G` with
measures in `A` and `B` it is `legs(G→A) + legs(G→B)`, which must never be rendered as the
traversal `G→A→G→B` — no such walk happened. Consumers:

- **Rendering "which relationships did this depend on"** → read the field directly; that is what it
  is.
- **Rendering a chain / explaining one operand** → read that operand's own `JOIN_PATH` pin
  (keyed by `column_dependency_key(catalog_source, object_ref)`), whose content is
  `grounding_trace.join_path_pin_content(from_table, to_table, outcome_kind, legs)` — the endpoints,
  the outcome and that operand's ordered leg hashes. The pin carries this as a HASH, so from the
  trace alone a chain is **verifiable by recompute**, not directly readable; a consumer that must
  READ chains consumes the engine's `TemplateCandidatesResult` in the same call, which V2 assembly
  does by construction (persistence rule: it never consumes a reloaded snapshot).
- **Identity (plan rule 23)** → the per-operand ASSIGNMENT is identity-bearing: each `JOIN_PATH`
  pin's `(key, content_hash)` enters `trace_content_hash`, so two candidates with the same leg set
  but different operand→chain assignments hash differently. An identity builder therefore derives
  `suggestion_id` material from `trace_content_hash` (or from the pins), **never from
  `ordered_relationship_path` alone** — being a set, it cannot distinguish them.

The cross-catalog counterpart (`plan_envelope.plan_relationship_dependencies`) IS a single ordered
chain: a compiled plan has one path.

### The actual decision seam (verified) and required producer changes

The trace is produced where the decisions are made. The current seam discards its own evidence:
`_validate_idea` reads governed values, join outcomes and hints, mints requirements/rejections,
and returns only `(FeatureIdea, Rejection)`. The named producer changes — **all additive, none
moving a decision**:

| # | Owner module | Change |
| --- | --- | --- |
| P1 | `overlay/upload/feature_assist.py` (`_validate_idea`) | Emit one `GroundingDecisionTraceV1` per candidate: record a `GroundingDependencyPinV1` at every read it already performs — `_ground_refs` grounding set, `_column_meta` additivity/unit/currency hints, `_governed_read` of `logical_representation`/`additivity`/`is_as_of`/`is_grain` (class VALIDATION; `content_hash` over the read value+status, `current_revision_id` = the C1 decision/projection pin), `_as_of_column_ref`/`_grain_column_ref` structural lookups (HARD_AVAILABILITY), `_ai_suggestion` unit/currency surfacing (SEMANTIC), and the `classify_join_path` outcome per cross-table operand. **Carrier: the return ARITY stays `(FeatureIdea | None, Rejection | None)`** — the trace travels on the returned objects via two new defaulted fields, `FeatureIdea.grounding_trace: GroundingDecisionTraceV1 | None = None` and `Rejection.trace: GroundingDecisionTraceV1 | None = None` (both frozen dataclasses; defaults-last is this codebase's established additive pattern). Two new optional keyword-only inputs `candidate_key: str | None = None` and `template_id: str | None = None` let the caller thread candidate identity into the trace; callers that pass nothing (the LLM path) are unchanged. See the call-site freeze below. |
| P2 | `overlay/upload/join_path.py` (`classify_join_path`) | Stop discarding the selected path: the returned `JoinOutcome.steps` (already ordered, already carrying `approved_join_fact_key`/`approved_join_status`/`authority`/`cardinality`) is converted **at this seam** into the ordered `SuggestionRelationshipDependencyV1` legs handed to P1. No re-walk, no adapter-side path selection. |
| P3 | `overlay/upload/contract/gate1.py` (`_template_candidates`) | **Carrier: the plain 8-tuple return is REPLACED by a frozen result dataclass** `TemplateCandidatesResult` in `contract/gate1.py`, with named fields carrying the exact same eight objects under their established names (`ideas, rejections, grounded_ids, rejected_ids, binding_by_id, incomplete_ids, contexts, keys_by_recipe`) plus `rejection_records: tuple[RejectionRecordV1, ...]` where `RejectionRecordV1 = {template_id, candidate_name, rejection: Rejection}` (the rejection carries its trace from P1). Survivor traces ride inside `ideas` on `FeatureIdea.grounding_trace` — `dataclasses.replace` at the existing server-stamp (gate1.py:297) preserves the field by construction. The V1 wire `rejections` element stays the byte-identical `{name, reason, code}` dict list; `SuggestionRejectionV2.template_id` is populated from `rejection_records`, never by name-matching. This is a **breaking arity change and is frozen as such**; every call site is enumerated below. |
| P4 | `overlay/upload/templates.py` (grounding) | No decision change. `GroundedFeature.role_bindings` / `binding_resolutions` / `tied_candidate_set_hash` already carry binding evidence; P1 copies `ordered_operand_roles` from them (`(catalog_source, logical_ref, role)` in binding order). |
| P5 | `overlay/upload/read_scope.py` | New pure function `read_scope_rule_content_hash()` = `contract_hash_v1("read-scope-rules", "1", {SENSITIVITY_ROLES, RESTRICTION_ROLES, VISIBILITY_PREDICATE, precedence: "migration-1032"})`, the single member of `read_scope_rule_content_hashes` today. |
| P6 | `overlay/upload/validation_requirements.py` | New pure function returning per-rule content hashes for the rule set `_validate_idea` actually evaluated (registry schema entries + rule constants), populating `validation_rule_content_hashes` — the exact rules evaluated, not a global registry version. |

### Call-site freeze (amended 2026-08-03 after review)

Both seams are unpacked **positionally** today, so the carrier choice above is frozen together
with its exact blast radius (all sites verified by grep at `30f8442b`):

- `_validate_idea` — arity unchanged, therefore **zero call-site changes required**. The six
  production two-name unpacks remain valid as-is: `feature_assist.py:995`,
  `feature_assist.py:1270`, `contract/gate1.py:286`, `contract/review.py:68`,
  `planner/b_gauntlet.py:123`, `planner/b_slice_spike.py:277`; likewise the ~45 test unpack
  sites across `tests/featuregen/overlay/upload/` (test_validate_idea_tristate,
  test_feature_loop, test_feature_v2_schemas, test_unit_confirm_loop, test_gating_confirm_lift,
  test_validation_requirements, test_ai_unit_proposal, test_suggestions,
  contract/test_operand_roles_carry) and `tests/featuregen/api/test_confirm_tamper_safety.py`.
  Only P3's caller (`gate1.py:286`) is *updated* — to pass `candidate_key`/`template_id` and read
  the trace — and that is already a P3-owned edit.
- `_template_candidates` — the result-dataclass change **breaks every positional unpack** and
  each must be migrated to attribute access in the same task (Task 1): production
  `overlay/upload/suggestions.py:95` (8-name unpack) and `contract/gate1.py:627`
  (`build_considered_set`'s own call), plus the **13 test call sites** in 7 files:
  `tests/featuregen/overlay/upload/test_suggestions.py`,
  `test_per_table_grounding_measurement.py`, `test_gating_confirm_lift.py`,
  `contract/test_operand_roles_carry.py`, `contract/test_gate1_scoped.py`,
  `contract/test_template_status.py`, `contract/test_h3c_governed_lineage.py`. No other
  production caller exists (verified).
- **Persistence rule**: `grounding_trace`/`trace` are transient carry. The existing considered-
  set (de)serializers are not extended; a persisted-and-reloaded `FeatureIdea` has
  `grounding_trace=None`, which is valid because V2 assembly always consumes fresh grounding
  output, never a reloaded snapshot (rule 15's "one grounding truth").

**Hard rule restated**: `overlay/upload/suggestions.py` and any V2 adapter consume the trace;
they never reconstruct it, never rerun path selection, and a `DESIGN_CHECKED` candidate without a
complete trace is invalid for V2.

---

## 0F-8. Frozen contract: `SuggestionReadScopeV1` <a id="read-scope"></a>

Dataclass exactly as the plan. Derivation is frozen against the real registry:

- The visibility-class universe is the union of the **values** of
  `read_scope.SENSITIVITY_ROLES` and `read_scope.RESTRICTION_ROLES`:
  `{confidential, pii, restricted}` — three independently grantable classes, each toggled by
  exactly one reader role (`confidential_reader`, `pii_reader`, `restricted_reader`;
  `restricted_reader` appears in both maps and dedupes). `prohibited` is unreachable.
- `allowed_classes` := the canonical sorted output of
  `read_scope.allowed_classes(identity.role_claims)`. Functional roles and unknown claims
  contribute nothing (verified: `allowed_classes` ignores any role not in the two maps), so **raw
  functional roles and user IDs cannot mint scope variants** — `{feature_engineer, pii_reader}`
  and `{data_owner, pii_reader}` produce the identical tuple `("pii",)`.
- The canonical scope-tuple lattice is the powerset: **8 tuples** —
  `()`, `("confidential",)`, `("pii",)`, `("restricted",)`, `("confidential","pii")`,
  `("confidential","restricted")`, `("pii","restricted")`,
  `("confidential","pii","restricted")`.
- **Import gate** (test, Task 1): recompute the class universe from the two role maps and assert
  the lattice bound is `2**len(universe)`; the number 8 is never hardcoded in business logic — a
  fourth grantable class must fail this gate loudly, not silently widen.
- `scope_key` := `contract_hash_v1("suggestion-read-scope", "1",
  {"schema_version": ..., "tenant": None, "allowed_classes": [...]})`. `tenant` is `None` until
  Task 0P declares otherwise (shared rule 22).
- **Release A stays on-demand**: `SuggestionReadScopeV1` is computed per request for response
  diagnostics, ETag/cursor **key material** (production of those is Task 0P) and V2's
  `read_scope_key`; it schedules nothing, persists nothing and creates no refresh work.
  `SuggestionDesiredTargetV1`/`SuggestionBuildTargetV1` are frozen as written for Release B and
  are not implemented in Release A.

---

## 0F-9. Frozen contract: `FeatureSuggestionV2` and companions <a id="v2"></a>

`FeatureSuggestionV2`, `SuggestionBuildProvenanceV1`, `SuggestionProjectionStateV1`,
`FeatureSuggestionHitV2`, `SuggestionSummaryV2`, `SuggestionRejectionV2`, `SuggestionGroupV2`,
`SuggestionCollectionContextV2`, `FeatureSuggestionPageV2` are frozen exactly as the plan's
dataclass listings, with these bindings to real symbols:

- `schema_version` = `"feature-suggestion-v2"`.
- `generation_source` ∈ `{recipe, llm_freeform, user_defined}` — the existing server-stamped
  vocabulary (`FeatureIdea.generation_source`); this surface emits only `recipe` today.
- `validation_status` ∈ `VALIDATION_STATES`; REJECTED candidates appear only as
  `SuggestionRejectionV2`, never as hits.
- `binding_quality` ∈ `BindingQuality` values.
- `requirements` use the existing `Requirement` object / `requirements_to_json` wire (D1).
- `SuggestionRejectionV2.code` ∈ the `RejectCode` closed set ([0F-3](#symbols) lists the ten
  reachable here).
- `neighbourhood` carries the existing `JoinNeighbourhood` value; its wire form is
  `as_metadata()` — the name `JoinNeighbourhoodV1` denotes exactly that five-field shape (D2).
  On the table route it is never `None` — even for an unknown table, where the synthesized zero
  block is carried (see the [0F-11](#v1proof) nullability rule); `None` occurs only on the
  global page.
- `RecipePartsV2` := frozen dataclass with exactly the V1 parts semantics —
  `{operation: str, measures: tuple[str, ...], grain: str, window: str, time: str}` — produced by
  the same rendering code as today (`suggestions._recipe_parts`); V2 adds **no** field here
  because operands/grain/time travel typed at the suggestion level (`operation_kind`, `window`,
  `time_ref`, `grain_refs`, `operands`).
- `SuggestionOperandV1` (frozen fields, per plan prose): `catalog_source, logical_ref,
  graph_object_ref, table_ref, recipe_role, classification
  (measure | grain | time | grouping | other), visibility_requires_current, evidence_refs` —
  **ordered by the engine's binding order and deduplicated exactly as `derives_pairs`**
  (load-bearing for V1 `uses` reconstruction), with `recipe_role` from the template-declared
  `operand_roles`/`role_bindings` (never inferred from concept/name/type).
- `SuggestionSourceDatasetV1` (frozen fields): `catalog_source, table_ref, data_role, authority_role,
  temporal_storage_model, primary_entity, dataset_profile_hash, profile_status` — every value
  attributed; all except catalog/table are explicitly-unavailable in Release A (D9).
- `SuggestionWarningV1` = `{code, operand_refs, detail}` with closed codes `{NEAR_LABEL,
  SENSITIVE_INPUT, MISSING_TEMPORAL_EVIDENCE, MISSING_UNIT, MISSING_CURRENCY,
  RELATIONSHIP_UNCONFIRMED, RELATIONSHIP_SAFETY_UNPROVEN, DIRECTIONAL_CARDINALITY_UNAVAILABLE,
  PROFILE_PROPOSED}`. Prose renders the code; it is never a decision field. Staleness is
  projection state, never a warning.
- `FacetBucketV1` = `{id, display_name, count}` — Release-B search facets; Release A returns an
  empty `facets` mapping.
- Owner module for all V2 dataclasses: **new `src/featuregen/overlay/upload/suggestion_contracts.py`**
  (backend contract; the API layer serializes it, the Release-B projection imports it — never
  copies), with the API response models in `api/routes/suggestions.py` typed from it.

Composite grain: `grain_refs` is an ordered tuple of `(catalog_source, object_ref)` key operands
(rule 25); single-grain today is the one-element case, empty only when unresolved.

---

## 0F-10. Frozen identity rules and the identity/provenance split <a id="identity"></a>

All new identities use `contract_hash_v1` (JCS). Frozen hash inputs:

- **`suggestion_id`** := `contract_hash_v1("feature-suggestion-id", "2", {template_id,
  canonical bound params (the `semantic_parameters` sorted pairs), sorted operand tuples
  `(catalog_source, logical_ref, recipe_role)`, entity identity
  `(entity_id | None, ordered grain_refs, time_ref | None)`, ordered logical relationship path
  `[(kind, direction, from_ref, to_ref), ...]`})`. The **requested anchor table is excluded**;
  the same cross-table candidate has one identity on table, column and global surfaces, and the
  canonical payload is anchor-independent (anchor, neighbourhood limits, truncation and cursors
  live only on `FeatureSuggestionPageV2` / `SuggestionCollectionContextV2`). Same columns over a
  different ordered relationship path ⇒ different `suggestion_id` (rule 23).
- **`recipe_revision_id`** := the existing
  `RecipeGroundingContextV1.template_content_hash` (`canonical-recipe-v1`). This is a reference
  to an existing verified content hash, not a new hash scheme — rule 11 permits referencing
  existing hashes and forbids rewriting them. Consequence recorded explicitly (D5): the canonical
  template payload covers **every** authored `Template` field, including legacy `use_cases`,
  `stage`, `notes`; editing those re-revisions the recipe. Accepted: the `Template` is one
  authored artifact; the separate discovery axis is the **new** registry keyed by
  `discovery_metadata_revision_id`, which never enters `recipe_revision_id`.
- **`suggestion_revision_id`** := `contract_hash_v1("feature-suggestion-revision", "2",
  {suggestion_id, recipe_revision_id, discovery_metadata_revision_id | None, sorted referenced
  semantic-context content hashes, sorted dataset-profile hashes, grounding
  `trace_content_hash`, sorted relationship/dependency content hashes, sorted evaluated
  validation-rule content hashes, sorted evaluated read-scope-rule content hashes,
  validation_status, producer_contract_version})`.
- **Excluded from all semantic hashes** (stored instead in `SuggestionBuildProvenanceV1`, the
  trace's `current_revision_id` pins and Release-B scope-dependency rows): raw catalog snapshot
  IDs, `metadata_snapshot_id`, evidence occurrence/event IDs, realization/observation revision
  IDs, refresh IDs, timestamps, producer commit, deployment/job identity, and registry-wide
  hashes (`discovery_registry_content_hash`, policy registry hashes — fencing inputs only).
  Byte-identical content re-uploaded/re-authored/rebuilt reuses the existing revision (rule 24).
- Changing an **unrelated** template, taxonomy label or policy rule does not re-key a suggestion:
  only *referenced* content revisions enter its revision hash.

---

## 0F-11. V1 reconstruction proof for `SuggestionCollectionContextV2` <a id="v1proof"></a>

Claim (plan requirement): the explicit V1 adapter can rebuild today's wire payload
byte-for-byte from `FeatureSuggestionPageV2` alone. Proof by exhaustive field map against the
**verified** V1 producer (`suggestions.py`) and consumer (`api.ts TableSuggestions`):

| V1 field | Source in V2 | Note |
| --- | --- | --- |
| `catalog_source` | `collection.anchor_catalog_source` | table route always sets it |
| `table` | `collection.anchor_table_ref` | **frozen two-case rule**: when `table_known=True`, the resolved bare name (`suggestions.py:87` rebinds `table = known`); when `table_known=False`, the **caller's requested string verbatim** (`suggestions.py:80` echoes the raw input — no resolver output exists to carry). `anchor_table_ref` holds whichever V1 emitted, so the adapter copies it in both states |
| `table_known` | `collection.table_known` | tri-state `bool | None`; table route sets True/False; global page None |
| `summary.suggested` | `collection.summary.suggested` | |
| `summary.clean_ready` | `collection.summary.design_checked` | rename only; adapter re-emits the V1 key |
| `summary.needs_review` | `collection.summary.needs_external_validation` | V1 computes `suggested - clean`; identical by construction |
| `summary.entities` | `collection.summary.groups` | frozen semantic: count of groups with a **non-empty group ref** (exactly V1's `sum(1 for ref in groups if ref)` — a label-less group keyed on a grain column still counts; only the `""`-ref bucket does not) |
| `groups[].entity_ref` | `SuggestionGroupV2.grain_refs[0][1]`, else `""` | V1's ref is the bound entity column's `graph_object_ref` (or the grain column, or `""`); frozen: the group's first grain_ref carries exactly that column |
| `groups[].entity_label` | `SuggestionGroupV2.entity.display_name`, else `""` | V1 label = `Concept.entity_link` id; `entity=None` ⇔ V1's unlabelled bucket |
| group ordering | re-derivable | V1 sorts by `(ref == "", ref)`; adapter applies the same key to group refs — pure function of carried data |
| card `name` | `FeatureSuggestionV2.name` | |
| card `description` | `business_interpretation.value` | V1 description = `template.intent`; frozen: V2 carries it as `AttributedTextV1(basis=template_authored, evidence cites recipe_revision_id)`; adapter reads `.value` |
| card `grain_table` | table segment of the source-entity operand's `table_ref`; fallback first operand's `table_ref`; else absent | Sound because `GroundedFeature.grain_table` is **always** the table of a bound operand (verified, `templates.py:565-570`), operands preserve binding order, and `recipe_role` marks the entity operand. Adapter test must pin equality |
| card `validation_status` | `validation_status` | |
| card `requirements` | `requirements` via `requirements_to_json` | same value objects, same serializer ⇒ same bytes (incl. additive `params`/`schema_version` emission rules) |
| card `uses` | `[o.graph_object_ref for o in operands]` deduped in order | frozen operand ordering = engine binding order = `derives_pairs` order ([0F-9](#v2)) |
| card `binding_quality` | `binding_quality` | |
| card `recipe` | `recipe` | carried verbatim (rendered once by the engine-side renderer, not re-rendered) |
| card `recipe_parts` | `recipe_parts` (`RecipePartsV2`) | field-identical shape ([0F-9](#v2)) |
| `rejections[]` `{name, reason, code}` | `SuggestionRejectionV2 {candidate_name, explanation, code}` | `template_id` is additive; adapter drops it |
| `neighbourhood` (5 fields) | `collection.neighbourhood.as_metadata()` | identical producer. **Frozen nullability rule**: on the table route `collection.neighbourhood` is **never `None`** — including the unknown-table state, where V1 synthesizes the zero block (`suggestions.py:83-86`: zeros, `truncated=False`, the effective `max_hops`, `limit_reason=None`) and V2 carries that same `JoinNeighbourhood` value. The plan's `JoinNeighbourhoodV1 | None` typing reserves `None` for the global page only |
| unknown-table payload | `table_known=False` + `anchor_table_ref` (raw requested string, row above) + zeroed summary + empty groups/rejections + the carried zero-neighbourhood block | every V1 byte is a carried value: the zero-shape needs no recomputation because the synthesized neighbourhood block (including `max_hops`) travels in `collection.neighbourhood`, not as a derived function |

New V2 fields the adapter must **drop** (never invent): `omitted_counts`, `hits`, `facets`,
`read_mode`, `read_scope_key`, `projection`, per-suggestion discovery/identity/provenance
fields. Nothing in the V1 payload lacks a V2 carrier; therefore V1 is reconstructable without
re-querying, and the byte-equality adapter test (Task 2A) is well-posed. ∎

---

## 0F-12. Frozen Release-A API negotiation (OpenAPI-facing) <a id="api"></a>

Route unchanged: `GET /catalog/{catalog_source}/tables/{table}/suggestions`, gate
`require_catalog_read`, read scope from `identity.role_claims` only, `max_hops` bounds and the
30s statement timeout retained.

- **Version negotiation**: query parameter `contract_version: int = Query(default=1)` —
  **deliberately unbounded at the FastAPI layer** (amended after review: a `ge`/`le` bound would
  make FastAPI reject an unsupported version before the handler runs, so the typed error code
  below could never be emitted and the error shape would be FastAPI's list-`detail` validation
  body instead). The **handler** validates membership in the supported set `{1, 2}` and returns
  the typed 422 body for any other integer — exactly one mechanism, in the handler. No
  `contract_version` exists anywhere in `src/featuregen/api` today (verified) — this is a new,
  additive parameter. `contract_version=1` (and absence) returns today's dict **byte-identical**
  (the V1 adapter path of [0F-11](#v1proof)). `contract_version=2` returns the
  `FeatureSuggestionPageV2` serialization with `read_mode="on_demand"`, `projection=None`,
  empty `facets`, `next_cursor=None`.
- **Response models**: the v2 response is a declared FastAPI/OpenAPI response model generated
  from the frozen dataclasses (owner: `api/routes/suggestions.py` + a pydantic mirror or
  dataclass-derived model in the same module; the overlay dataclasses in
  `suggestion_contracts.py` remain the single source of field truth). The v1 shape is documented
  in OpenAPI as the default via the existing dict contract.
- **Typed error bodies**: errors keep FastAPI's `{"detail": ...}` envelope (the platform-wide
  convention, verified in `api/routes/*.py`) and add a machine field:
  `{"detail": str, "error_code": str}` — emitted **only by handler-level checks**, where the
  shape is actually controllable. Closed codes: `SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION`
  (422, an integer `contract_version` outside `{1, 2}`, returned by the handler check above via
  an explicit `JSONResponse`) and `SUGGESTIONS_TABLE_TIMEOUT` (500, statement-timeout surfaced
  honestly). **Explicit boundary** (amended after review): a non-integer `contract_version` —
  and any other parameter type/bounds failure, e.g. `max_hops` — keeps FastAPI's native
  validation error, status 422 with `detail` as a **list**, which is the platform's existing
  behavior and sits *outside* the typed `{detail: str, error_code: str}` contract. Task 3 types
  only the two handler-emitted codes plus the untouched existing 401/403 auth envelopes.
  `table_known=false` remains a **200** payload state, never an error (frozen honesty rule).
- **Caching/cursor**: no ETag, no cursor and no cache-control changes in Release A —
  production of cursor/ETag binding to `scope_key` is Task 0P; Release A responses remain
  uncached/private by default.
- The API may expose `read_scope_key` (the opaque JCS hash) for diagnostics; it never echoes
  role claims and is never itself authorization.

---

## 0F-13. Measured baselines (2026-08-03, this machine) <a id="baselines"></a>

Focused suites (commands verbatim):

- `uv run pytest tests/featuregen/overlay/upload/test_suggestions.py
  tests/featuregen/api/routes/test_suggestions_route.py` → **52 passed** (43 + 9) in ~50s.
- `cd frontend && npm test` (vitest) → **543 passed / 29 files**, ~19s.

SQL statement counts per page view, measured by wrapping `suggest_features_for_table` with the
same per-`Connection.execute` counter the suite's `_statements` helper uses (read-only
measurement plugin; no repo file changed), across the existing fixtures:

| Fixture (tests/featuregen/overlay/upload/test_suggestions.py) | Table | Statements | Notes |
| --- | --- | --- | --- |
| narrow — `ftr_catalog` (3 tables/19 cols) | `mkt_risk_pos` (3 cols) | **335** | 1 suggestion |
| mid — `ftr_catalog` | `loan_repay` (7 cols) | **321** | 8 suggestions |
| wide — `ftr_catalog` | `comp_fin_tran` (9 cols) | **409** | 10 suggestions; identical with/without reader roles and via the route |
| joined — `join_catalog` (2 tables over a VERIFIED join) | `txn_ledger` | **813** | plan cited 813 — unchanged; unwidened/unverified variants 396–414 |
| hub — `hub_catalog` (40 direct spokes + 1 two-hop) | `cust_hub`, capped default | **6,284** | plan cited 6,284 — unchanged |
| hub, bounds lifted (`max_hops=99`, caps monkeypatched away) | `cust_hub` | **12,710** | plan cited 12,710 — unchanged |
| unknown table | any | **1** | the `graph_node` resolve only |

Test-asserted ceilings stand at 7,000 (hub) and 850 (ordinary). The plan's economic claim —
hundreds-to-thousands of statements per on-demand table read; a global surface cannot repeat it
per table — is **re-confirmed current**, which is why rule 8 (no search-time grounding) is frozen.

---

## 0F-14. Deviations, discrepancies and explicit decisions <a id="deviations"></a>

Recorded, never silently redesigned:

- **D1 — `RequirementV1` does not exist as a class.** The code type is
  `feature_assist.Requirement` + the `requirements_to_json` wire shape. Frozen: V2 reuses them;
  "RequirementV1" names the wire shape. No new requirement class may be created.
- **D2 — `JoinNeighbourhoodV1` does not exist as a class.** The code type is
  `join_path.JoinNeighbourhood`; its `as_metadata()` five-field dict is the frozen wire shape the
  plan's name refers to.
- **D3 — `RelationshipKind` is not in code.** Owned by unlanded semantic Task 1. Frozen value
  vocabulary `{direct_equality, crosswalk, transformed, semantic_only}`; typed `str` until the
  shared StrEnum lands. Not a Release-A blocker: the on-demand path's relationship evidence comes
  from `JoinStep` (`authority`, `approved_join_status`, `cardinality`), which maps into
  `SuggestionRelationshipDependencyV1` without the enum.
- **D4 — `featuregen.canonical` is not in code** (only `materialize/canonical.py`,
  `formula/canonical.py`, `overlay/upload/canonical.py` exist, none neutral). Task 0S gate is
  real and remains the prerequisite of Task 1.
- **D5 — `recipe_revision_id` includes authored legacy fields.** See [0F-10](#identity):
  reusing `template_content_hash` means legacy `use_cases`/`stage`/`notes` edits re-revision the
  recipe. Accepted deliberately; the alternative (minting a second, narrowed hash for the same
  artifact) would create dual identities for one authored file.
- **D6 — plan's "~152" operation kinds is stale**: 156 distinct authored aggregation labels
  measured. Cosmetic; the docstring can be corrected whenever that file is next edited.
- **D7 — hub/joined statement counts unchanged** (6,284 / 12,710 / 813) — the plan's numbers were
  re-measured, not trusted.
- **D8 — route `role_claims` mixes functional and reader roles.** Harmless by construction:
  `allowed_classes` ignores everything but the three reader roles, so scope tuples cannot vary by
  functional role. Frozen as the reason no separate role-filtering step is needed.
- **D9 — no controlled business-domain resolver and no `SemanticContextBundleV1` /
  `DatasetSemanticProfileV1` in code.** Catalog `domain` values are free enrichment text.
  Release A therefore ships `business_domains=()`, domain wording as `AttributedTextV1`
  (search-text, no facet), and `SuggestionSourceDatasetV1` context fields explicitly
  unavailable — never guessed (plan's "Contextual fields remain explicitly unavailable").
- **D10 — verified UI gaps confirmed as described**: `'clean & ready'` label
  (`SuggestedFeaturesScreen.tsx:43`), `key={s.name}` identity, client-side column filtering
  (`AssetDetailScreen.tsx:697`), no generation-source badge.
- **D11 — `EvidenceAuthorityV1` ownership handoff.** The shared ledger sketches it under
  semantic Task 1; this plan's Task 0S will land it first at
  `featuregen/contracts/evidence_axes.py`. Cross-plan rule frozen in [0F-4](#task0s): one
  definition, later plans import it. The shared ledger §12 cross-link records this.

No discrepancy found that makes a Release-A task impossible as specified.

---

## 0F-15. Explicitly not decided here <a id="deferred"></a>

Task 0P owns: tenancy declaration, cursor/ETag production, retention/withdrawal/erasure,
numeric freshness/latency/capacity/recovery targets, durable-projection activation. Release B
owns the dedicated fenced worker, scope-set CAS publication and the projection schema
(migrations ≥ 1051 via shared ledger §5 amendment). Nothing in this freeze authorizes a deploy,
catalog upload, backfill or live LLM call.
