# Catalog Profiles, Crosswalk Relationships, and Temporal Source Policy — Implementation Plan

Date: 2026-07-30
Status: **FINALIZED** after adversarial code-vs-plan review on 2026-07-30. Verified against three
live lines (named in “Verified Baseline”). Execution begins with Task 0 selecting the merged
baseline and reserving migrations from `1040`.

> **For agentic workers:** execute this plan task by task. Do not implement the relationship
> assessment, realization, or cross-catalog materialization contracts independently from
> `2026-07-29-bridge-cardinality-and-link-trust-remediation.md`; that plan owns those shared
> contracts **and its Tasks 0B and 1–8 are already implemented** (see “Verified Baseline”). This
> plan extends the frozen contracts with catalog/dataset profiles, authority, crosswalks, temporal
> selection, upload UX, LLM suggestion/critic/probe loops, and end-to-end lineage.

## Finalization Notes (what changed in this revision)

1. Baseline rewritten from live verification: bridge-remediation Tasks 0B + 1–8 are BUILT
   (migrations `1036`–`1039`); several “Missing” items in the draft are already fixed and are
   marked so below.
2. `RelationshipDefinitionAvailabilityV1` **deleted** — it duplicated (and weakened) the frozen
   availability family in `bridge_assessment.py` (`LinkAvailability`, `LinkUnavailableReason`,
   `FoldedLinkStatus`, `OverlayIdentifierLinkStateV1`). This plan extends that family, never
   redefines it.
3. `RelationshipDefinitionRevisionV1` now carries typed endpoints and derives identity through the
   frozen `candidate_identity_payload`; the undefined “namespace” fields are gone.
4. Catalog→dataset inheritance semantics decided: effective reads resolve against the **current**
   catalog profile pointer; the revision’s stored `catalog_profile_revision_id` is authoring-context
   provenance only; lineage records the resolved pair.
5. `entity_or_purpose` split into `entity_id` + `serving_purpose` (closed enum).
6. Historical requests against `current_only` storage resolve to `EXPLICIT_ONLY` (the draft’s
   “observed vintage” rule had no defined evidence source).
7. Four LLM/feedback loops added, all reusing shipped machinery: a refute-oriented critic pass on
   profile suggestions (Task 4); deterministic temporal-hypothesis probes (Task 4B); refusals routed
   into the data agent’s clarification flow and `learning.open_gaps` (Task 6); sandbox probe results
   persisted to the shared observation store (Task 9).
8. Feature-generation replay integrity closed: profile, serving-policy, and source-selection
   decisions become sealed `catalog_metadata_snapshot_item` kinds (Tasks 9/10).
9. UI scope extended: search facets for `data_role` / `authority_role` / `temporal_storage_model`
   (plus the existing `graph_node.table_role`, which is not surfaced anywhere today), and
   profile-suggestion dispositions in `IngestResultCallout` (Task 5).
10. LLM-proposed crosswalk candidates enter discovery as `llm_only` without waiting for a human
    table classification (Task 7), consistent with the bridge plan’s link policy.
11. **Consumer wiring made mandatory** (2026-07-30 follow-up review): profile text feeds the
    feature-idea generator’s existing `table_context` prompt seam (Task 4C), the search full-text
    document and the data-agent question→table retrieval (Task 5), and `data_role` gains advisory
    planner consumers (Task 9). Task 12 mutations pin each wiring, so collected-but-unused
    metadata is a test failure, not a silent rot.

## Goal

Turn uploaded catalogs from collections of columns and advisory hints into governed, explainable
data products that feature generation and the data agent can use safely.

The completed system must:

1. Describe each catalog and table in business language.
2. Classify tables as facts, dimensions, references, crosswalks, and other closed data roles.
3. Separately identify whether a dataset is a system of record, mastered view, replica, derived
   dataset, or external reference.
4. Let the uploader provide or override descriptions and classifications without allowing free text
   or an LLM guess to become join, cardinality, temporal, or physical-source authority.
5. Let an LLM suggest descriptions, roles, keys, dependencies, temporal behavior, and relationships
   as evidence-bound proposals — and pass every suggestion through a refute-oriented critic.
6. Distinguish a direct identifier equality from a mapping-table crosswalk, a deterministic
   transformation, and a semantic-only relationship.
7. Keep unreviewed relationships available to discovery, feature planning, data-agent planning, and
   marked sandbox use.
8. Require physical safety evidence—not human approval—for production relationship execution.
9. Default current master-data requests to an explicitly authoritative current copy, while every
   historical request uses point-in-time semantics and never silently joins today's dimension.
10. Carry the selected catalog profile, dataset profile, source, temporal rule, relationship
    realization, and assessment into generated-artifact and analysis lineage — and into the sealed
    feature-generation snapshot so replay reproduces the same selections.
11. Convert every typed refusal (ambiguous source, unknown temporal model, missing allocation
    policy) into a clarification question when a user is present and an open gap demand when not —
    a refusal is a work item, never a dead end.
12. Accumulate sandbox execution evidence so production eligibility emerges from use, not from
    re-measurement.

This is a functional correctness plan. General multi-tenant, transport-security, quota, scheduling,
and disaster-recovery work remains under the master roadmap's deferred programme.

## Verified Baseline

Verified live on 2026-07-30 against three lines. Task 0 re-verifies after the merge baseline is
cut; file/line references below are evidence anchors, not permission to implement from stale lines.

### The three lines

| Line | Head | Contains |
| --- | --- | --- |
| `integration/ontology-data-agent` | `ec570a34`+ | governance-review redesign (transplanted, e.g. `cdce4739`), data-agent analysis stack (bounded retrieval `edda2934`, clarification `ad02395c`, plan preview `534290f7`, grounded narrative `d8aa006a`, analysis API `4cf48389`), materializer, local Spark/Hive sandbox `e689de51` |
| `feature/bridge-cardinality-link-trust-remediation` | `7b1a9cfa` | bridge plan Tasks 0B + 1–8 (see below) |
| audited `eb19aa06` | (governance line) | **ancestor of neither** — its changes were transplanted into integration under different SHAs. Do not implement from its line numbers. |

The implementation baseline is **the bridge branch merged into integration**; neither line alone
contains everything this plan touches. `2026-07-29-governance-review-redesign.md` exists only in
the integration worktree’s docs — the merge must carry it.

### Already implemented (bridge-remediation plan) — do not rebuild

- Task 0B fail-closed patch: `_hop_evidence` has no hardcoded bridge `MANY_TO_ONE`
  (`planner/declarations.py` — only realization-resolved or `CARDINALITY_SOURCE_UNAVAILABLE`
  branches remain); `cross_catalog_links.AVAILABLE_STATUSES = {DRAFT, PARTIALLY_CONFIRMED,
  VERIFIED}` allow-list with `REVERIFY` handled and NULL-`fact_key` rows classified `UNGOVERNED`.
- Task 1 contracts frozen in `overlay/upload/bridge_assessment.py` (`TypeBasis`,
  `ConceptAuthority`, `KeyMemberRole`, `TupleKeyRole`, `NamespaceVerdict`, `PopulationRelation`,
  `EvidenceKind`, `EvidenceRefV1`, `strongest_evidence_label`, `IdentifierColumnMemberV1`,
  `IdentifierEndpointV1`, `candidate_identity_payload`, `candidate_id_for`,
  `IdentifierLinkAssessmentV1`, `LinkAvailability`, `LinkReviewStatus`, `LinkUnavailableReason`,
  `FoldedLinkStatus`, `OverlayIdentifierLinkStateV1`) and `overlay/upload/bridge_realization.py`
  (`DirectionalCardinalityVerdictV1`, `CardinalityBasis`, `SafetyStatus`, `RealizationLifecycle`,
  `ExecutionTier`, `ColumnPairV1`, typed predicates, `RealizationApplicabilityScopeV1`,
  `BridgeJoinRealizationRevisionV1`, `BridgeRealizationCurrentV1`, `eligible_for_sandbox`,
  `eligible_for_production`).
- Tasks 2–8 modules: `bridge_candidates.py` (bounded enumeration), `bridge_cardinality.py`
  (complete-key truth table), `bridge_store.py` (revision + CAS current pointers),
  `bridge_admission.py`, `attest/bridge_grounding.py`, `attest/bridge_critic.py`,
  `data_agent/relationship_observation.py` (tuple observations).
- Migrations: `1036_physical_dataset_binding_revision`, `1037_bridge_candidate_realization_store`,
  `1038_relationship_observation_v2`, `1039_structured_result_store`. **Migration head is `1039`;
  this plan allocates from `1040`.**
- `bridge_propose.py:141-144` already refreshes `candidate_id`, `data_type_family`,
  `evidence_json` on duplicate proposal — the draft’s “re-proposing does not refresh evidence”
  item is FIXED and removed from Task 8.

### Verified present (reuse targets)

- Closed table-role vocabulary + normalizer: `table_vocab.py` (`TABLE_ROLE_ENUM`,
  `CANONICAL_TABLE_ROLES` = event_fact/snapshot_fact/dimension/reference/bridge/fact,
  `normalize_table_role` with the fact→event/snapshot split). Reuse; do not write a second
  normalizer.
- Per-field LLM salvage + disposition recording: `table_synth.py` (`DISPOSITION_FIELDS`,
  accepted/dropped_invalid/abstained per field).
- LLM propose→critic→revise→gauntlet loop: `feature_assist.py:1015-1157` (critic-clean features
  kept, flagged ones revised once under critic notes, machine `avoid` feedback separated from human
  `feedback`).
- Adversarial critic seam for the bridge family: `attest/bridge_critic.py`; shared
  content-addressed LLM structured-result store: migration `1039`.
- Data-agent clarification loop: abstention → typed question → candidate plan from a closed
  vocabulary (`ad02395c`, plan preview `534290f7`, grounded narrative with cell citations
  `d8aa006a`, API `4cf48389`).
- Learning/gap store: `data_agent/learning.py` — `record_gap`, `record_refusal`, `resolve_gap`,
  `open_gaps` (migration `1034_analysis_learning_event`).
- Deterministic observation executor + dialects: `data_agent/executor.py` (`Dialect` protocol,
  `effective_method`), `sql_hive.py`, `sql_postgres.py`, `data_observation` store (`1033`).
- Post-parse human review vehicle: the `semantics_pending` connector workflow
  (`UploadScreen.tsx` already carries `onSemanticsQueue`).
- Sealed feature-generation snapshot: `catalog_metadata_snapshot_item` is a generic item store
  (`item_kind`, `value_json`, `authority_json {authority, provenance, status}`,
  `decision_event_id`, `item_hash`; deferred FK; content-hash sealed) —
  `feature_metadata_snapshot.py:451-470`. New item kinds slot in without schema change.
- Governance queue folds the authoritative event stream (`bridge_governance.py` uses
  `load_fact`/`fold_overlay_state`), with orientation convergence and honest truncation
  (`cdce4739`, `3a402e98`, `13e35732`, `b25374c1`).

### Verified missing (this plan’s scope)

- Upload accepts only `file` + `source` (`api/routes/uploads.py:123-125`); no catalog narrative.
- `GET /catalogs` returns slug + read-scoped counts only (`api/routes/catalogs.py:39`,
  `list_visible_catalogs`); no display name or profile.
- Table role is recommendation-only, not uploader-editable, and **not surfaced anywhere**: search
  facets are source/domain/sensitivity/additivity/entity/kind + grain/as-of flags
  (`search.py` `_COLUMN_FACETS`/`_FLAG_FACETS`) — no `table_role` facet, no asset-detail display.
- No authority-role axis (system of record vs replica) anywhere.
- OpenMetadata import drops table descriptions — the mapping comment says it verbatim: *“table
  descriptions have no home”* (`connectors/openmetadata.py:11`); column descriptions map to
  `definition`.
- No human-authored business dependencies; existing dependency stores are technical
  invalidation/lineage indexes.
- No crosswalk data model: the bridge stores two scalar endpoints; no mapping-table realization,
  validity interval, allocation rule, or relationship kind.
- Planner availability population is still ledger-driven (`cross_catalog_links.py:270` reads
  `FROM entity_bridge_candidate_evidence`) while governance folds the stream — the two can
  disagree about whether an available fact exists (bridge plan Tasks 6B/9 remain open).
- No serving-purpose source resolver; no current-vs-historical request policy; the generic
  materializer join adapter remains single-catalog.
- Generated artifacts and successful analyses do not retain complete relationship-usage lineage,
  and the sealed snapshot has no profile/source-selection item kinds.

## Relationship to Existing Plans

### Authoritative ownership

- `2026-07-28-product-roadmap-and-contract-ownership.md` — owns the release sequence, the three
  use tiers, the governed-fact authority rule, and the shared physical identity / dependency
  snapshot / candidate lifecycle / execution IR.
- `2026-07-29-bridge-cardinality-and-link-trust-remediation.md` — owns identifier-link assessment
  revisions, directional realizations, pairwise observations, deterministic safety validation,
  available-link vs executable-realization readers, cross-catalog physical IR, and evidence
  currentness. **Tasks 0B, 1–8 are built; Tasks 6B, 9–12 are open.** Task 0 here records the
  precise completion matrix; this plan’s Tasks 7–9 compose with those open tasks rather than
  waiting on the whole plan.
- `2026-07-29-governance-review-redesign.md` — owns the cross-catalog decision queue and bridge
  confirmation/rejection UX; extended, not rebuilt. Must be present on the merged baseline.
- `2026-07-12-phase2-table-facts.md` — owns Pass-B grain/availability/table-role proposals; this
  plan promotes the semantic-profile concern into a versioned model without weakening the
  grain/as-of authority gates.
- `2026-07-28-data-powered-ontology-analysis-agent-program.md` — owns the observation workflow and
  analysis product; this plan makes profiles, crosswalks, and temporal selection consumable by it
  and routes refusals into its clarification/learning loops.

### No duplicate contracts

This plan must not introduce:

- another physical object or physical binding type (reuse `PhysicalObjectIdentityV1`,
  `PhysicalDatasetBindingV1`, migration `1036` revisions);
- another generic candidate lifecycle (reuse `bridge_store.py` / migration `1037`);
- another dependency snapshot;
- another LLM call/replay store (reuse `llm_call` + the `1039` structured-result store);
- another relationship observation executor (reuse `relationship_observation.py` / `1038`);
- another execution interpreter;
- **another availability definition** — the frozen family is `LinkAvailability`,
  `LinkUnavailableReason`, `FoldedLinkStatus`, `OverlayIdentifierLinkStateV1` in
  `bridge_assessment.py`; this plan extends it with `relationship_kind` and nothing else;
- a second directional realization model beside `BridgeJoinRealizationRevisionV1` / the shared
  relationship realization family.

Where this plan names a crosswalk realization, it is a typed member of the shared relationship
realization family, not a parallel planner.

## Architectural Decisions

### A. Classify at two levels

A catalog may contain facts, dimensions, references, and crosswalks together. Therefore:

- a catalog profile contains defaults and a summary;
- a dataset/table profile is the authoritative classification for that table;
- an explicit dataset value overrides an inherited catalog default;
- a catalog with multiple effective table roles displays `mixed`, never a false single label.

### B. Data role and authority role are independent

`dimension` describes data shape and analytical use. It does not mean “master”, “current”, or
“system of record”. Two closed axes:

```text
data_role =
  event_fact | snapshot_fact | fact | dimension | reference | crosswalk | unknown

authority_role =
  system_of_record | mastered_view | authoritative_replica |
  non_authoritative_replica | derived | external_reference | unknown
```

`data_role` extends `table_vocab.CANONICAL_TABLE_ROLES` with `crosswalk` and `unknown`; legacy
`fact` is retained exactly as the shipped normalizer retains it. Neither axis supplies uniqueness,
join cardinality, physical source selection, or PIT semantics by itself.

### C. Suggestion, assertion, and operational authority are different

Every profile field carries provenance and authority:

```text
llm_suggested | uploader_asserted | source_attested | human_confirmed | deterministically_observed
```

- LLM values are suggestions, and every suggestion must survive a refute-oriented critic pass
  (Decision L) before it is stored as the current suggestion.
- Uploader edits are human-authored assertions and may become the current descriptive profile.
- Source metadata may attest structural facts.
- Operational grain, cardinality, source selection, and temporal behavior still pass their own
  governed fact/evidence gates.
- An uploader may set the displayed authority classification. It becomes source-selection authority
  only when the actor also holds the dedicated source-governance permission or a separately governed
  source-selection policy adopts it.
- A description or free-text dependency is never executable.

Advisory fields (description, business context, data role, functional meaning) do not need
four-eyes to become the current display profile. Load-bearing fields keep their existing authority
rules.

### D. “Latest” has two separate meanings

1. **Dataset/source selection:** which physical/canonical copy should be read?
2. **Row/snapshot selection:** which version of an entity row is valid for the request?

The source resolver uses explicit authority and freshness. The row resolver uses the temporal model
and request cutoff. Upload time is neither business effective time nor source freshness unless a
source contract explicitly says so.

### E. Current requests and historical requests use different defaults

For a current request, an authoritative master dataset may default to its current row or latest
eligible snapshot.

For a historical request:

- current-value joins are forbidden for temporal dimensions;
- select the row valid at the requested cutoff/event/period end;
- select only data available by that cutoff;
- refuse overlapping/tied records rather than choosing one;
- refuse an unknown temporal model rather than silently using today's value;
- a `current_only` dataset resolves `EXPLICIT_ONLY` — there is no defined vintage evidence for it,
  so no automatic historical default exists.

An explicit user override is allowed, but it is typed, shown in plan preview, included in lineage,
and scoped to the request. It does not mutate the catalog's default profile.

### F. Relationship type is explicit

```text
direct_equality | crosswalk | transformed | semantic_only
```

- `direct_equality`: endpoint tuples can be compared directly.
- `crosswalk`: a governed mapping dataset connects two identifier namespaces through two join legs.
- `transformed`: a closed, versioned normalization maps values before comparison.
- `semantic_only`: useful for discovery/explanation and never executable.

The existing scalar `entity_bridge` is adapted as `direct_equality` unless evidence says it is only
semantic. Do not call every entity bridge a crosswalk.

### G. Crosswalk is a data model, not a label

A table having `data_role=crosswalk` does not by itself create an executable relationship. A
crosswalk realization also declares: mapping dataset binding/revision; source key tuple;
mapping-left key tuple; mapping-right key tuple; target key tuple; effective-from/effective-to or
snapshot scope; active/preferred mapping rule where applicable; mapping grain and uniqueness
evidence; direction-specific cardinality; conflict or allocation policy.

Many-to-many mappings are production-ineligible until a governed allocation policy exists.

### H. Human review, availability, and safety remain independent

Reuse the frozen policy exactly as implemented (`bridge_assessment.py`,
`cross_catalog_links.AVAILABLE_STATUSES`):

```text
availability:  DRAFT | PARTIALLY_CONFIRMED | VERIFIED -> available
               REJECTED | REVERIFY | STALE | unreadable/missing -> unavailable
review_status: unreviewed | human_verified | review_provenance_unknown
safety_status: unassessed | deterministically_validated | unsafe
```

Human review never makes an unsafe relationship safe. Lack of review never hides an otherwise
available relationship. Production execution requires a current safe realization, not a review.

### I. Directional safety must be visible

A symmetric link can be safe in only one direction. API and UI payloads must report:

```text
A -> B: many_to_one, validated
B -> A: one_to_many, sandbox_only
```

Never collapse that into a generic “validated for production” badge on the unordered link.

### J. Free text cannot become a hidden query language

Descriptions, business context, functional meaning, and free-text dependencies are bounded text.
Executable dependencies, predicates, transformations, source choices, and temporal rules use closed
typed contracts. No stored free SQL, templated SQL, or natural-language predicate is executed.

### K. A refusal is a work item, never a dead end

Every typed refusal — ambiguous source, unknown temporal model, overlapping SCD rows, missing
allocation policy — routes into:

1. the data agent’s clarification flow (a typed question from a closed vocabulary) when a user is
   present, and
2. `learning.record_gap` / `open_gaps` when not, so the governance queue and enrichment work are
   prioritized by actual blocked demand.

Fail-closed stays fail-closed; it just stops being silent.

### L. Suggest → probe → observe

An LLM hypothesis that is deterministically checkable must be checked, not merely displayed:

- every profile suggestion passes a refute-oriented critic (the `feature_assist` loop shape through
  the `attest` critic seam) before persisting;
- a `temporal_storage_model` hypothesis triggers a bounded deterministic probe (overlap rate,
  `effective_to` null pattern, current-flag consistency) through the existing observation executor;
  a confirmed hypothesis upgrades to `deterministically_observed`, a refuted one is re-suggested
  with the observation attached.

### M. Sandbox evidence accumulates

Every bounded relationship probe run for sandbox execution persists to the shared observation store
(`1038`) against the exact realization revision and scope. Sandbox use advances assessments toward
`deterministically_validated`; nothing is measured twice and thrown away.

## Core Contracts

Task 1 owns the exact module locations after interface verification. The separation and field
ownership below are fixed. All bridge-family types are **imported from
`bridge_assessment.py`/`bridge_realization.py`, never restated.**

```python
class DataRole(StrEnum):
    EVENT_FACT = "event_fact"
    SNAPSHOT_FACT = "snapshot_fact"
    FACT = "fact"                    # legacy canonical role, kept per table_vocab
    DIMENSION = "dimension"
    REFERENCE = "reference"
    CROSSWALK = "crosswalk"
    UNKNOWN = "unknown"


class AuthorityRole(StrEnum):
    SYSTEM_OF_RECORD = "system_of_record"
    MASTERED_VIEW = "mastered_view"
    AUTHORITATIVE_REPLICA = "authoritative_replica"
    NON_AUTHORITATIVE_REPLICA = "non_authoritative_replica"
    DERIVED = "derived"
    EXTERNAL_REFERENCE = "external_reference"
    UNKNOWN = "unknown"


class TemporalStorageModel(StrEnum):
    CURRENT_ONLY = "current_only"
    SCD1 = "scd1"
    SCD2 = "scd2"
    SNAPSHOT = "snapshot"
    EVENT_LOG = "event_log"
    UNKNOWN = "unknown"


class DefaultTemporalSelection(StrEnum):
    CURRENT_RECORD = "current_record"
    LATEST_AVAILABLE_AS_OF = "latest_available_as_of"
    VALID_AT_REPORT_CUTOFF = "valid_at_report_cutoff"
    VALID_AT_EVENT_TIME = "valid_at_event_time"
    PERIOD_END_PER_PERIOD = "period_end_per_period"
    EXPLICIT_ONLY = "explicit_only"


class ServingPurpose(StrEnum):
    FEATURE_SERVING = "feature_serving"
    ANALYTICAL = "analytical"
    OPERATIONAL = "operational"
    AUDIT = "audit"
    ANY = "any"
```

```python
CatalogSemanticProfileRevisionV1(
    catalog_source,
    profile_revision_id,
    display_name,
    description,
    business_domains,
    functional_context,
    owner_refs,
    steward_refs,
    default_data_role,
    default_authority_role,
    declared_refresh_cadence,
    dependency_declarations,
    evidence_refs,               # EvidenceRefV1 (frozen bridge contract)
    created_by,
    created_at,
)

CatalogSemanticProfileCurrentV1(catalog_source, profile_revision_id, pointer_version)

DatasetSemanticProfileRevisionV1(
    dataset_logical_ref,
    profile_revision_id,
    authored_under_catalog_revision_id,  # PROVENANCE ONLY — the catalog revision current when this
                                         # revision was authored. Effective inheritance ALWAYS
                                         # resolves against the catalog's CURRENT pointer; the
                                         # resolved pair is recorded in lineage at use time.
    display_name,
    description,
    business_context,
    functional_context,
    data_role,
    authority_role,
    primary_entity,
    granularity_description,
    governed_grain_fact_key,
    temporal_storage_model,
    default_temporal_selection,
    effective_from_ref,
    effective_to_ref,
    current_flag_ref,
    snapshot_ref,
    availability_ref,
    key_candidates,
    dependency_declarations,
    relationship_suggestions,
    inherited_fields,            # which effective values came from the catalog default
    evidence_refs,
    created_by,
    created_at,
)

DatasetSemanticProfileCurrentV1(dataset_logical_ref, profile_revision_id, pointer_version)
```

The effective read model returns, per field: value, direct/inherited origin, provenance authority,
and evidence. Inheritance must never fabricate dataset-local evidence.

```python
DatasetDependencyDeclarationV1(
    dependency_kind,       # technical_input | business_dependency |
                           # semantic_reference | refresh_predecessor
    target_logical_ref,    # optional for unresolved/free-text business dependency
    description,
    required,
    evidence_refs,
)
```

Only a resolvable, typed `technical_input` or `refresh_predecessor` may feed invalidation or
execution ordering. Business dependencies remain explanatory until grounded.

```python
FeatureTimeContextV1(
    request_mode,          # current | historical
    report_cutoff,
    event_time_ref,
    period_definition,
    explicit_temporal_override,
)

DatasetServingPolicyRevisionV1(
    entity_id,                # split from the draft's entity_or_purpose
    serving_purpose,          # ServingPurpose
    policy_revision_id,
    eligible_dataset_refs,
    eligible_authority_roles,
    preferred_dataset_refs,
    freshness_requirement,
    ambiguity_behavior,       # closed enum; 'refuse' is the only member in this slice —
                              # 'ask' (clarification) and 'escalate' are reserved, not implemented
    evidence_refs,
)

DatasetSourceSelectionV1(
    entity_id,
    serving_purpose,
    selected_dataset_ref,
    selected_profile_revision_id,
    resolved_catalog_revision_id,   # the catalog pointer resolved at selection time
    selected_binding_revision_id,
    serving_policy_revision_id,
    authority_role,
    freshness_evidence_ref,
    selection_basis,
    considered_candidates,
)

DatasetRowSelectionV1(
    dataset_ref,
    temporal_storage_model,
    selection_kind,
    cutoff_source,
    effective_from_ref,
    effective_to_ref,
    snapshot_ref,
    availability_ref,
    tie_break_refs,
)
```

```python
class RelationshipKind(StrEnum):
    DIRECT_EQUALITY = "direct_equality"
    CROSSWALK = "crosswalk"
    TRANSFORMED = "transformed"
    SEMANTIC_ONLY = "semantic_only"


RelationshipDefinitionRevisionV1(
    relationship_fact_key,     # derived via candidate_identity_payload over the canonical
                               # UNORDERED endpoint pair + relationship_kind — the frozen identity
                               # derivation, one namespace convention, no new hashing scheme
    relationship_revision_id,
    relationship_kind,
    entity_id,
    left_endpoint,             # IdentifierEndpointV1 (frozen bridge contract)
    right_endpoint,            # IdentifierEndpointV1
    evidence_refs,
)

# AVAILABILITY: no new contract. Reuse OverlayIdentifierLinkStateV1 /
# LinkAvailability / LinkUnavailableReason / FoldedLinkStatus from bridge_assessment.py,
# extended with relationship_kind as a carried field on the read model. A definition with no
# governed fact is UNGOVERNED (the shipped cross_catalog_links rule), distinct from unreadable.

CrosswalkRealizationRevisionV1(
    realization_id,
    realization_revision_id,
    relationship_fact_key,
    direction,
    source_endpoint,                     # IdentifierEndpointV1
    mapping_dataset_binding_revision_id, # migration-1036 binding revision
    source_to_mapping_pairs,             # ColumnPairV1 tuples (frozen contract)
    mapping_to_target_pairs,
    target_endpoint,
    temporal_predicates,                 # frozen typed predicates only
    normalization_ids,
    mapping_preference_policy,
    allocation_policy_ref,
    applicability_scope,                 # RealizationApplicabilityScopeV1
    cardinality,                         # DirectionalCardinalityVerdictV1
    cardinality_basis,                   # CardinalityBasis
    assessment_refs,
    dependency_snapshot_id,
)
```

`CrosswalkRealizationRevisionV1` implements the shared relationship-realization protocol owned by
the bridge-remediation plan (`SafetyStatus`, `RealizationLifecycle`, `eligible_for_sandbox`,
`eligible_for_production` apply unchanged). It does not create a second safety/currentness store.

## Persistence Model

Additive, immutable revisions plus compare-and-set current pointers. **Migrations allocate from
`1040`** (head is `1039_structured_result_store`); Task 0 reserves exact numbers jointly with the
bridge plan’s remaining tasks in the shared verified-interface document.

Required logical stores:

```text
catalog_semantic_profile_revision / _current
dataset_semantic_profile_revision / _current
dataset_dependency_revision
dataset_serving_policy_revision / _current
relationship definition fact/revision      # bridge shared fact/revision family (1037 store)
relationship availability projection       # rebuildable; folded from the overlay stream
crosswalk realization revisions/current    # rows in the SHARED realization store (1037), typed
temporal/profile observations              # rows in the SHARED observation stores (1033/1038)
snapshot item kinds                        # catalog_metadata_snapshot_item — NEW item_kind values:
                                           #   dataset_profile, serving_policy, source_selection,
                                           #   row_selection — sealed under the same content hash
```

Rules:

- Revisions are immutable; current-pointer updates use CAS (the `bridge_store.py` pattern).
- Read models and `graph_node` projections are rebuildable.
- Existing `graph_node.table_role`, `primary_entity`, `event_or_snapshot` remain compatibility
  projections during migration.
- Existing grain and availability facts remain authoritative; profile fields reference them and do
  not replace them.
- `entity_bridge_candidate_evidence` is compatibility input, not current authority.
- `catalog_metadata_snapshot` remains a sealed replay artifact — extended with the new item kinds,
  never repurposed as a mutable profile store.
- Relationship review/lifecycle remains authoritative in the generic overlay event stream.

## End-to-End Flow

```text
upload file + catalog narrative
  -> parse catalog
  -> persist uploader-authored catalog profile revision
  -> LLM creates evidence-bound catalog/table suggestions
  -> critic pass refutes unsupported suggestions (Decision L)
  -> deterministic probes corroborate temporal/key hypotheses where cheap (Decision L)
  -> uploader reviews/overrides dataset profiles (semantics_pending vehicle)
  -> deterministic profile resolver publishes effective profile read model
  -> source selector chooses an authoritative dataset for the request
       (ambiguity -> clarification question / open gap, Decision K)
  -> temporal selector chooses current/PIT rows
  -> relationship resolver chooses direct/crosswalk/transformed realization
  -> assessment gate proves direction, scope, cardinality and fan-out
       (sandbox probes persist as observations, Decision M)
  -> planner/materializer compiles the closed realization
  -> snapshot seals profile/policy/selection items; lineage records every selected revision
```

## Task 0 — Select the Merged Baseline and Freeze Ownership

**Purpose:** Three diverging lines exist. Starting from any single one recreates already-fixed
defects or omits required modules.

**Files:**

- Update the shared verified-interface document owned by the bridge-remediation plan
- Modify this plan only when verified interfaces differ
- No product code

**Steps:**

- [ ] Create the implementation baseline by merging `feature/bridge-cardinality-link-trust-remediation`
  (`7b1a9cfa`) into `integration/ontology-data-agent` (`ec570a34`+). Verify the merge carries:
  governance-review queue + redesign fixes; bridge modules/migrations `1036`–`1039`; materializer;
  data-agent analysis + clarification stack; `docs/superpowers/plans/2026-07-29-governance-review-redesign.md`.
- [ ] Record commit SHA, migration head (`1039` expected), frontend package state, fixture data
  revision, and feature flags.
- [ ] Record the bridge-plan completion matrix (0B, 1–8 built; 6B, 9–12 open) with evidence
  anchors; downstream tasks here must consume, not rebuild.
- [ ] Reserve migration numbers from `1040` in the shared verified-interface document — never
  “allocate the next number” independently in parallel worktrees (the `1034` collision is the
  standing example).
- [ ] Run the focused backend and frontend suites; record exact counts.
- [ ] Record the CIB/FTR bridge facts, ledger rows, lifecycle states, grain facts, and physical
  binding availability on the merged tree.
- [ ] Verify whether historical proposals were authored by a user or service actor; define the
  data-repair cohort.

**Acceptance:**

- One implementation SHA and one migration allocation are named.
- Every shared concern has exactly one owning plan/module.
- The merged baseline reproduces current governance/link behavior before profile work starts.

## Task 1 — Add Profile, Authority, Dependency, and Temporal Contracts

**Purpose:** Freeze the language and invariants before persistence, APIs, or LLM prompts create
competing meanings.

**Files:**

- Create: `src/featuregen/overlay/upload/catalog_profiles.py`
- Create: `src/featuregen/overlay/upload/dataset_profiles.py`
- Create: `src/featuregen/overlay/upload/profile_vocab.py`
- Create: `src/featuregen/overlay/upload/temporal_policy.py`
- Reuse (import, never restate): `bridge_assessment.py`, `bridge_realization.py`,
  `table_vocab.py`
- Test: corresponding contract/vocabulary suites

**Steps:**

- [ ] Implement the closed enums and immutable contracts above.
- [ ] `DataRole` composes with `table_vocab.normalize_table_role` — one normalizer; profile_vocab
  maps its output into `DataRole` and adds only `crosswalk`/`unknown` handling.
- [ ] Reuse canonical logical refs; no profile-only object identity.
- [ ] Define per-field authority/provenance and inherited-versus-direct origin.
- [ ] Encode the inheritance decision: effective reads resolve against the catalog’s CURRENT
  pointer; `authored_under_catalog_revision_id` is provenance; the resolved pair is a lineage
  concern.
- [ ] Define advisory versus load-bearing fields explicitly.
- [ ] Define a `mixed` catalog summary as a derived display value, not a stored data role.
- [ ] Define bounded lengths and cardinalities for descriptions, contexts, domains, owners,
  dependencies, key candidates, and relationship suggestions.
- [ ] Define typed dependency kinds; prohibit free-text dependencies from operational use.
- [ ] Define source selection and row selection as separate contracts, keyed by
  (`entity_id`, `serving_purpose`).
- [ ] Extend the frozen relationship identity: `relationship_fact_key` via
  `candidate_identity_payload` over the canonical unordered endpoint pair + kind. A
  `direct_equality` and a `crosswalk` between the same endpoints have distinct identities.
- [ ] Reject unknown enum values field-by-field without discarding an otherwise valid profile
  revision (the `table_synth` per-field salvage rule).
- [ ] Keep free SQL and unbounded arbitrary JSON outside every contract.

**Tests:**

- A catalog default is inherited only when the dataset has no direct value.
- Advancing the catalog profile changes a dataset’s inherited effective value WITHOUT a new
  dataset revision; the dataset’s direct values are untouched.
- A dataset override preserves its own provenance.
- A mixed catalog cannot be mislabeled as one data role.
- `dimension + non_authoritative_replica` and `event_fact + system_of_record` are valid.
- A free-text business dependency cannot become a technical dependency.
- Unknown temporal storage produces `EXPLICIT_ONLY`, never `CURRENT_RECORD`.
- `current_only` + historical request resolves `EXPLICIT_ONLY`.
- Direct-equality and crosswalk definitions over identical endpoints never hash identically.
- Serialization is deterministic; revision IDs ignore live/current-pointer state.

## Task 2 — Add Immutable Profile Persistence, Effective Read Models, and Backfill

**Purpose:** One source of truth without turning `graph_node` into another mutable semantic store.

**Files:**

- Add migrations reserved by Task 0 (from `1040`)
- Create: `src/featuregen/overlay/upload/profile_store.py` (follow the `bridge_store.py`
  revision+CAS pattern)
- Create: `src/featuregen/overlay/upload/profile_resolution.py`
- Modify: `src/featuregen/overlay/upload/asset_detail.py`
- Modify: `src/featuregen/overlay/upload/catalogs.py`
- Test: migration, store, resolution, and compatibility suites

**Steps:**

- [ ] Create immutable catalog/dataset revision tables and CAS current pointers.
- [ ] Add typed dependency revision storage.
- [ ] Add immutable serving-policy revisions and CAS current pointers keyed by
  (`entity_id`, `serving_purpose`).
- [ ] Add indexes for catalog, logical dataset ref, profile revision, current pointer, and
  dependency target.
- [ ] Add write-once guards consistent with existing immutable stores.
- [ ] Implement effective-profile resolution with field-level origin/provenance, resolving
  inheritance against the current catalog pointer.
- [ ] Project compatibility values to `graph_node` only from the effective profile.
- [ ] Keep grain/availability projection independent and authoritative.
- [ ] Backfill one catalog profile per existing source: description absent; authority role
  `unknown`; no fabricated owner or dependency.
- [ ] Backfill dataset revisions from current advisory fields with their real evidence provenance;
  mark inherited/source/LLM origin honestly.
- [ ] Map legacy table role `bridge` to `crosswalk` only as a data-role suggestion; create no
  relationship realization.
- [ ] Do not backfill `system_of_record` from the word `master` in a table name — and pin the
  mirror: a replica named `master` stays `unknown`.
- [ ] Add reconciliation reporting profile/current-pointer/projection disagreement.
- [ ] Make migration replay idempotent.

**Acceptance:**

- Every visible catalog and table returns an effective profile, even if all values are unknown.
- No backfill grants new execution authority.
- Existing asset/search screens remain byte-compatible while the new fields are feature-flagged
  off.

## Task 3 — Extend Upload with Catalog Narrative and Structured Declarations

**Purpose:** Capture what the uploader knows when the catalog enters the system.

**Files:**

- Modify: `src/featuregen/api/routes/uploads.py`
- Modify: `src/featuregen/overlay/upload/ingest.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/screens/UploadScreen.tsx`
- Test: upload API, ingest, and frontend suites

**API shape:** keep `file` + `source`; add one optional bounded multipart JSON field
`catalog_profile` (display_name, description, business_domains, functional_context, owner_refs,
steward_refs, default_data_role, default_authority_role, declared_refresh_cadence,
dependency_declarations). No unversioned collection of unrelated form fields.

**Steps:**

- [ ] Validate size and structure before opening any semantic-profile write.
- [ ] Persist the raw accepted declaration hash on the ingestion run.
- [ ] Persist an uploader-authored catalog profile revision in the same successful upload
  transaction; a parse/ingest failure leaves the durable failed-run record but no current profile
  pointing at content that never ingested.
- [ ] Show description and classification fields in the upload UI; explain data role versus
  authority role in plain language.
- [ ] Every field optional; missing information is not a rejected upload.
- [ ] Route the post-parse dataset-profile review through the **existing `semantics_pending`
  workflow** (`UploadScreen.onSemanticsQueue`) — do not invent a second post-parse review loop.
- [ ] Support a versioned sidecar/manifest for bulk table declarations.
- [ ] Record uploader identity and ingestion run on every authored revision.
- [ ] Do not let `default_authority_role` select a physical source during ingest.

**Tests:**

- The old two-field upload remains compatible.
- A valid narrative creates one profile revision; invalid profile JSON fails with a bounded 4xx and
  no partial profile.
- A failed file parse cannot advance the profile current pointer.
- An uploader may declare `dimension` and `authoritative_replica` independently.
- Free-text dependencies never enter technical invalidation indexes.
- The dataset review step lands in the semantics-pending queue, not a new surface.

## Task 4 — Evidence-Bound LLM Profile Suggestions, Critic Pass, and Temporal Probes

**Purpose:** Use the LLM to reduce manual catalog work — with a refute-oriented critic and
deterministic corroboration — without granting it operational authority.

**Files:**

- Modify: `src/featuregen/overlay/upload/table_synth.py`
- Modify: `src/featuregen/overlay/upload/enrich_llm.py`
- Create: `src/featuregen/overlay/upload/profile_suggestions.py`
- Create: `src/featuregen/overlay/upload/attest/profile_critic.py` (the `bridge_critic.py`
  pattern)
- Reuse: the `1039` content-addressed structured-result store; the `feature_assist.py:1015-1157`
  propose→critic→revise→validate loop shape; `data_agent` executor/dialects for probes
- Test: prompt, schema, egress, replay, critic, probe, and authority suites

**4A — suggestions:**

- [ ] Extend bounded table synthesis to suggest: table description; business/functional context;
  data role; authority-role hypothesis; primary entity; granularity description; key candidates;
  temporal storage model; default temporal selection candidate; typed dependency candidates;
  direct/crosswalk/transformed/semantic relationship candidates.
- [ ] Require every suggestion to cite existing evidence IDs/refs.
- [ ] Reuse `table_synth`’s per-field disposition machinery (`DISPOSITION_FIELDS` extension), not a
  new salvage path.
- [ ] Preserve: LLM grain uniqueness is a claim, not proof; authority role is a hypothesis; an LLM
  cannot select a system of record; physical source selection, cardinality, fan-out, PIT
  semantics, population relation, and executable predicates stay outside LLM control.
- [ ] Bind prompts to the current profile/catalog revision and evidence revision; use the shared
  replay store and deterministic accepted-output hash; exclude sample values and sensitive rows.
- [ ] Bound the number of tables, columns, dependencies, and relationship candidates.
- [ ] Stale only the LLM producer's prior suggestions when inputs change; never stale
  uploader/source/human evidence from an LLM rerun.

**4B — critic and probes (Decisions L):**

- [ ] Run a refute-oriented critic over each suggestion batch: *given only the cited evidence, is
  this value supported, or inferred from a name?* The critic flags; flagged suggestions get one
  revise pass under the critic’s notes (the `feature_assist` loop), then per-field disposition.
  High-risk fields (`authority_role`, dependency candidates, relationship candidates) must not
  persist as current suggestions without a critic verdict.
- [ ] Wire `temporal_storage_model` hypotheses to bounded deterministic probes through the existing
  observation executor: interval-overlap rate on (key, effective_from, effective_to);
  `effective_to` null pattern; current-flag consistency; snapshot-column distinctness. Persist
  results in the shared observation store; a confirmed hypothesis upgrades provenance to
  `deterministically_observed`; a refuted one is re-suggested with the observation attached.
- [ ] Record per-field accepted/dropped/abstained/refuted disposition on the ingestion stage
  detail.

**4C — wire profiles into the existing LLM consumers (files: modify
`src/featuregen/overlay/upload/feature_assist.py`):**

- [ ] Extend the feature-idea generator’s existing `table_context` prompt input
  (`feature_assist.py` — the seam already threaded at idea generation, revision, and recipe
  relevance) with the effective dataset profile: display name, description, business/functional
  context, `data_role`, `authority_role`, and temporal storage model. An LLM that knows *what the
  table is* proposes materially better features than one reading 126 column names.
- [ ] Bind the profile text entering prompts to its profile revision ID (the same binding rule as
  4A), so the sealed snapshot’s `dataset_profile` items (Task 9) make idea generation replayable.
- [ ] Flag-gate with the profile read model; flag-off prompts are byte-identical to today.
- [ ] Bounded: profile text in prompts obeys the Task 1 length bounds; no sample values.

**Tests:**

- An LLM may suggest `system_of_record`; effective operational authority is unchanged.
- A suggestion with no evidence reference is rejected; a valid description survives an invalid role
  in the same response (per-field salvage).
- The critic refutes `system_of_record` supported only by a table named `master`.
- A refuted suggestion does not persist as current; the revise pass runs at most once.
- An SCD2 hypothesis over a table with overlapping intervals is refuted by the probe and recorded.
- A confirmed SCD2 hypothesis carries `deterministically_observed` provenance.
- Replay on the same revision is deterministic; a changed catalog revision creates new suggestions
  without mutating old ones.
- No sample values appear in any prompt or persisted suggestion.
- Flag-on, a feature-idea prompt contains the effective dataset description bound to its profile
  revision; flag-off, the prompt is byte-identical to today’s.
- Profile text in a prompt respects the Task 1 bounds and carries no sample values.

## Task 5 — Uploader Override, Profile Review UI, Search Facets, and Catalog/Dataset Screens

**Purpose:** Make the new metadata visible, searchable, and correctable.

**Files:**

- Create profile routes under `src/featuregen/api/routes/`
- Modify: `src/featuregen/api/routes/assets.py`, `src/featuregen/api/routes/catalogs.py`
- Modify: `src/featuregen/overlay/upload/search.py`
- Modify: `frontend/src/api.ts`
- Create or modify catalog/profile screens; modify `frontend/src/screens/AssetDetailScreen.tsx`,
  `SearchScreen`, `IngestResultCallout.tsx`
- Test: backend and frontend profile suites

**Routes:**

```text
GET  /catalogs/{source}/profile
POST /catalogs/{source}/profile/revisions
GET  /catalogs/{source}/datasets/{object_ref}/profile
POST /catalogs/{source}/datasets/{object_ref}/profile/revisions
GET  /uploads/{run_id}/profile-suggestions
```

**Steps:**

- [ ] **Immediate quick win (independent of new stores):** surface the existing
  `graph_node.table_role` as a search facet and an asset-detail badge — it exists today and is
  shown nowhere.
- [ ] Add `data_role`, `authority_role`, and `temporal_storage_model` search facets from the
  effective-profile projection (flag-gated with the projection), read-scoped like every existing
  facet.
- [ ] **Index profile text for full-text search:** extend the single `_SEARCH_DOC` expression in
  `graph.py` (insert-time INSERTs and `rebuild_search_doc` render the same expression — keep that
  invariant) so table nodes fold in the effective profile display name and description; rebuild
  affected docs when a profile current pointer advances. Searching “customer master” must find the
  table by its description, not only by a lucky column-name match.
- [ ] **Extend data-agent question→table retrieval with profile text:** the bounded-retrieval seam
  matches questions to tables, and profile descriptions are precisely the text that makes that
  matching work. Task 0 records the concrete module; this step is retrieval-input wiring only —
  no new retrieval engine, same bounds, read-scoped.
- [ ] Return effective value, direct/inherited origin, evidence, and current revision per field.
- [ ] Use OCC/CAS on profile edits; a stale browser edit returns 409 and reloads.
- [ ] Let authorized uploaders edit advisory fields; accepting a key/relationship candidate does
  not attest uniqueness, cardinality, fan-out, or execution safety.
- [ ] Show the advisory granularity description beside — not in place of — the governed grain
  fact; keep load-bearing confirmation on the existing specialized-fact commands.
- [ ] Show LLM suggestion versus uploader value versus source-attested value, plus critic/probe
  disposition where present.
- [ ] Show catalog role distribution and `mixed` summary; add data-role and authority-role badges;
  display `unknown` rather than hiding missing classification.
- [ ] Extend `GET /catalogs` with display name, role distribution, and profile presence (keep
  slug+counts compatibility).
- [ ] Add relationship-type labels (Direct identifier link / Crosswalk mapping / Transformed link /
  Semantic-only); a crosswalk gets its own detail naming the mapping dataset and both join legs; no
  second ambiguous “Bridge” tab.
- [ ] Show profile-suggestion dispositions (accepted/dropped/abstained/refuted counts) in
  `IngestResultCallout`, like other stages.
- [ ] Import OpenMetadata **table** descriptions, owners, and supported domains into profile
  evidence with source provenance (closing the documented “table descriptions have no home” drop at
  `connectors/openmetadata.py:11`); keep unsupported fields visible as dropped in preview.

**Tests:**

- An uploader override wins over an LLM suggestion for display; an override cannot confirm
  grain/cardinality by editing text.
- Inherited values are labelled inherited; mixed catalogs display mixed.
- `table_role` facet filters search today; new facets appear only flag-on and respect read scope.
- A query matching only a table’s profile description finds the table; flag-off, `search_doc` is
  byte-identical to today’s expression.
- A data-agent question phrased in the description’s business language retrieves the table.
- Rebuilt and insert-time search docs stay identical after a profile pointer advance (the #20
  invariant extended).
- OpenMetadata table descriptions no longer disappear.
- Suggestion dispositions appear in the ingest callout.

## Task 6 — Authoritative Source Selection, Temporal Defaults, and Refusal Routing

**Purpose:** Make “use the latest master by default” precise, safe — and never silent.

**Files:**

- Create: `src/featuregen/overlay/upload/source_selection.py`
- Extend: `src/featuregen/overlay/upload/temporal_policy.py`
- Modify shared planner/analysis request contracts
- Reuse: `materialize.spine` (`LATEST_AVAILABLE_AS_OF`), availability facts, PIT renderers
  (`nodes_compute._latest_available_as_of`), the data-agent clarification flow, and
  `data_agent/learning.py`
- Test: source-selection, temporal-policy, and refusal-routing suites

### Source-selection rules

- [ ] Candidates must match the requested `entity_id` + `serving_purpose` and be readable in the
  caller's scope.
- [ ] Apply a governed `DatasetServingPolicyRevisionV1` first — a mastered analytical view may
  outrank an operational system of record for feature serving while audit requires the system of
  record.
- [ ] Within policy, rank only from explicit effective authority and named preference. `effective`
  means source-attested or accepted by a source-governance actor/policy; uploader assertions and
  LLM hypotheses remain display evidence.
- [ ] With no purpose-specific policy: admit only system-of-record, mastered-view, and
  authoritative-replica candidates and refuse ties; no invented universal order.
- [ ] Require current physical binding and freshness evidence.
- [ ] Equal ties return typed ambiguity; never break by upload time or table name.
- [ ] An explicit request override may select another readable dataset and is recorded.

### Row/snapshot-selection rules

| Request | Storage model | Default |
| --- | --- | --- |
| current | current_only / SCD1 | current record |
| current | SCD2 | row valid at current request cutoff |
| current | snapshot | latest snapshot available by cutoff |
| historical | SCD2 | row valid at report/event cutoff |
| historical | snapshot | greatest eligible snapshot `<= cutoff`, available by cutoff |
| historical | current_only | `EXPLICIT_ONLY` — refuse; no automatic default exists |
| any | unknown | explicit policy required |

- [ ] Reuse half-open SCD intervals; refuse overlap and unresolved ordering ties.
- [ ] Keep effective time and availability time as separate filters.
- [ ] Expand beyond `REPORT_CUTOFF` only through explicit event-time and period-end
  implementations; never implement `CURRENT_VALUE` as a historical fallback.
- [ ] **Flag-gate identity binding:** binding resolved source/row policy into plan identity changes
  every existing plan’s identity — shadow first (Task 11), bind on flip, exactly the 3B.4
  discipline.

### Refusal routing (Decision K)

- [ ] Every typed refusal (ambiguous source, unknown temporal model, SCD overlap, missing policy)
  emits: a clarification question through the data agent’s existing typed-question flow when
  interactive, and `learning.record_gap` when not.
- [ ] `open_gaps` demand counts surface on the governance dashboard so enrichment work is ranked by
  blocked demand.
- [ ] A resolved gap (e.g., an adopted serving policy) closes via `resolve_gap` with the resolving
  decision recorded.

**Required tests:**

- Current Customer request selects the effective system-of-record/mastered dataset.
- Historical Customer request never joins today's segment.
- A later-arriving row effective before cutoff is excluded if unavailable by cutoff.
- Two active SCD rows refuse; two equally authoritative replicas refuse as ambiguous — and each
  refusal creates exactly one clarification question or gap demand, idempotently.
- Upload timestamp cannot win source or row selection.
- A user override is request-scoped and auditable.
- Historical + `current_only` refuses with `EXPLICIT_ONLY`, not a silent current-value join.

## Task 7 — Extend the Shared Relationship Model with Crosswalks

**Purpose:** Model `cust_num -> cif` through a mapping dataset without pretending the endpoint
values are directly equal.

**Dependency:** the shared assessment/realization contracts are already frozen and built
(`bridge_assessment.py`, `bridge_realization.py`, store `1037`) — this task starts after Task 1
maps `RelationshipKind` onto them.

**Files:**

- Extend the shared relationship definition/realization modules (bridge family)
- Create: `src/featuregen/overlay/upload/crosswalk.py`
- Modify: relationship candidate discovery
- Add migrations only through the shared relationship stores (from Task 0’s allocation)
- Test: crosswalk contracts, discovery, and lifecycle suites

**Steps:**

- [ ] Add `relationship_kind` to the shared relationship definition; adapt legacy scalar
  `entity_bridge` rows as `direct_equality`, preserving fact keys.
- [ ] Add the governed crosswalk relationship fact/revision naming left endpoint, mapping dataset,
  right endpoint (typed endpoints, identity via `candidate_identity_payload`).
- [ ] Derive crosswalk candidates from BOTH: (a) a dataset effectively classified `crosswalk` plus
  key/relationship evidence, and (b) **LLM-proposed crosswalk candidates labelled `llm_only`** —
  an unclassified mapping table must still produce a visible, probe-validatable candidate without
  waiting for a human classification (the bridge plan’s link policy applied to crosswalks). The
  label alone remains insufficient for execution in both paths.
- [ ] Model both join legs with ordered `ColumnPairV1` tuples.
- [ ] Require complete mapping-table grain evidence.
- [ ] Support optional effective-from/effective-to, current flag, snapshot, preference, and status
  columns through the frozen typed predicates; closed normalization IDs only; no free SQL.
- [ ] Model mapping preference separately from many-to-many allocation; refuse many-to-many
  production use without a governed allocation policy.
- [ ] One directional realization per execution direction; never reverse cardinality by copying a
  verdict.
- [ ] Semantic-only relationships stay discoverable and structurally non-executable.
- [ ] A direct equality and a crosswalk between the same endpoints keep distinct identities and
  explanations.

**Required fixtures:**

```text
direct:    transactions.cif_id = customer_master.cif_id
crosswalk: transactions.cust_num -> customer_cif_crosswalk.cust_num
             -> customer_cif_crosswalk.cif_id -> customer_master.cif_id
temporal:  same, valid only when effective_from <= cutoff < effective_to
```

**Tests:**

- Crosswalk execution renders two join legs, never direct endpoint equality.
- Duplicate current mappings make the relevant direction unsafe.
- Historical mapping uses the mapping valid at the requested cutoff.
- Reversing a safe many-to-one realization can become one-to-many and is reported separately.
- A crosswalk table label without mapping keys creates no executable relationship.
- An `llm_only` crosswalk candidate is visible, labelled, and probe-validatable without any human
  classification of the mapping table.
- A semantic-only relation can never enter the executable reader.

## Task 8 — Integrate Measured Evidence and Repair Link Currentness

**Purpose:** Connect the probe evidence to the shared assessment store and close the remaining
governance/planner population split.

**Ownership:** the observation plan/store (`1038`), assessment revisions, and duplicate-proposal
evidence refresh (`bridge_propose.py:141-144`) are BUILT. This task delivers what remains.

**Files:**

- Modify: `src/featuregen/overlay/upload/cross_catalog_links.py` (available-link population)
- Modify: ranking/strength derivation; reconciliation
- Test: relationship evidence, reassessment, and reader-consistency suites

**Steps:**

- [ ] **Unify the available-link population on the event stream** — `cross_catalog_links.py:270`
  still reads `FROM entity_bridge_candidate_evidence` while `bridge_governance.py` folds
  `load_fact`; make the stream the population and the ledger optional enrichment, so governance and
  planner cannot disagree about whether an available fact exists.
- [ ] Reconcile/backfill existing governed bridge facts without candidate-ledger rows.
- [ ] Replace raw/frozen grain safety ranking: governed complete-key evidence in the safety band;
  uploader/LLM grain membership only as a labelled advisory hint.
- [ ] Bind every strength/ranking component to its evidence authority and freshness.
- [ ] Expose direction-specific eligibility and reason.
- [ ] Invalidate current assessments/realizations when profile, binding, grain, temporal, mapping,
  or normalization dependencies change (extend the shared dependency index with profile/mapping
  dependency kinds).
- [ ] Keep: exact-complete-only uniqueness proof; samples disprove but never prove; no
  source-key-frequency-as-fan-out (all already enforced in the shipped observation contracts —
  regression-pin, don’t rebuild).

**Tests:**

- Governance and planner return the same available link set.
- Missing ledger enrichment does not hide an available proposed link.
- New attested type/grain evidence advances the current assessment.
- Raw `is_grain=true` cannot create a production-safety score.
- A safe A→B direction does not label B→A safe.
- Evidence from one partition cannot validate a global realization.
- A changed crosswalk mapping revision invalidates both dependent directions.
- A changed dataset profile (temporal refs, key candidates) invalidates dependent realizations.

## Task 9 — Compile Direct and Crosswalk Relationships into Shared Execution

**Purpose:** Make profile/source/time/relationship decisions reach generated code — and seal them
for replay.

**Dependency:** composes with the bridge plan’s open Task 9 (cross-catalog IR and materializer);
one implementation, jointly scheduled via Task 0’s ownership record.

**Files:**

- Extend shared planner and materialization IR modules
- Modify source/temporal planning composition
- Modify: `src/featuregen/overlay/upload/feature_metadata_snapshot.py`
- Add pre-computation relationship gate and post-computation validation
- Test: planner, renderer, authorization, runtime-gate, and replay suites

**Steps:**

- [ ] Resolve in order: time context → authoritative source → row/snapshot policy → relationship
  definition → directional realization → current assessment/safety → physical read authorization.
- [ ] Carry catalog and dataset profile revision IDs into compiler context.
- [ ] Keep single-catalog `plan_join()` strict; use the typed cross-catalog adapter for
  direct/transformed/crosswalk realizations.
- [ ] Add every crosswalk dataset/key/predicate column to the physical read set.
- [ ] Render only closed equality, normalization, temporal, status, and preference predicates.
- [ ] **Seal replay:** write `dataset_profile`, `serving_policy`, `source_selection`, and
  `row_selection` item kinds into `catalog_metadata_snapshot_item` under the same content hash, so
  formula/recipe replay reproduces the same source and rows (`recipe_formula_worker.py` /
  `recipe_authoring.py` read this store).
- [ ] Recheck lifecycle, safety, evidence freshness, profile currentness, source freshness, and
  temporal applicability immediately before execution.
- [ ] For marked sandbox: allow an available unreviewed relationship; run bounded relationship
  probes; **persist every probe result to the shared observation store against the exact
  realization revision and scope** (Decision M); show unresolved safety; prevent publication.
- [ ] For production: require deterministic validation of the exact direction and scope; refuse
  unknown cardinality; refuse fan-out/many-to-many without allocation; validate mapping uniqueness
  before join; validate output key uniqueness and amplification after join.
- [ ] Never make human review a production predicate.
- [ ] Never allow “latest master” to overwrite an explicit historical cutoff.
- [ ] **Give `data_role` advisory planner consumers** (advisory reason codes, never hard gates —
  the profile is not join/cardinality authority):
  - emit an advisory warning code when an aggregation stage targets a table whose effective
    `data_role` is `dimension`/`reference` (summing a dimension is usually a modelling error);
  - prefer `event_fact`/`snapshot_fact` tables when ranking spine candidates in data-agent
    planning, as a tiebreaker below governed evidence;
  - both surface in plan preview with the profile revision they came from.

**Required end-to-end cases:**

- Current feature using the latest eligible authoritative Customer copy.
- Historical feature using PIT Customer dimension rows.
- Direct cross-catalog identifier equality; date-qualified direct realization.
- Current crosswalk mapping; historical effective-dated crosswalk mapping.
- Reversed fan-out direction refused in production.
- Same cases in sandbox with explicit warnings when unassessed — and the sandbox run’s probe
  observations visible in the assessment afterward.
- Replay of a sealed snapshot reproduces the identical source selection after a serving-policy
  edit.
- Aggregating over a `dimension`-classified table carries the advisory warning in plan preview;
  the warning never blocks an otherwise governed plan.

## Task 10 — Complete Lineage, Governance Queue, and Explainability

**Purpose:** Every choice inspectable; every stale dependency traceable; every refusal visible as
demand.

**Files:**

- Modify contract/artifact dependency stores selected by the bridge plan
- Add or extend data-agent analysis-run persistence
- Modify governance queue API/read model; governance and asset/catalog frontend surfaces
- Test: lineage, queue, stale propagation, and UI suites

**Lineage must record:**

```text
catalog_profile_revision_id        resolved_catalog_revision_id (inheritance pair)
dataset_profile_revision_id        physical_binding_revision_id
serving_policy_revision_id         source_selection_basis
feature_time_context               row_selection_policy
relationship_fact_key              relationship_definition_revision_id
realization_revision_id            assessment_revision_id
dependency_snapshot_id             normalization_ids
mapping dataset revision
```

**Steps:**

- [ ] Extend generated-artifact lineage with relationship/profile/temporal dependencies.
- [ ] Persist successful data-agent analysis runs and their relationship usage; feed refusals
  through `record_refusal` (already shipped) and surface `open_gaps` demand counts on the
  governance dashboard.
- [ ] Keep planned, selected, generated, published, and analysis usage categories distinct; report
  “not tracked yet” instead of zero for uninstrumented categories.
- [ ] Governed profile decisions enter the queue only when they require a governed action; advisory
  edits stay on the profile surface.
- [ ] Show: crosswalks as a separate relationship kind; review and safety independently;
  direction-specific cardinality/eligibility; selected source and why it won; current-vs-historical
  rule and cutoff; inherited-vs-direct fields; actual measurement metrics and freshness — never an
  unexplained confidence score.
- [ ] On staleness, block new execution/reuse and visibly mark dependent artifacts; do not claim
  old published rows were corrected.

**Tests:**

- A generated crosswalk feature traces through mapping table, realization, assessment, source, and
  temporal policy.
- A data-agent analysis appears in relationship usage; its refusal (when refused) appears as a gap.
- A stale profile/source/mapping/evidence revision blocks reuse.
- The UI never calls an unordered link generically production-safe.
- Zero and “not tracked yet” remain distinguishable.

## Task 11 — Rollout, Compatibility, and Live Data Reconciliation

**Purpose:** Introduce new semantics without silently changing existing feature outputs.

**Steps:**

- [ ] Independent feature flags for: profile persistence/read model; upload profile fields; LLM
  profile suggestions (+critic); temporal probes; source/temporal selection shadow; crosswalk
  candidate discovery; crosswalk sandbox execution; crosswalk production execution.
- [ ] Dual-read effective profiles against legacy `graph_node` fields; report disagreement.
- [ ] Keep legacy writes/projections until all consumers use the effective reader.
- [ ] Shadow source and temporal selection without changing generated plans or plan identity;
  identity binding flips only with the flag (Task 6).
- [ ] Reconcile existing CIB/FTR: nine bridge facts; candidate evidence rows; actor/four-eyes
  state; current grain/as-of facts; binding revisions; missing/stale evidence.
- [ ] Backfill/repair evidence without fabricating human confirmation.
- [ ] Classify the Customer relationship honestly (direct equality vs crosswalk vs transformed vs
  semantic-only) from evidence; do not preserve a misleading legacy label.
- [ ] Run exact bounded direct/crosswalk probes where physical access exists (the local Spark/Hive
  sandbox `e689de51` covers rendering/transport before bank access).
- [ ] Compare shadow-selected current/PIT rows and sources with hand-reconciled expectations.
- [ ] Sandbox first; production only after Task 12 gates and one hand-reconciled pilot pass.
- [ ] Rollback by disabling new readers/execution, never by deleting immutable revisions.

**Live acceptance:**

- Upload UI captures a catalog description and authority/data-role defaults.
- LLM suggests table profiles; the critic refutes at least one unsupported suggestion in the live
  run’s disposition record; uploader overrides one.
- Catalog and asset screens show direct/inherited values and provenance; search filters by role
  facets.
- Current Customer feature chooses the intended authoritative current dataset; historical uses the
  correct PIT dimension.
- A direct link executes without human review once its exact realization is deterministically safe.
- A mapping-table crosswalk renders two join legs and the correct effective mapping.
- Unsafe reverse/fan-out remains sandbox-only or refused; a sandbox run leaves a persisted
  observation.
- An ambiguous-source request produces a visible clarification/gap, and resolving it unblocks the
  request.
- Every result traces to exact profile, source, temporal, realization, and evidence revisions;
  snapshot replay reproduces the selection.

## Task 12 — Adversarial, Mutation, and Definition-of-Done Gates

**Required adversarial cases:**

- one catalog containing both facts and dimensions;
- a dimension that is a non-authoritative replica; a snapshot fact that is the authoritative
  current source;
- two equally authoritative copies; misleading table name containing `master` (both directions:
  master-named replica stays unknown; nothing backfills system_of_record from the name);
- LLM suggesting system of record with no authoritative evidence — critic refutes;
- critic wrongly flagging a well-evidenced suggestion — revise pass restores it with evidence;
- SCD2 hypothesis refuted by overlap probe; SCD2 hypothesis confirmed and upgraded;
- uploader overriding an LLM role; stale inherited catalog default (catalog pointer advanced);
- free-text dependency resembling SQL;
- direct equality versus real crosswalk between the same endpoints;
- crosswalk label with no mapping keys; `llm_only` crosswalk candidate on an unclassified mapping
  table;
- duplicate current crosswalk mappings; effective-dated crosswalk with overlap;
- many-to-many crosswalk with no allocation policy; transformation causing collisions;
- relationship fact present but candidate ledger missing (stream-population rule);
- newer evidence stronger than frozen legacy evidence; raw grain membership without complete
  governed grain;
- direction A→B safe and B→A unsafe;
- current master request; historical SCD2 request; historical request against current-only dataset
  (`EXPLICIT_ONLY`); late-arriving dimension record; tied latest snapshot;
- ambiguous source producing exactly one gap demand, idempotent across retries;
- sandbox probe observation advancing an assessment;
- snapshot replay after a serving-policy edit;
- a feature-idea prompt for a profiled table containing its description; the same prompt flag-off
  byte-identical to today;
- a search query matched only by profile description finding the table;
- a dimension-aggregation advisory appearing in plan preview without blocking the plan;
- source/profile/evidence revision changing during compilation;
- generated artifact and analysis lineage after staleness.

**Required mutations:**

- collapse data role and authority role;
- classify a whole mixed catalog from one table;
- allow LLM authority to select a source; skip the critic pass; persist a refuted suggestion;
- use upload time as business effective time;
- join current dimension values into a historical request;
- turn unknown temporal storage into current-value fallback; give `current_only` a historical
  default;
- treat a `crosswalk` table label as an executable relationship; render a crosswalk as direct
  endpoint equality;
- use raw `is_grain` as governed uniqueness;
- read the ledger instead of the stream as the available-link population;
- mark both directions safe from one far-side grain;
- make human verification satisfy deterministic safety;
- discard sandbox probe results instead of persisting;
- swallow a refusal without a question or gap;
- omit serving-policy/source-selection items from the sealed snapshot;
- resolve inheritance against the pinned catalog revision instead of the current pointer;
- **drop profile text from the feature-idea `table_context` prompt** — a wiring test must die;
- **drop profile display name/description from `_SEARCH_DOC`** — the description-only search test
  must die;
- **drop profile text from data-agent retrieval input** — the business-language retrieval test
  must die;
- silence the dimension-aggregation advisory, or promote it into a hard gate — both must die;
- turn “not tracked yet” into zero;
- drop profile/temporal/relationship revisions from lineage.

The mutation harness must include a literal focused-test count, one must-die sentinel, one
must-survive no-op, and proof the suite executed.

## Execution Order

```text
Task 0  merged baseline, ownership matrix, migration reservation (from 1040)
  -> Task 1  contracts (imports frozen bridge family)
  -> Task 2  profile persistence/read model/backfill
  -> Tasks 3 and 4  upload declarations + LLM suggestions/critic/probes   (parallel)
  -> Task 5  profile review/UI/search facets
  -> Task 6  source and temporal selection + refusal routing

Bridge contracts already frozen (Tasks 1–8 built)
  -> Task 7  relationship kinds and crosswalks        (after Task 1 here)
  -> Task 8  stream population, ranking, currentness  (joint with bridge Task 6B)
  -> Task 9  shared planner/materializer/runtime/snapshot sealing (joint with bridge Task 9)
  -> Task 10 lineage/governance/explainability
  -> Task 11 rollout/live reconciliation
  -> Task 12 adversarial and mutation gates
```

Task 5’s `table_role` facet quick-win may land any time after Task 0. Task 6 may develop against
direct single-catalog fixtures in parallel with Task 7. Crosswalk persistence, assessment, and
execution reuse the bridge substrate (`1036`–`1039`) and cannot start on independent tables or
readers.

## Definition of Done

1. Every catalog has a versioned profile and every table an effective dataset profile.
2. Data role and authority role are separate, visible, provenance-backed, and searchable.
3. Mixed catalogs are represented honestly.
4. Uploaders can supply and override descriptive/classification metadata.
5. Every LLM profile suggestion is evidence-bound, critic-reviewed, and never hidden operational
   authority; deterministically checkable hypotheses (temporal model) are probed, not merely
   displayed.
6. Table descriptions and supported upstream metadata are no longer discarded.
7. Grain, primary-key, source, temporal, and relationship suggestions remain distinct from their
   operational evidence.
8. Direct equality, crosswalk, transformed, and semantic-only relationships are different typed
   objects sharing the frozen identity/availability/safety families.
9. A crosswalk names a mapping dataset, both join legs, temporal scope, grain, and
   conflict/allocation policy.
10. Proposed/unreviewed relationships — including `llm_only` crosswalk candidates — remain
    available to discovery, feature generation, data-agent planning, and marked sandbox use.
11. Production execution depends on current deterministic safety evidence, never human review.
12. Governance and planner return the same available relationship population (stream-derived).
13. Relationship evidence refreshes when stronger or newer inputs arrive; sandbox probe results
    persist and advance assessments.
14. Raw/frozen grain hints cannot masquerade as governed complete-key safety.
15. Direction-specific safety is visible in APIs, UI, plans, and lineage.
16. Current master-data requests select an explicitly authoritative current source; historical
    requests always use an explicit PIT rule and never silently use today's dimension.
17. Every typed refusal produces a clarification question or an open gap demand; nothing fails
    silently.
18. Direct and crosswalk relationships compile into the shared execution path with complete read
    authorization and pre/post join validation.
19. Generated artifacts and analyses trace to exact profile, source, temporal, realization, and
    evidence revisions — and the sealed snapshot reproduces the same selections on replay.
20. Stale dependencies block new execution/reuse and are visible without falsely claiming
    historical outputs were corrected.
21. **Every field this plan adds has at least one named non-display consumer, pinned by a
    mutation:** profile text reaches feature-idea prompts, search full-text, and data-agent
    retrieval; `authority_role` drives source selection; `temporal_storage_model` drives row
    selection; `data_role` drives crosswalk discovery and advisory planner warnings. Metadata the
    tool collects but never consumes is a test failure, not a dashboard.

## Explicitly Deferred

- Fuzzy/probabilistic entity resolution from names, addresses, or other personal attributes.
- Unrestricted user-authored SQL transformations or relationship predicates.
- Automatic many-to-many allocation without a governed business policy.
- Full ontology graph/ER exploration product; unrestricted multi-hop traversal (the two-leg
  crosswalk is the only multi-hop shape in this slice).
- `ambiguity_behavior` values beyond `refuse` (`ask`/`escalate` reserved; Decision K routes the
  refusal externally in this slice).
- Automatic physical withdrawal or restatement of already-published feature rows.
- General scheduler/worker, tenant, quota, mTLS, secret-manager, and disaster-recovery programmes.
- Treating an LLM or uploader statement alone as proof of source authority, uniqueness,
  cardinality, fan-out, population, or PIT correctness.
