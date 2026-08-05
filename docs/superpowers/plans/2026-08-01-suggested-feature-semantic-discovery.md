# Suggested Feature Semantic Discovery and UI — Implementation Plan

> **Status:** ADVERSARIAL-REVIEW REVISED; READY FOR TASK 0F ONLY. Functional implementation is blocked
> only on the minimal contract/ownership work in Task 0F, the shared JCS/evidence primitives in Task
> 0S and the two bounded baseline correctness repairs in Task 0C. Production activation remains
> blocked on Task 0P, the durable-projection gates and Release C.
> No deployment, backfill, catalog upload, catalog re-upload or live LLM call is authorized by this
> document. LLM-assisted taxonomy proposal support may be implemented, but running it requires the
> normal separately approved/audited dispatch path.

## Goal

Turn the existing deterministic, no-hypothesis feature suggestions into a discoverable and
understandable product surface:

```text
catalog upload + semantic/profile enrichment
        -> deterministic template grounding
        -> FeatureSuggestionV2 with business meaning, safety and provenance
        -> resumable current projection + search index
        -> global discovery, table suggestions and column-usage UI
```

An end user must be able to answer, without opening source code:

- What does this feature measure?
- Which feature category, business domains and use cases does it belong to?
- What entity and grain is it calculated for?
- What time window and point-in-time rule does it use?
- Which catalogs, tables, columns and relationships does it depend on?
- Is it design-checked, waiting for evidence, or stale?
- Which warnings or limitations apply?
- Which values are recipe-authored, catalog-derived, human-authored or LLM-proposed?

This is the successor to P4 v1, present on the reviewed `origin/main` as
`docs/superpowers/plans/2026-07-27-p4-suggested-features-v1.md`. It retains P4's read-only and
non-fabrication posture while closing the semantic, discovery, scalability and UI gaps deliberately
deferred from v1. Task 0F must stop if the implementation branch does not yet contain that P4 base.

The delivery deliberately separates **functional value** from **production hardening**. Tasks 1-3
must make the current table/column experience materially useful without waiting for tenancy,
retention, capacity and recovery decisions. Existing authenticated read-scope filtering remains a
functional correctness rule and is not deferred. Task 0P owns the additional production envelope
before durable global search is activated.

## Journey Ownership

This plan primarily owns the **catalog discovery journey**:

```text
upload catalog
  -> enrich and profile catalog/table/columns
  -> determine which governed recipes can ground
  -> browse/search/filter suggested features
  -> inspect meaning, inputs, evidence and limitations
```

The hypothesis-driven generation journey remains owned by the feature-assist/contract plans:

```text
user hypothesis
  -> intent/use-case scope
  -> considered set
  -> validation/ranking
  -> human confirmation
```

Both journeys must consume the same template-discovery vocabulary and `FeatureSuggestionV2`
presentation semantics. The hypothesis journey must not read the suggestion search projection as
execution authority, and this plan must not create a second grounding, validation, join-safety or
feature-registration engine.

An explicit user action may hand a suggestion into the existing hypothesis/contract journey. That
handoff carries the selected suggestion/revision as context, then re-grounds against current facts;
it never treats the search projection as execution authority and never writes merely because a card
was viewed.

## Verified Current Baseline

The executable baseline reviewed for this plan is `origin/main fa9a20b0` on 2026-08-01. Task 0F must
re-resolve it because `main` moves quickly and the root checkout may be older or dirty.

The current implementation has these useful foundations:

- `templates.Template` already carries `family`, `intent`, legacy `use_cases`, `stage`,
  `eligibility`, `near_label`, `notes`, `additivity`, PIT intent and newer primary/supporting
  objectives.
- `suggestions.suggest_features_for_table` reuses `_template_candidates`; it does not invoke an LLM,
  create a hypothesis or write state.
- The suggestion payload already exposes the recipe, bound columns, entity grouping, validation
  status, requirements and binding quality.
- The route threads authenticated read scope and bounds the join neighbourhood and statement time.
- The table screen and column dossier reuse one `SuggestionCard` component.

The verified gaps are:

1. `_suggestion` drops the template's family, use cases, stage, eligibility, near-label and other
   discovery metadata before the API response.
2. `FeatureSuggestion` and `SuggestionCard` consequently show no category, domain, use-case,
   business-value, authority, provenance or detailed temporal context.
3. The UI calls `DESIGN_CHECKED` “clean & ready”, although the model explicitly says design checking
   is not proof of predictive usefulness or production readiness.
4. The screen presents deterministic recipe suggestions without a visible generation-source badge;
   users can reasonably mistake them for LLM-authored or registered features.
5. The feature template's current `family` is too fine-grained to be the only browse category. At
   the reviewed baseline, 157 templates have 121 distinct family values.
6. The existing 107 legacy use-case strings are not the canonical taxonomy. Only 14 templates have
   a canonical `primary_objective`; raw legacy strings cannot be exposed as governed use-case IDs.
7. No stable suggestion identity or revision identity exists; React and callers use the rendered
   name as an identity.
8. No global search/facet API or first-class Suggested Features navigation surface exists.
9. P4 recomputes suggestions on each table read and writes nothing. It is therefore not an
   ingestion-time feature catalog and cannot support catalog-wide search economically.
10. The current table call grounds the registry with hundreds to thousands of SQL statements. Its
    own measured hub fixture reports 6,284 statements at the cap. A global page cannot repeat that
    work for every table.
11. The column dossier fetches every suggestion for the table and filters by column in the browser;
    this is not a scalable or least-data server contract.
12. There is no explicit refresh/invalidation model for catalog changes, semantic corrections,
    recipe changes, join changes or sensitivity/read-scope changes.
13. Grounding is read-scope dependent: a broader caller can change which column wins a template
    binding. One maximum-scope projection followed only by result filtering is secure when correctly
    filtered, but it is not behaviorally equivalent or complete for narrower callers.
14. The general runtime queue cannot execute an arbitrary projection writer: its registered handler
    protocol receives a read-only connection and commits run-stream effects. A suggestion refresh
    row placed on that path would be retried/DLQ'd or violate the handler contract.
15. The original persistence sketch has per-suggestion current pointers but refreshes table scopes
    in batches. Without an immutable scope set and one atomic scope pointer, a crash can expose a
    half-old/half-new table result or let two anchor refreshes disagree about the same revision.
16. No cursor contract prevents a second page from mixing projection generations or read scopes,
    and no HTTP cache contract prevents a role change from reusing a more privileged response.
17. No retention, catalog-withdrawal, erasure/legal-hold or orphan-revision policy exists for the
    proposed persisted descriptions, column refs, search documents and immutable revisions.
18. No numeric freshness, latency, capacity, worker-fairness or recovery target is frozen. “Agreed
    threshold” is not an activation gate an operator can evaluate.

Task 0F must recompute the counts and symbol locations. Counts describe the reviewed baseline, not a
permanent product invariant.

## Scope and Dependencies

### In scope

- One controlled feature-discovery taxonomy, distinct from dataset domain and use case.
- `FeatureSuggestionV2`, stable suggestion identity and immutable revision identity.
- Additive wiring of existing recipe metadata into the backend contract and visible UI.
- Context from `SemanticContextBundleV1` and `DatasetSemanticProfileV1`, with authority retained.
- A resumable post-ingestion/current-state suggestion projection and dependency index.
- Global text search, facets, cursor pagination and honest projection-currentness states.
- A first-class Suggested Features UI plus richer table and column views.
- Server-side table/column filtering, authorization, freshness, performance and adversarial tests.
- Reuse of the discovery metadata by hypothesis-driven feature explanations/ranking.
- A user-initiated, re-grounded “use this suggestion” handoff into the existing feature-assist /
  contract journey; no new feature lifecycle is created here.

### Dependencies owned elsewhere

- `2026-08-01-semantic-enrichment-context-consumption.md` owns `SemanticContextBundleV1`, evidence
  authority, the shared business-domain/entity vocabularies when controlled IDs exist, relationship
  context, read-scoped context and feature-generation context consumption.
- `2026-08-01-catalog-profiles-source-temporal-crosswalk-rebased.md` owns catalog narrative,
  effective dataset profiles, data/authority/temporal roles and `dataset_profile_hash`.
- `2026-07-29-bridge-cardinality-and-link-trust-remediation.md` owns identifier-link lifecycle,
  directional realization and relationship execution safety.
- `2026-07-31-codegen-review-remediation.md` owns the known generated-execution correctness gaps.
  It may proceed in parallel with Releases A/B, but its correctness gate is a named predecessor of
  Release-C activation and any end-to-end claim that a handed-off suggestion is safely executable.
- Existing feature lifecycle/contract code owns acceptance, confirmation, registration,
  materialization and activation.

Release A may expose recipe-authored metadata before the semantic/profile plans land. Contextual
fields remain explicitly unavailable, not guessed. Release B's projection must import the frozen
shared contracts rather than copying them.

Before Task 1, the shared prerequisite slice must land `featuregen.canonical` and the canonical
evidence-axis types from the verified-interface ledger. Before Task 2 consumes semantic/profile
context, their public adapter contracts must exist in code. A plan document or duplicated local
dataclass is not an implementation dependency.

### Explicitly out of scope

- Accept, edit, dismiss, promote or register controls. The explicit handoff is a navigation/seed
  action into the existing lifecycle, not acceptance or registration.
- A new hypothesis, contract or feature lifecycle.
- A relevance percentage or uncalibrated LLM score.
- Embeddings/vector infrastructure before controlled taxonomy and PostgreSQL FTS are evaluated.
- A new LLM call merely to decorate every suggestion. A bounded, audited, one-time/batch proposal of
  template discovery mappings is allowed; its output remains `llm_proposed`.
- Physical profiling, OpenMetadata, source-selection policy or materialization execution.
- Treating human review, an LLM confidence score or a UI badge as join/cardinality authority.

## Non-Negotiable Rules

1. **Separate axes.** `feature_category`, `recipe_family`, `business_domain` and `use_case` are
   different fields. A single overloaded `category` string is forbidden.
2. **Suggestion-level semantics.** Metadata belongs to the recipe/suggestion. Columns expose a
   reverse “suggestions using this column” projection; category strings are not duplicated as
   independent column facts.
3. **Multiple values retain provenance.** A multi-source suggestion may have several domains. Never
   pick the first operand's domain as the feature's one true domain.
4. **Authority travels.** Recipe-authored, source-attested, human-authored, deterministic-derived
   and LLM-proposed values remain distinguishable in API, search and UI.
5. **LLM proposals are proposals.** They may add aliases, explanations or possible use cases, but
   never establish physical type, grain, uniqueness, cardinality, PIT safety or production
   readiness.
6. **Design checked is not production ready.** Render `DESIGN_CHECKED` as “design checked” and
   explain its limit. “Ready” is reserved for a real lifecycle/activation state owned elsewhere.
7. **Suggested is not registered.** Every card shows generation source and suggestion status. A
   suggestion never appears in the registered feature inventory until the existing governed flow
   creates it.
8. **No search-time grounding.** Global browse/search reads the projection. It never loops through
   tables and calls `_template_candidates` during an HTTP request.
9. **Read scope is current, not copied trust.** A projection may cache dependency visibility for
   indexing, but every read revalidates all current bound operands/endpoints. A hidden operand hides
   the whole suggestion and cannot leak through facets, counts, snippets or provenance.
10. **Staleness is visible.** A last-known revision may be shown as stale if still authorized; it
    cannot be labelled current. A first build in progress is `pending`, not an empty result.
11. **Identity is content-addressed and versioned.** New identities use the shared RFC-8785/JCS
    hasher. Existing field-evidence hashes are not rewritten. Provenance IDs, producer commits,
    timestamps and job state do not enter semantic identity merely because they are stored nearby.
12. **Unknown is valid.** `unclassified`, `not supplied` and `needs SME mapping` are explicit values;
    coverage targets must not force invented metadata.
13. **Taxonomy IDs are controlled.** Free-form/comma-separated domain and use-case values never
    become filter keys. Display labels resolve from versioned registries.
14. **Ranking does not weaken safety.** Text/category relevance may order equally visible results;
    it cannot turn a failed/unsafe candidate into an offered suggestion or rank an unsafe state as
    production-ready.
15. **One grounding truth.** Projection generation calls the existing grounding/gauntlet path. Any
    shortlist must be a proven superset optimization and must not reimplement validation. The
    gauntlet/planner emits the exact decision/dependency/path trace consumed by suggestion V2; the
    adapter never reconstructs it after the fact.
16. **Ground per visibility scope.** Projection scope is the canonical tuple returned by
    `allowed_classes(identity.role_claims)`, not a user ID or raw role list. Public and sensitive
    visibility profiles have separate immutable scope sets; filtering a maximum-scope winner is not
    a substitute for scope-correct grounding.
17. **Publish sets atomically.** A worker stages one complete table/read-scope set and CAS-publishes
    one pointer. Individual members never become current during a partial build.
18. **Critical drift fails closed immediately.** Missing/hidden operands, catalog withdrawal,
    relationship demotion, validation-fact drift, template disablement or authoritative projection
    lag suppresses readiness or the whole hit at read time; asynchronous refresh is not the first
    safety control.
19. **One consistent read snapshot.** Hits, totals, facets and cursor fingerprint are assembled
    under one `REPEATABLE READ` request transaction or one equivalent SQL snapshot.
20. **No privileged cache reuse.** Cursors, ETags and client caches are bound to the canonical
    visibility-scope key and tenant posture. Responses are private and role changes clear prior
    results.
21. **Dedicated worker contract.** Suggestion refresh uses a dedicated, fenced queue claimant and
    writer transaction registered in the production worker composition. It is excluded from the
    generic run-step claimant, just like the existing formula-shadow dedicated route.
22. **Single-tenant claim unless proved otherwise.** The current catalog schema is not consistently
    tenant-qualified. Task 0P must declare the release single-tenant or expand every suggestion
    scope/key/query to tenant-qualified catalog identity before any multi-tenant deployment claim.
23. **Relationship path is logical identity.** Two candidates with the same columns but different
    ordered relationship paths/directional realizations are not silently one candidate. The logical
    path enters `suggestion_id`; exact realization/dependency revisions enter the revision.
24. **Content and provenance are separate.** Byte-identical semantic/profile/catalog content does not
    churn a suggestion merely because it was re-uploaded, re-authored or produced by a new commit.
    Exact evidence/snapshot/event IDs remain queryable provenance and freshness pins.
25. **Composite grain is representable.** Grain is an ordered tuple of key operands; no V2 contract
    narrows the platform back to one grain column.
26. **Global identity converges across anchors.** Global search deduplicates by `suggestion_id` and
    returns it only when all authorized current anchor memberships agree on one revision. A mixed
    old/new multi-anchor state is partial/degraded and withheld, never shown twice.

## Contracts to Freeze Before Coding

### Discovery taxonomy and ownership

The axes have different owners:

- `feature_category` is local to this plan: a coarse way to browse computations such as ratio,
  trend, recency, frequency and concentration;
- `recipe_family` is the existing authored `Template.family`, but Task 1 must wrap its 121 current
  values in a stable ID/display registry rather than pretending arbitrary text is a controlled label;
- `business_domain` imports the shared semantic/ontology registry. If no controlled resolver exists,
  catalog/profile domain wording remains attributed search text and cannot become a facet;
- `use_case` imports selectable leaves from the existing use-case taxonomy;
- `entity` imports the shared semantic/entity vocabulary. Unresolved catalog entity wording remains
  attributed text and cannot become an entity facet.

No suggestion-local business-domain or entity ontology may compete with the Context Graph. Keep the
existing `Template.family` as `recipe_family` and add user-facing discovery mappings keyed by
`template_id`:

```python
@dataclass(frozen=True, slots=True)
class DiscoveryControlledAssignmentV1:
    controlled_id: str
    basis: str                               # template_authored | human | llm_proposed
    evidence: tuple[EvidenceAuthorityV1, ...]
    operational_influence: str | None        # discovery hint only in v1

@dataclass(frozen=True, slots=True)
class DiscoveryTextAssignmentV1:
    value: str
    basis: str
    evidence: tuple[EvidenceAuthorityV1, ...]
    operational_influence: str | None

@dataclass(frozen=True, slots=True)
class TemplateDiscoveryMetadataV1:
    template_id: str
    feature_category: DiscoveryControlledAssignmentV1 | None
    business_domains: tuple[DiscoveryControlledAssignmentV1, ...]
    canonical_use_cases: tuple[DiscoveryControlledAssignmentV1, ...]
    keywords: tuple[DiscoveryTextAssignmentV1, ...]
    business_value: DiscoveryTextAssignmentV1 | None
    disposition: str                         # complete | partial | unclassified | needs_sme
```

This registry adds discovery metadata only. It does not duplicate `intent`, `family`, `stage`,
`eligibility`, `near_label`, formula declarations or needs from `Template`. Import validation must
prove:

- every discovery entry names exactly one real template;
- there are no orphan/duplicate entries;
- category and domain IDs exist in their registries;
- canonical use cases are selectable leaves;
- keyword and prose bounds are enforced;
- an unmapped template is explicit and searchable as `unclassified`.

Do not silently convert the 107 legacy `Template.use_cases` strings into canonical use-case IDs.
Use them as migration evidence in a reviewed manifest; mappings become current only after they pass
the canonical registry validator. The validator proves referential and shape correctness; it does
not relabel an LLM proposal as human-authored. A bounded audited LLM pass may propose mappings for
all 157 templates, and those controlled-ID proposals may be used for discovery with per-value
`basis=llm_proposed`, visible evidence and no operational authority. Human edits improve the mapping
but are not a prerequisite for a non-operational suggestion card to exist. `disposition` summarizes
coverage only; it never replaces per-value provenance, so a human category and LLM-proposed use case
can coexist without one relabelling the other.

### Attributed discovery values

Reuse the shared evidence-authority vocabulary. Do not invent `verified: bool`:

```python
@dataclass(frozen=True, slots=True)
class AttributedLabelV1:
    id: str
    display_name: str
    basis: str                 # template_authored | catalog_resolved | human | llm_proposed
    evidence: tuple[EvidenceAuthorityV1, ...]
    operational_influence: str | None  # governed | hint | None; read, never inferred
    source_refs: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class AttributedTextV1:
    value: str
    basis: str
    evidence: tuple[EvidenceAuthorityV1, ...]
    operational_influence: str | None
    source_refs: tuple[str, ...]
```

Template-authored labels cite `recipe_revision_id` or `discovery_metadata_revision_id` as applicable.
Catalog/profile labels cite their evidence and `dataset_profile_hash`. An LLM-proposed label is usable
for discovery but visibly proposed.
`AttributedLabelV1` is only for controlled registry IDs. Existing free-text catalog `domain` or
entity wording uses `AttributedTextV1`: it may be displayed and text-searched, but it does not become
a facet ID until an explicit resolver maps it to the controlled registry with provenance.

One semantic value may carry source, LLM and human evidence simultaneously. The tuple above is the
same multi-axis evidence model as `SemanticValueV1`; choosing one “best authority” and retaining only
the other IDs is forbidden.

### Grounding and validation trace

The current `FeatureIdea` does not expose enough information to prove why a candidate was
`DESIGN_CHECKED` or which same-/cross-catalog path it used. Before building V2, extend the existing
grounding/gauntlet/planner result with one trace:

```python
class SuggestionDependencyClass(StrEnum):
    HARD_AVAILABILITY = "hard_availability"
    VALIDATION = "validation"
    SEMANTIC = "semantic"

@dataclass(frozen=True, slots=True)
class GroundingDependencyPinV1:
    dependency_class: SuggestionDependencyClass
    dependency_kind: str
    dependency_key: str
    content_hash: str
    current_revision_id: str | None          # provenance/freshness pin; not automatically content ID
    evidence: tuple[EvidenceAuthorityV1, ...]

@dataclass(frozen=True, slots=True)
class SuggestionRelationshipDependencyV1:
    relationship_ref: str
    relationship_kind: RelationshipKind
    from_ref: tuple[str, str]
    to_ref: tuple[str, str]
    realization_content_hash: str | None
    cardinality: str
    safety_status: str
    review_status: str
    evidence: tuple[EvidenceAuthorityV1, ...]

@dataclass(frozen=True, slots=True)
class GroundingDecisionTraceV1:
    candidate_key: str
    ordered_operand_roles: tuple[tuple[str, str, str], ...]
    ordered_relationship_path: tuple[SuggestionRelationshipDependencyV1, ...]
    validation_status: str
    requirements: tuple[RequirementV1, ...]
    dependency_pins: tuple[GroundingDependencyPinV1, ...]
    validation_rule_content_hashes: tuple[str, ...]  # exact rules evaluated, not a global version
    read_scope_rule_content_hashes: tuple[str, ...]
    trace_content_hash: str
```

The trace is produced at the decision points that already read type, grain, as-of, additivity,
unit/currency and relationship state. It is not reconstructed by `suggestions.py`, and the adapter
does not rerun path selection. A `DESIGN_CHECKED` result without a complete trace is invalid for V2.
`trace_content_hash` hashes the dependency content hashes, evaluated-rule hashes, decision and
logical path; it excludes `current_revision_id`, evidence occurrence IDs and build observations.
Those exact pins are persisted as scope/build provenance and used for currentness comparison.

One semantic relationship may expose zero or many directional realizations. The grounding trace
records the one selected ordered realization per traversed leg and its content hash; it does not
flatten the entire relationship into one arbitrary direction. Exact realization/revision/snapshot
IDs are provenance pins, not fields of the immutable semantic dependency above.

### Visibility scope and dependency classes

The projection must preserve the behavior of caller-scoped grounding without creating one copy per
user or functional role:

```python
@dataclass(frozen=True, slots=True)
class SuggestionReadScopeV1:
    schema_version: str
    tenant: str | None              # must be None in the declared single-tenant release
    allowed_classes: tuple[str, ...] # canonical sorted output of read_scope.allowed_classes
    scope_key: str                  # JCS hash of the fields above

@dataclass(frozen=True, slots=True)
class SuggestionDesiredTargetV1:
    catalog_source: str
    table_ref: str
    read_scope: SuggestionReadScopeV1
    desired_generation: int         # scheduling/fencing token, not semantic identity
    reason_codes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SuggestionBuildTargetV1:
    catalog_source: str
    table_ref: str
    read_scope: SuggestionReadScopeV1
    desired_generation: int
    table_grounding_state_hash: str
    overlay_checkpoint_and_head: tuple[int, int]
    semantic_context_hashes: tuple[str, ...]
    dataset_profile_hashes: tuple[str, ...]
    relationship_content_hashes: tuple[str, ...]
    recipe_registry_content_hash: str
    discovery_registry_content_hash: str
    grounding_policy_registry_hash: str       # rebuild/fencing input; not suggestion identity itself
    read_scope_policy_registry_hash: str
    producer_contract_version: str
    target_fingerprint: str
```

The scheduler does not fabricate `catalog_snapshot_ids`. The existing
`catalog_metadata_snapshot` is owned by feature-generation runs and ingestion never creates one; it
is not repurposed as a current catalog pointer. An invalidation hook only increments/upserts the
durable `desired_generation`. Under its repeatable-read worker transaction, the suggestion worker
builds `table_grounding_state_hash` from the exact sorted, read-scoped graph rows and current
dependency content used for the anchor plus admitted neighbourhood. It stores exact row/fact /
relationship revisions as scope-set dependency provenance, rechecks them before CAS publication and
publishes a no-op when the content target already equals current. A newer desired generation still
fences an older worker even when both eventually describe identical content.

`producer_contract_version` is an explicit version of the suggestion builder contract. A Git commit,
deployment ID, current time, ingestion-run ID or job attempt is build provenance and never part of
`target_fingerprint` merely because the implementation can observe it.

There are currently three independently grantable visibility classes, so the theoretical lattice is
bounded. Task 0F must recalculate and import-gate that bound from the actual read-scope registries.
Do not hardcode eight profiles in business logic. Build every canonical scope tuple reachable from
the deployment's role policy asynchronously after ingest/configuration change, with public scope
prioritized. A request for a valid scope whose target is not complete returns `pending` or explicitly
`partial`; the GET remains read-only and never enqueues work or relabels a maximum-scope result.

Dependency handling is set-based at read time:

- `HARD_AVAILABILITY`: removed/hidden operand or endpoint, withdrawn catalog, unavailable
  relationship, disabled template. The hit, count and facet contribution are withheld.
- `VALIDATION`: grain, as-of, type, unit, currency, additivity, relationship-safety or governing
  projection revision drift. The prior suggestion may remain discoverable as stale, but its
  `DESIGN_CHECKED`/readiness status is suppressed until rebuilt.
- `SEMANTIC`: description, category/domain/use-case label or non-operational profile context drift.
  The authorized prior revision may be shown with an explicit stale banner while refresh runs.

The refresh dependency store records class, dependency key and exact consumed revision. No reader
re-runs the grounding engine, but every list/detail query compares current authoritative pointers to
these pins before presenting status.

The mutable graph is not a historical snapshot store. A worker can build `SuggestionBuildTargetV1`
only while all listed current pointers still match; if any changes between batches, the old target
is superseded/aborted and rebuilt from the new target. It must never claim it reconstructed an old
graph snapshot from current rows.

### `FeatureSuggestionV2`

```python
@dataclass(frozen=True, slots=True)
class FeatureSuggestionV2:
    schema_version: str                    # feature-suggestion-v2
    suggestion_id: str                     # stable logical candidate identity
    suggestion_revision_id: str            # exact content/context revision
    generation_source: str                 # recipe | llm_freeform | user_defined

    template_id: str | None
    recipe_revision_id: str | None          # computational/authored recipe only
    discovery_metadata_revision_id: str | None
    validation_rule_content_hashes: tuple[str, ...]
    read_scope_rule_content_hashes: tuple[str, ...]
    name: str
    display_name: str
    business_interpretation: AttributedTextV1 | None
    business_value: AttributedTextV1 | None

    feature_category: AttributedLabelV1 | None
    discovery_disposition: str
    recipe_family: AttributedLabelV1 | None
    business_domains: tuple[AttributedLabelV1, ...]
    contextual_domain_terms: tuple[AttributedTextV1, ...]
    use_cases: tuple[AttributedLabelV1, ...]
    keywords: tuple[AttributedTextV1, ...]

    entity: AttributedLabelV1 | None
    contextual_entity_terms: tuple[AttributedTextV1, ...]
    grain_refs: tuple[tuple[str, str], ...]  # ordered composite key; empty only when unresolved
    operation_kind: str
    window: str | None
    time_ref: tuple[str, str] | None
    recipe: str
    recipe_parts: RecipePartsV2

    source_datasets: tuple[SuggestionSourceDatasetV1, ...]
    operands: tuple[SuggestionOperandV1, ...]
    relationship_dependencies: tuple[SuggestionRelationshipDependencyV1, ...]
    validation_status: str
    requirements: tuple[RequirementV1, ...]
    warnings: tuple[SuggestionWarningV1, ...]
    binding_quality: str

    semantic_context_hashes: tuple[str, ...]
    dataset_profile_hashes: tuple[str, ...]
    grounding_trace_content_hash: str

@dataclass(frozen=True, slots=True)
class SuggestionBuildProvenanceV1:
    scope_set_id: str | None
    metadata_snapshot_ids: tuple[str, ...]
    dependency_revision_ids: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    relationship_realization_revision_ids: tuple[str, ...]
    producer_commit: str | None
    refresh_id: str | None
    generated_at: datetime | None

@dataclass(frozen=True, slots=True)
class SuggestionProjectionStateV1:
    state: str                             # current | stale | pending | partial | failed | retired
    scope_set_id: str | None
    read_scope_key: str
    scope_epoch: int
    target_fingerprint: str
    current_fingerprint: str | None
    generated_at: datetime | None
    stale_reason: str | None
    omitted_counts: Mapping[str, int]

@dataclass(frozen=True, slots=True)
class FeatureSuggestionHitV2:
    suggestion: FeatureSuggestionV2
    projection: SuggestionProjectionStateV1 | None
    provenance: SuggestionBuildProvenanceV1

@dataclass(frozen=True, slots=True)
class SuggestionSummaryV2:
    suggested: int
    design_checked: int
    needs_external_validation: int
    groups: int

@dataclass(frozen=True, slots=True)
class SuggestionRejectionV2:
    template_id: str | None
    candidate_name: str
    code: str
    explanation: str

@dataclass(frozen=True, slots=True)
class SuggestionGroupV2:
    entity: AttributedLabelV1 | None
    contextual_entity_terms: tuple[AttributedTextV1, ...]
    grain_refs: tuple[tuple[str, str], ...]
    suggestion_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SuggestionCollectionContextV2:
    anchor_catalog_source: str | None
    anchor_table_ref: str | None
    anchor_column_ref: str | None
    table_known: bool | None
    summary: SuggestionSummaryV2
    groups: tuple[SuggestionGroupV2, ...]
    rejections: tuple[SuggestionRejectionV2, ...]
    neighbourhood: JoinNeighbourhoodV1 | None
    omitted_counts: Mapping[str, int]

@dataclass(frozen=True, slots=True)
class FeatureSuggestionPageV2:
    read_mode: str                         # on_demand | projected
    read_scope_key: str
    projection: SuggestionProjectionStateV1 | None
    collection: SuggestionCollectionContextV2
    hits: tuple[FeatureSuggestionHitV2, ...]
    facets: Mapping[str, tuple[FacetBucketV1, ...]]
    next_cursor: str | None
```

Projection state and timestamps are deliberately outside the immutable suggestion revision. The
same revision may move from current to stale while a refresh is pending without changing its
semantic identity or stored canonical bytes. Release-A table reads use `read_mode=on_demand` and no
projection state; Release-B global/table/column reads use `read_mode=projected`. The API may expose
the opaque scope key for diagnostics/cursor binding but never echoes raw role claims or treats the
key itself as authorization.

Evidence inside attributed semantic values is canonicalized to meaning-bearing producer/basis /
strength/lifecycle/content, not raw occurrence IDs. Adding a genuinely different human or source
attestation may change the revision; replaying the same evidence under a new event ID does not.
Exact event, fact, profile-snapshot and realization revision IDs live in
`SuggestionBuildProvenanceV1` and the scope dependency rows.

`SuggestionSummaryV2` renames the misleading V1 `clean_ready` count to `design_checked` and preserves
`suggested`, `needs_external_validation` and entity/group counts. `SuggestionRejectionV2` carries the
existing typed rejection code, safe explanation and template/candidate identity. The table route
retains `table_known`, no-concept, no-as-of and bounded-neighbourhood states. The explicit V1 adapter
can therefore reconstruct the existing wire shape byte-for-byte; it never invents data discarded by
V2. A global page has `anchor_* = None`, an empty rejection/group presentation when not requested and
uses hits/facets as its primary collection.

`SuggestionOperandV1` includes catalog source, logical/object ref, table ref, recipe role, measure /
grain / time / grouping classification, current visibility dependency and evidence refs.

`SuggestionSourceDatasetV1` groups operands by dataset and carries attributed data role (event fact,
snapshot fact, dimension/master, reference, crosswalk or unknown), authority role, temporal storage
model, primary entity, dataset-profile hash and profile status. These are discovery/explanation
fields; source/row selection and “latest master” execution policy remain owned by the profile plan.

`SuggestionWarningV1` uses closed codes such as `NEAR_LABEL`, `SENSITIVE_INPUT`,
`MISSING_TEMPORAL_EVIDENCE`, `MISSING_UNIT`, `MISSING_CURRENCY`, `RELATIONSHIP_UNCONFIRMED`,
`RELATIONSHIP_SAFETY_UNPROVEN`, `DIRECTIONAL_CARDINALITY_UNAVAILABLE` and `PROFILE_PROPOSED`.
Review/accountability and automatic execution safety are separate warnings: an unconfirmed link may
remain usable for exploration, while absent directional safety evidence remains explicit. Staleness
belongs to `SuggestionProjectionStateV1`, not the immutable suggestion's warning list. User-facing
prose is a rendering of the code plus bounded detail, not an alternative decision field.

### Identity rules

`suggestion_id` hashes the logical candidate independent of the screen from which it is viewed:

```text
template_id + canonical bound params + sorted bound (catalog_source, logical_ref, recipe_role)
+ resolved entity/ordered-grain/time identity
+ ordered logical relationship path (kind, direction, endpoints)
```

The requested anchor table is excluded so the same cross-table suggestion has one identity on its
table, column and global surfaces.

The canonical suggestion payload is also anchor-independent. Requested table, neighbourhood limits,
page truncation and search cursor belong to `FeatureSuggestionPageV2`; otherwise opening the same
candidate from two operand tables could create one logical ID with conflicting revision bytes.

`suggestion_revision_id` additionally hashes every meaning-bearing input:

```text
suggestion_id + recipe_revision_id + discovery_metadata_revision_id
+ referenced semantic-context content hashes + dataset-profile hashes
+ grounding trace content hash + relationship/dependency content hashes
+ exact evaluated validation/read-scope rule content hashes + validation result + contract version
```

Changing only display order does not change identity. Changing a template's meaning, binding,
domain/use-case mapping, context or safety result produces a new revision and atomically repoints the
current projection. Realization IDs/revisions, metadata snapshot IDs, evidence/event revision IDs,
refresh IDs, timestamps, producer commit and global registry versions are stored as provenance/currentness
pins but do not enter semantic identity by default. Referenced rule/label/recipe content revisions
enter; changing an unrelated policy rule, template or taxonomy label does not re-key every
suggestion. A registry-wide hash may schedule/fence a rebuild, but identical referenced content
reuses the existing suggestion revision.

### UI information contract

The following is a product contract, not optional decoration:

| Information | Compact card | Expanded detail | Filter/search |
| --- | --- | --- | --- |
| Display name and generation source | Yes | Yes | Text/source |
| Design status and stale/pending state | Yes | Explanation | Status |
| Feature category | Yes, primary badge | With definition | Category |
| Recipe family | Secondary/expand | With definition | Recipe family |
| Business domains | Yes, first values + count | All with provenance | Domain |
| Use cases | Yes, first values + count | All with provenance | Use case |
| What it measures | Yes | Full interpretation | Text |
| Why it is useful | Yes | Full business value | Text |
| Entity and governed grain | Yes | Evidence and source | Entity |
| Operation, window and time anchor | Yes | Full recipe/PIT intent | Window/operation |
| Source tables/columns | Count/summary | Every operand and role | Catalog/table/column |
| Source data/authority role and temporal model | Role summary | Per dataset with authority | Data role/temporal model |
| Requirements and limitations | Prominent warning count | Codes, operands, evidence | Readiness/warning |
| Authority/provenance | Source badge | Exact evidence/revision IDs | Authority |
| Projection currentness | Page/card state | Refresh identity/time | State |

## Projection and Refresh Architecture

P4 v1 remains a useful on-demand correctness oracle, but not the global read path. Release B adds
an immutable revision store plus current pointers and an asynchronous refresh worker.

Suggested physical model; Task 0P reserves the actual migration number after reconciling the shared
migration ledger:

- `feature_suggestion_revision`: immutable canonical V2 payload, identity columns, validation
  fields and FTS `search_doc`.
- `feature_suggestion_label`: normalized `(revision, axis, ordinal, label_id, basis,
  operational_influence)` rows for category, family, domain and use-case facets.
- `feature_suggestion_label_evidence`: normalized evidence rows keyed to one suggestion-label row so
  multi-evidence values are not collapsed into one authority blob. Do not rely on array unnest/JSON
  scans for facet counts or authority filters at production cardinality.
- `feature_suggestion_operand`: normalized catalog/table/column dependencies and recipe roles.
- `feature_suggestion_source_dataset`: normalized source table plus attributed data/authority role,
  temporal model, primary entity and profile hash for source-role/temporal facets.
- `feature_suggestion_dependency`: immutable content dependencies of one suggestion revision.
- `feature_suggestion_scope_dependency`: exact current fact/evidence/relationship revision IDs used
  by a scope set/member for freshness and targeted invalidation. This is where rebuild provenance
  lives so a provenance-only change can republish an unchanged semantic revision.
- `feature_suggestion_scope_set`: immutable build generation for one canonical
  `(tenant posture, catalog, table, read_scope_key, target_fingerprint)` scope.
- `feature_suggestion_scope_member`: complete suggestion/revision membership of that immutable set.
- `feature_suggestion_scope_current`: the one CAS-published pointer per table/read-scope scope; it
  points only to a completed set or to an explicit empty completed set.
- `feature_suggestion_scope_epoch`: monotonic epoch per tenant posture/read-scope key, bumped in the
  same transaction as any scope-current CAS. Global cursors/ETags bind this O(1) token instead of
  hashing every current set on every request.
- `feature_suggestion_refresh`: desired target, build cursor, lease/fence, attempt, error and
  disposition. Queue rows are wakeups; this table is durable refresh truth.
- `feature_suggestion_worker_heartbeat`: durable worker instance, handler/contract version and last
  seen time used by readiness; process-local counters cannot prove a compatible worker is alive.

There is deliberately no mutable global “latest revision per suggestion” chosen by wall-clock time.
Global search reads members of authorized current scope sets and groups by `suggestion_id`, not by
the `(suggestion_id, suggestion_revision_id)` pair. Every authorized current anchor membership for
that logical suggestion must resolve to the same `suggestion_revision_id`. If even two table scopes
claim different revisions—whether or not their table target fingerprints differ—the logical hit is
withheld, the page is partial/degraded, an alert is emitted and all affected anchors are scheduled for
repair. Identical memberships deduplicate to one hit. Detail lookup applies the same convergence rule.
Never choose the newest timestamp and never show old/new revisions as duplicate suggestions.

Use a GIN index for `search_doc` and B-tree indexes for current scope lookup, normalized labels,
status/entity/template, operand reverse lookup and dependency invalidation. Payload JSON is the
canonical read/audit snapshot, not the filter engine. Do not store comma-separated facets or query
meaning-bearing fields by arbitrary JSON paths.

Refresh is **post-ingestion**, not an unbounded synchronous upload stage:

1. A successful ingest/current-fact transaction atomically increments/upserts the affected
   table/read-scope `desired_generation` and enqueues a minimal idempotent wakeup. It does not claim
   to know an immutable catalog snapshot or final semantic target before the worker reads current
   state. A queue payload contains IDs/generation only, not catalog prose.
2. The dedicated suggestion claimant takes a fenced lease for one catalog/read-scope target. It is
   excluded from `runtime.queue.claim_one`, because the generic handler path is read-only and
   run-stream-specific.
3. For each table scope, a worker transaction first proves overlay/semantic/profile projections are
   caught up, captures the desired generation, computes the content-based build target from one
   `REPEATABLE READ` snapshot and records exact current revision pins separately.
4. It calls the existing grounding/gauntlet path with a proven-superset template shortlist and
   writes an unpublished immutable scope set, revisions, labels, operands and dependencies.
5. It rechecks content inputs, desired generation and lease fence, marks the set complete, and
   CAS-publishes one scope pointer. A lost lease, newer generation, timeout or crash cannot publish
   the staged set. A content-identical rebuild may reuse the immutable suggestion revision while
   publishing a new scope provenance set.
6. A continuation resumes from a durable table cursor. Published scopes are individually complete;
   the catalog/global page is honestly `partial` until all desired scopes finish.
7. Orphaned unpublished sets are safe to garbage-collect after the approved retention interval.
8. Completion records exact coverage for every supported canonical scope. A request for an unbuilt
   scope returns `pending`/`partial`; role-policy/configuration hooks and the reconciler, not GET,
   schedule missing scope work.

Semantic/profile/fact/relationship changes schedule only affected tables and their admitted join
neighbourhoods through the dependency index. A referenced template/mapping change schedules affected
templates; a registry schema or grounding-policy change schedules a full rebuild. Coalesce duplicate
requests by table/scope plus desired generation, and publish no-op on a content-identical target. A low-cost
periodic reconciler compares desired/current fingerprints and schedules missed work; it never
grounds templates itself. This is defense against a missed write hook, not permission to make hooks
best-effort.

The HTTP path never waits for a rebuild. It returns one of:

- `current`: exact projection is ready;
- `stale`: an authorized prior revision is shown with its age/reason;
- `pending`: first build has no result yet;
- `failed`: last build failed, with a safe retry/status message;
- `partial`: bounded refresh has completed only part of the requested scope.

## Minimum Production Envelope

Task 0P must record environment-specific SLOs and reference-catalog cardinalities in the verified
interface/runbook ledger. The following minimum controls are fixed by this plan; activation cannot
waive them with a feature flag:

- zero unauthorized hit, detail, total, facet, autocomplete, cache or cursor leakage;
- zero missing suggestions relative to the full-registry oracle for every supported read scope;
- list/detail HTTP paths perform no grounding and have fixed query-count ceilings independent of
  returned hit count;
- maximum page size 50, bounded facet/filter cardinality and maximum serialized response size 1 MiB;
- a configurable search `statement_timeout` no greater than 5 seconds at launch; timeout returns a
  typed 503 and a metric, not a partial success;
- one worker table-scope transaction is bounded to 30 seconds at launch, renews its lease before
  expiry and cannot publish after losing the fence;
- public-scope refresh is prioritized after every successful ingest; every supported sensitive scope is
  refreshed without starving the platform's run, timer, projection or formula workers;
- durable metrics expose oldest desired-target age, pending/running/failed/DLQ counts, current /
  partial/stale scope counts, refresh duration and search latency/error/timeout counts;
- `/health` or the deployment readiness check reports degraded when schema is behind, the dedicated
  worker handler is absent while the flag is enabled, or projection age/backlog exceeds the numeric
  SLO frozen in Task 0P;
- an operator runbook covers retry/DLQ replay, forced scope rebuild, taxonomy/template full rebuild,
  stale-set diagnosis, rollback and derived-data cleanup.

Task 0P may tighten latency/time limits after load testing. It may relax a proposed timing default
only through an explicit recorded production decision; correctness, authorization, atomicity and
boundedness gates are not relaxable.

## Release A — Semantic Contract and Visible UI

### Task 0F — Re-resolve baseline and freeze functional interfaces (read only)

**Inspect:** current `main`, P4 plan/implementation/tests, template/use-case/entity/domain registries,
suggestion route/API/UI, grounding/gauntlet outputs and the semantic/profile interface ledger.

- [ ] Record the exact baseline commit, dirty-worktree state and existing migration head/reservations.
- [ ] Recompute template count, distinct recipe families, legacy use-case strings, canonical
  objective coverage and unmapped templates.
- [ ] Confirm current `_template_candidates` and `FeatureIdea` signatures by symbol, not old line
  number or tuple-length assumptions.
- [ ] Freeze the contracts above in the shared verified-interface ledger, including canonical names,
  hash inputs, enum vocabularies and owner modules.
- [ ] Freeze `SuggestionCollectionContextV2`, including `table_known`, summary, groups, typed
  rejections and neighbourhood, and prove on paper that it can reconstruct every current V1 field.
- [ ] Freeze the split between `recipe_revision_id`, `discovery_metadata_revision_id`, semantic
  suggestion identity and build provenance. Explicitly exclude raw snapshot IDs and producer commits
  from semantic hashes.
- [ ] Freeze `GroundingDecisionTraceV1` at the actual gauntlet/path decision seam and name every
  producer change required; `suggestions.py` is not allowed to reconstruct it.
- [ ] Name the shared owners for feature category, recipe family, business domain, use case and entity;
  no second business-domain/entity ontology may be created by this plan.
- [ ] Freeze `SuggestionReadScopeV1` from the real `allowed_classes` registry and enumerate/import-
  gate every canonical scope tuple reachable from deployment role policy. Raw functional roles and
  user IDs must not create variants. Release A remains on-demand and creates no refresh work.
- [ ] Capture focused backend/frontend test baselines and the measured query counts for narrow,
  wide, joined and hub fixtures.
- [ ] Freeze Release-A API version negotiation, typed error bodies and response models in OpenAPI
  before frontend work begins. Cursor/ETag production details remain Task 0P.

**Exit:** no Release-A implementer needs to guess which taxonomy is canonical, what makes a
suggestion/revision stable, how a validation/path decision is traced, which read scope applies or how
V2 preserves all current table/column states. Production activation questions remain explicitly in
Task 0P and do not block Tasks 1-3.

### Task 0S — Land the shared implementation prerequisites

**Owned jointly with the semantic/profile interface plan; required before Task 1/2.**

- [ ] Add `src/featuregen/canonical.py` with the verified `jcs_sha256` and `contract_hash_v1`; make
  materialization delegate byte-identically without rewriting existing hashes.
- [ ] Add/import the canonical evidence producer, strength, lifecycle, `EvidenceAuthorityV1` and
  `SemanticValueV1` contracts. Do not copy them into the suggestion package.
- [ ] Import the canonical `RelationshipKind`, selected directional-realization/join-leg pin and
  dataset-profile content-hash contracts from their owner modules. `GroundingDecisionTraceV1` may
  wrap them for suggestion presentation but must not freeze a competing flat relationship model.
- [ ] Register/freeze the shared domain/entity resolvers. When absent, expose attributed text and no
  controlled facet instead of inventing an ID.
- [ ] Register every new serialized contract version with the real schema/adapter registries. A new
  context version may not be a byte alias of its predecessor, and an unregistered version must fail
  loudly in tests/startup rather than yielding an empty suggestion stage.
- [ ] Prove shared contract import/serialization tests and fail import on an owner/version mismatch.

**Exit:** Task 1 can hash mappings and Task 2 can serialize evidence without local substitutes.

### Task 0C — Correct the baseline readers this feature depends on

**Modify:** `column_authority.logical_ref_of`, suggestion table resolution and their focused tests.
If Task 0F finds either defect already fixed on the implementation baseline, retain the regression
test and record the actual owner instead of rewriting working code.

- [ ] Make `logical_ref_of` resolve object kind/schema through the canonical graph/object-identity
  reader. The two-part table ref `public.accounts` must remain a table ref, while a two-part legacy
  column spelling must be rejected or resolved with explicit kind; positional guessing may not turn
  a table field decision into a phantom `public.public.accounts` column decision.
- [ ] Make `suggestions._resolve_table` derive table existence from at least one caller-visible
  column (or the shared scoped asset resolver), using `visible_requires`, rather than treating the
  world-visible table node as proof. A caller who can see no column in a table must not learn its
  existence through `table_known`, rejections, neighbourhood metadata or counts.
- [ ] Pin both fixes with public/restricted/all-hidden, same table name in two schemas, table-anchor
  field-decision and column-ref fixtures. Reuse canonical object identity; do not solve either bug by
  introducing a second logical-ref parser or copying the old raw `sensitivity` predicate.

**Exit:** Release-A semantic/profile context attaches to the intended table/column and the table
suggestion surface enforces the same current read scope as its operands.

### Task 1 — Add and validate the discovery taxonomy

**Create:**

- `src/featuregen/overlay/upload/suggestion_taxonomy.py`
- `src/featuregen/overlay/upload/template_discovery.py`
- reviewed mapping fixture/manifest under `tests/featuregen/overlay/upload/fixtures/`

**Modify only where required:** `templates.py`, existing use-case taxonomy display helpers.

**Tests:** registry validation, full-template coverage, canonical-use-case and hash fixtures.

- [ ] Define the coarse feature-category registry with stable IDs, display names, descriptions and
  version fingerprints. Import the shared business-domain registry; do not create a local copy.
- [ ] Keep `Template.family` as `recipe_family`; do not rename or overload it. Add a stable
  recipe-family display registry with exact coverage of current authored family IDs.
- [ ] Create `TemplateDiscoveryMetadataV1` entries with exact template-ID coverage, per-assignment
  basis/evidence and explicit aggregate disposition. Do not use one template-level authority to
  relabel every category/domain/use-case/value.
- [ ] Review legacy `Template.use_cases` against selectable canonical leaves. Map only supported
  meanings; retain unmapped values in the audit manifest, not as filter IDs.
- [ ] Add attributed `business_value` and bounded keywords where authored or proposed. Implement an
  optional bounded audited batch that may propose category/domain/use-case/value mappings once per
  template revision; do not call an LLM per grounded suggestion and do not run the batch as part of
  tests, import or ordinary page reads.
- [ ] Give that optional batch a registered closed input/output schema. Extend the existing egress
  sanitization allowlist only for named bounded template fields, validate outputs exclusively
  against existing controlled IDs plus abstention, and store model/prompt/schema/producer provenance.
  `llm_proposed` is a value basis with `operational_influence=hint`, not a fabricated governed
  authority label.
- [ ] A blocked, malformed or abstaining item records a typed per-template result and the batch
  continues within its bounded failure budget. It must not turn one blocked item into an unexplained
  zero-output run or silently coerce free text into a controlled ID.
- [ ] Name a taxonomy owner and retain an append-only provenance/audit record for every mapping
  change. Record whether the change was authored, LLM-proposed or human-edited; do not fabricate a
  human review record for an unreviewed proposal. Treat business value, keywords and display labels
  as bounded plain text: Unicode-normalize for identity/search where specified, reject control
  characters/HTML, and never render registry text as raw HTML.
- [ ] Reuse the computational `recipe_revision_id` for `Template` meaning. Produce a separate
  `discovery_metadata_revision_id` from category/domain/use-case/value/keyword mapping content using
  the shared JCS hasher. A keyword edit must not alter recipe/formula identity.
- [ ] Fail import/tests on an unknown taxonomy ID, duplicate template, orphan entry, duplicate
  canonical use case, blank business value marked mapped, or mutable display-name-based identity.
- [ ] Mutation: replace one canonical ID with a plausible legacy string and prove the registry test
  dies.
- [ ] Must-survive: reorder sets/dictionaries/manifest rows and prove hashes remain identical.

**Exit:** every template has an explicit discovery disposition, and no uncontrolled string can
silently become a facet.

### Task 2A — Make the existing grounding path emit its decision trace

**Modify:** `feature_assist.py`, `contract/gate1.py`, `recipe_grounding_context.py`, same-catalog
join-path result types and cross-catalog plan-envelope adapters. Do not put this logic in
`suggestions.py`.

**Tests:** focused gauntlet/path trace tests plus existing full-registry and cross-catalog grounding
regressions.

- [ ] Add `GroundingDecisionTraceV1` to the existing candidate result without changing the
  accept/reject decision.
- [ ] Emit the exact type, grain, as-of, unit, currency, additivity, visibility and template pins at
  the point each check reads them.
- [ ] Retain the ordered same-catalog join path as well as the cross-catalog directional realization;
  no later consumer may rerun a path search to explain an already-selected candidate.
- [ ] Separate logical path identity from exact realization/dependency revision pins.
- [ ] Prove every `DESIGN_CHECKED` recipe candidate has a complete trace and every requirement names
  the exact dependency/operand that caused it.
- [ ] Differentially prove candidates, rejections and V1 response bytes are unchanged when the trace
  is ignored.
- [ ] Mutation: remove a type/grain/as-of/path dependency from the trace and prove the completeness
  test dies; reorder a semantically unordered evidence set and prove the trace hash survives.

**Exit:** V2 can explain and invalidate the existing decision without duplicating the gauntlet or
join planner.

### Task 2 — Build `FeatureSuggestionV2` and a byte-stable v1 adapter

**Create:**

- `src/featuregen/overlay/upload/suggestion_contract.py`
- `src/featuregen/overlay/upload/suggestion_identity.py`

**Modify:**

- `src/featuregen/overlay/upload/suggestions.py`
- `src/featuregen/api/routes/suggestions.py`
- semantic-context/profile adapters only through their owned public interfaces

**Tests:** extend `test_suggestions.py` and suggestion-route tests; add identity, authority and
contract serialization tests.

- [ ] Implement the frozen dataclasses, closed warning/state vocabularies and JCS serialization.
- [ ] Consume `GroundingDecisionTraceV1`; refuse a V2 `DESIGN_CHECKED` card when the trace is absent or
  incomplete. Do not infer validation/path dependencies from the final status or prose.
- [ ] The no-hypothesis producer emits `generation_source=recipe` only. The shared enum may describe
  hypothesis candidates, but this plan does not project LLM-freeform/user-defined ideas into global
  discovery without a separately designed lifecycle.
- [ ] Build one template-ID index once; never scan all templates separately for every suggestion.
- [ ] Assemble existing recipe metadata: category, family, intent, business value, canonical use
  cases, stage, eligibility, near-label, notes, additivity and PIT declaration.
- [ ] Add contextual domains, entity/grain/time, dataset profile and relationship dependencies from
  `SemanticContextBundleV1`/`DatasetSemanticProfileV1` when available. Preserve each label's basis;
  never collapse conflicting authored and proposed values into one unqualified string.
- [ ] Project per-source dataset data role, authority role, temporal model, primary entity and
  profile status into `SuggestionSourceDatasetV1`; never turn these descriptive fields into source /
  row selection or join safety.
- [ ] Keep free-text catalog domain/entity terms as attributed text and search-only context. Only an
  explicit controlled resolver may emit a domain/entity facet ID; LLM/source text is not converted
  by lowercasing or slugging.
- [ ] Derive warning codes from existing typed requirements/template declarations. Keep
  `RELATIONSHIP_UNCONFIRMED` separate from `RELATIONSHIP_SAFETY_UNPROVEN` and
  `DIRECTIONAL_CARDINALITY_UNAVAILABLE`; do not parse warning truth out of prose.
- [ ] Generate stable suggestion/revision IDs exactly as frozen. The same multi-table candidate must
  have one `suggestion_id` regardless of the requested table/column surface, while two different
  ordered relationship paths must not share one ID.
- [ ] Keep anchor table, neighbourhood limits and page truncation in the collection envelope, not
  the canonical suggestion revision. Differentially prove both operand-table views yield identical
  suggestion/revision bytes for the same binding.
- [ ] Bound domains, use cases, keywords, operands, relationships and evidence IDs; report omitted
  counts rather than silently truncating.
- [ ] Thread read scope into every context/dependency reader. If any bound operand or relationship
  endpoint is hidden, withhold the entire suggestion; never emit a partial suggestion with a new
  caller-specific identity.
- [ ] Derive `SuggestionReadScopeV1` from canonical allowed classes and prove on-demand grounding
  differs correctly across public and sensitive fixtures. Never pass a client-supplied scope key or
  role list to grounding.
- [ ] Classify and pin every dependency used by the validation result from the trace. A V2 card with
  `DESIGN_CHECKED` and no exact validation dependencies is invalid.
- [ ] Populate `SuggestionCollectionContextV2` with `table_known`, V2 summary names, entity groups,
  typed rejections and exact neighbourhood metadata. Global/on-demand modes use the same envelope.
- [ ] Keep P4 v1 response bytes available through an explicit `to_table_suggestions_v1(page)` adapter
  during migration. V1 must not reassemble metadata independently or lose rejection/neighbourhood
  states.
- [ ] Add `?contract_version=2` per-table negotiation to the existing route while leaving v1 as the
  default during Release A. Use explicit Pydantic response/error models and OpenAPI snapshots;
  reject unknown versions with 422, and require the frontend to request V2 deliberately.
- [ ] Move the multi-query V2 read to the existing `REPEATABLE READ` connection dependency and bind
  one statement timeout. Hits, counts, neighbourhood metadata and revision IDs must describe one
  snapshot; authoritative projection lag returns a typed unavailable state, not torn output.
- [ ] Prove `suggest_features_for_table` remains read-only in Release A.
- [ ] Add query-count tests to prevent per-card context/profile/evidence N+1 reads.
- [ ] Prove producer commit, refresh ID and byte-identical re-upload snapshot IDs do not alter
  `suggestion_revision_id`; changing referenced semantic content, discovery mapping, ordered path or
  validation result does.
- [ ] Mutation: collapse an evidence tuple, choose the first domain, exclude the logical relationship
  path from identity, put producer commit into the semantic hash, drop a collection rejection, or use
  rendered name as ID; every mutation must die.

**Exit:** one typed suggestion carries enough meaning for all UI surfaces without creating new
grounding or authority.

### Task 3 — Put the important information on existing table and column UI

**Modify:**

- `frontend/src/api.ts`
- `frontend/src/screens/SuggestedFeaturesScreen.tsx`
- `frontend/src/screens/SuggestedFeaturesScreen.test.tsx`
- `frontend/src/screens/AssetDetailScreen.tsx` and dossier tests
- `frontend/src/index.css`

- [ ] Add exact TypeScript types for attributed labels, warnings, operands, currentness and V2;
  avoid `Record<string, unknown>` for meaning-bearing fields.
- [ ] Request the explicit per-table V2 contract; do not infer V2 by testing whether optional fields
  happen to exist in a v1 response.
- [ ] Replace “clean & ready” with “design checked”. Add the concise explanation that predictive
  usefulness and production execution are not yet proven.
- [ ] Replace the stale “concepts are confirmed on the Semantics screen” copy. LLM/source proposals
  may be usable context without human confirmation; the UI names missing context rather than
  instructing the user to approve something that is not a gate.
- [ ] Show `recipe` as the generation source. Distinguish suggested from registered/governed
  features in the page copy and card badge.
- [ ] In the compact card show: name, state/status, primary category, first domain/use-case badges
  with overflow count, “what it measures”, “why useful”, entity/grain, window/time and warning count.
  Recipe family is secondary/expandable so 121 fine-grained families do not overwhelm the card.
- [ ] Summarize source dataset roles on the card (for example, transaction fact + customer
  dimension) and show each source's authority/temporal model/profile status in detail. Proposed and
  unknown profiles remain visibly proposed/unknown.
- [ ] Make missing temporal, unit, currency, grain, join and near-label warnings visible without
  opening the detail drawer. A warning may not be hidden behind color alone.
- [ ] Add an accessible expanded section/drawer showing all domains/use cases with provenance,
  recipe parts, every operand and role, relationship dependencies, requirements, eligibility,
  evidence basis, recipe/discovery/context/profile revisions and truncation notices.
- [ ] Show unmapped catalog domain/entity wording separately as “catalog terms” with authority. Do
  not badge it as a controlled business domain/use case merely because the text looks similar.
- [ ] Use human labels first and stable IDs in tooltips/details. Preserve technical refs for audit.
- [ ] Render all catalog/template prose as text, never `dangerouslySetInnerHTML`; bound tooltip and
  accessible-name length while leaving the complete sanitized value in the detail view.
- [ ] Render `unclassified`, `not supplied`, `proposed`, `stale` and `pending` honestly. Do not hide
  absent metadata by omitting the entire section.
- [ ] Preserve permission, unknown-table, no-concept, no-as-of and bounded-neighbourhood states.
- [ ] Keep the UI strictly read-only. No accept/edit/dismiss control or dead button.
- [ ] Ensure the reused column dossier card shows the same semantic and warning vocabulary.
- [ ] Clear suggestion requests/results when authenticated identity or visibility claims change.
  A React cache/query keyed only by URL is forbidden for this surface.
- [ ] Add keyboard, screen-reader, narrow-layout and long-label tests. Badge overflow must not create
  horizontal page overflow.

**Exit:** before global search exists, a user opening a table or column can understand and audit each
suggestion.

## Release B — Durable Projection and Global Discovery

### Task 0P — Freeze the production envelope (does not block Release A)

- [ ] Declare the deployment tenancy posture. Either enforce single-tenant-only in startup/config or
  add tenant to catalog identity, scope sets, refresh partitions, cursors and every query/index. An
  optional `IdentityEnvelope.tenant` alone is not isolation.
- [ ] Obtain the data-owner decision for suggestion payload classification, retention, catalog
  withdrawal, hard erasure and legal hold. Identify revisions pinned by considered-set/audit
  artifacts and therefore not disposable cache rows.
- [ ] Freeze the numeric SLO/capacity values required by the Minimum Production Envelope: reference
  cardinalities, search latency/error/timeout, projection age, initial build, queue/DLQ, database
  budget, backfill rate and recovery objective.
- [ ] Map every invalidation producer: ingest/graph rebuild, field decision, profile current pointer,
  grain/as-of/additivity/unit/currency, relationship lifecycle, sensitivity/read-scope and recipe /
  discovery deployment. Classify each as hard availability, validation or semantic drift.
- [ ] Freeze the dedicated worker seam: handler/version, queue exclusion, lease/fence/heartbeat,
  partition key, retry/permanent taxonomy, attempts, DLQ replay and production composition.
- [ ] Reconcile the active migration ledger and reserve the migration used by Task 4.
- [ ] Freeze cursor signing/version, ETag/cache behavior, global/detail error bodies and deployment
  readiness checks. These harden projected global reads; they are not prerequisites for Release-A
  on-demand V2.

**Exit:** Tasks 4-10 have explicit deployment, storage and operations constraints; Tasks 1-3 were not
held behind them.

### Task 4 — Add immutable suggestion revisions and current projection

**Create:** migration number reserved in Task 0P; `suggestion_store.py`; migration/store tests.

- [ ] Create the exact revision, normalized-label/label-evidence, source-dataset, operand, immutable
  content dependency, scope dependency/provenance, immutable scope-set/member, scope-current,
  scope-epoch, refresh-target and worker-heartbeat tables described above with foreign keys and
  closed checks.
- [ ] Scope-current keys include canonical catalog/table identity plus `read_scope_key` and the
  declared tenant posture. Raw role claims, subject IDs and mutable display names are forbidden in
  keys.
- [ ] Store structured scalar filter columns plus normalized label rows alongside canonical payload
  JSON. Payload is audit/read shape; query predicates and facets do not scan arbitrary JSON or
  comma-separated/unnested arrays.
- [ ] Use checked idempotent inserts: an existing revision/set/member ID with identical canonical
  bytes is a replay; the same ID with different bytes is an integrity error and cannot be ignored by
  `ON CONFLICT DO NOTHING`.
- [ ] Build `search_doc` from display name, interpretation, business value, controlled labels,
  keywords and authorized source names. Raw evidence prose and hidden operand text never enter it.
- [ ] Add one atomic `publish_completed_scope_set` CAS. A partial/failed set and a worker that lost
  its lease fence cannot repoint current. A completed empty set is distinct from pending/no set.
  The same transaction bumps the canonical visibility-scope epoch exactly once.
- [ ] Suggestions absent from a successfully published replacement disappear from that scope via
  set membership; immutable history remains until the approved compaction/erasure policy applies.
- [ ] Keep anchor membership in scope sets and logical identity in revisions so one cross-table
  suggestion can belong to several anchors without duplicate semantic identity.
- [ ] Add an integrity query/path grouping every authorized current anchor membership by
  `suggestion_id`. More than one revision is a conflict even when anchor target fingerprints differ;
  search/detail withhold it and no `ORDER BY created_at` chooses truth.
- [ ] Persist content dependencies on the immutable revision and exact current dependency revision
  pins on the scope set/member. Both are indexed for set-based hard/validation/semantic drift checks;
  do not deserialize every payload to determine freshness.
- [ ] Add rebuild/reset tools that recreate the projection from source truths; projection rows are
  never operational authority.
- [ ] Implement the Task-0P retention decision: garbage-collect orphan unpublished sets and
  unreferenced obsolete revisions, preserve revisions pinned by governed considered-set/audit
  artifacts, and handle catalog withdrawal/hard erasure/legal hold through an audited path. A
  withdrawn/erased catalog is removed from reads immediately even if physical cleanup is delayed.
- [ ] Make migration apply/replay/idempotency tests pass from empty, previous-head and current
  schemas. Do not assume a fixed migration number before Task 0P.
- [ ] Add indexes and `EXPLAIN` assertions for current lookup, column reverse lookup, dependency
  invalidation and FTS/facet paths.
- [ ] Mutation: a failed/partial refresh cannot repoint current; a revision row cannot be updated;
  deleting one anchor cannot retire the logical suggestion from another anchor; changing raw role
  order cannot create a new scope; two anchors with one logical ID/two revisions cannot both appear;
  a provenance-only republish can reuse identical revision bytes; an erased catalog cannot survive
  in FTS/facet results.

**Exit:** suggestions have auditable revisions, stable current pointers and indexed reverse
dependencies without becoming registered features.

### Task 5 — Build resumable refresh, invalidation and shortlist paths

**Create:**

- `src/featuregen/overlay/upload/suggestion_refresh.py`
- `src/featuregen/overlay/upload/suggestion_worker.py`
- focused worker/refresh tests

**Modify:** ingest completion and existing semantic/profile/fact/relationship write chokepoints;
runtime worker registration.

- [ ] In the same successful source transaction, increment/upsert the affected desired generation
  and call `enqueue_checked` with a table/scope/generation-specific message ID. Failed source writes
  enqueue nothing; queue insertion failure rolls back the source change rather than losing its
  invalidation. The hook does not fabricate a final content fingerprint.
- [ ] Add the suggestion handler name to the queue's dedicated-handler exclusion and implement a
  dedicated `claim/process/heartbeat/complete/fail` path with `lease_owner + lease_fence`, following
  the formula-shadow precedent. Register a separately bounded drain stage in `runtime.worker`; the
  generic `process_one` path must never claim it.
- [ ] Run suggestion work in its own configurable per-tick budget/priority so a very wide catalog
  cannot starve run steps, timers, overlay projections, formula work or another catalog. Add fair
  rotation/max-consecutive-scope tests.
- [ ] Process bounded table/read-scope targets with a durable cursor and fenced/idempotent ownership.
  Public scope is prioritized; every role-policy-reachable sensitive scope key is an independent
  target. A crash resumes, and two workers cannot publish different current sets for one target
  fingerprint.
- [ ] At table-build start, use `REPEATABLE READ`, assert overlay and all named authoritative
  projections are caught up, compute `table_grounding_state_hash`, capture exact current dependency
  pointers, and recheck them plus desired generation/lease fence immediately before publishing.
  Lag/drift defers the build; it never produces a stale `DESIGN_CHECKED` set.
- [ ] Build a deterministic template shortlist from the admitted table/join-neighbourhood concept
  inventory. The shortlist must be an over-approximation; final truth still comes from
  `_template_candidates` and the gauntlet.
- [ ] Differential-test shortlist output against the full registry over every existing grounding
  fixture plus randomized concept sets. One missing full-registry suggestion fails the optimization.
- [ ] Coalesce repeated changes by table/scope plus desired generation. A newer request supersedes an
  unstarted older request; an in-progress older result cannot become current after the newer target
  is known.
- [ ] Use transactional dependency hooks to refresh affected tables, every active visibility scope
  and admitted neighbour surfaces after semantic, profile, grain/as-of/unit/currency/additivity,
  relationship, template-enable or sensitivity changes. Also implement the periodic fingerprint
  reconciler that detects but does not excuse a missed hook.
- [ ] Apply hard/validation/semantic read guards immediately while refresh is pending. In particular,
  relationship demotion or validation-fact drift cannot leave an old card labelled design checked.
- [ ] A referenced recipe/discovery mapping change schedules affected anchors. A registry schema,
  grounding policy or read-scope schema change schedules a full rebuild without catalog re-upload or
  an LLM rerun.
- [ ] A read-scope registry/policy change recomputes the supported scope lattice, invalidates cursor
  epochs and schedules all newly required/re-meaninged scopes before V2 readiness can be healthy.
- [ ] If visibility changes while a job runs, revalidate before swap and refuse the stale result.
- [ ] Record pending/running/current/partial/failed state, attempts, batch counts, durations,
  statement counts and safe last-error codes.
- [ ] Record a bounded durable heartbeat containing worker/handler/contract version. Readiness is
  degraded when V2 is enabled and no compatible heartbeat is fresh; expire/prune abandoned worker
  identities without erasing refresh history.
- [ ] Separate deterministic permanent failures (invalid contract/taxonomy/payload) from retryable
  database timeout/serialization/lease faults, inherit the bounded attempt budget, emit DLQ metrics
  and provide an authorized operator replay/force-rebuild command. Never log suggestion prose,
  operand definitions, credentials or raw payloads in `last_error`.
- [ ] Put hard caps on tables, columns, templates, suggestions and wall time per worker transaction;
  continuation, not a larger transaction, handles the remainder. Renew the lease before expiry and
  prove a post-timeout abandoned worker cannot publish.
- [ ] Expose upload/result status as `queued`, `building`, `partial`, `current` or `failed` with the
  refresh ID. “Queued” must not be reported as “suggestions generated”; the UI can truthfully say
  suggestions are being prepared after ingestion.
- [ ] Add narrow, wide, one-hop hub and high-table-count benchmarks. Query/work growth must be tied
  to the bounded batch and shortlist, not total catalog size inside one transaction.
- [ ] Mutation: remove the dedicated-handler exclusion, cursor, idempotency key, new-target fence,
  scope key, projection-lag check, heartbeat fence, visibility recheck or omission retirement; each
  mutation must die.

**Exit:** upload and metadata changes eventually produce a current searchable projection without
blocking the request or running an unbounded job.

### Task 6 — Add global suggestion search and detail APIs

**Create:** `suggestion_search.py`; extend suggestion routes and route tests.

**Endpoints:**

```text
GET /feature-suggestions
GET /feature-suggestions/{suggestion_id}
GET /catalog/{source}/tables/{table}/suggestions       # compatibility adapter during rollout
```

The global list accepts bounded repeated filters:

```text
q, category, recipe_family, domain, use_case, entity,
source, table_ref, column_ref, data_role, temporal_model, operation, window, validation_status,
warning, authority, projection_state, limit, cursor
```

- [ ] Implement FTS and exact controlled facets. AND across facet groups, OR within one group;
  facet counts use exclude-own-facet semantics. Feed user text only through bound PostgreSQL
  `plainto_tsquery`/equivalent safe builders; never interpolate it into SQL or raw `to_tsquery`.
- [ ] Use keyset/cursor pagination over a total deterministic order. The opaque versioned cursor
  binds the normalized query/filter hash, sort boundary, `read_scope_key`, declared tenant posture
  and current visibility-scope epoch. A changed query/scope/projection returns typed 409
  `CURSOR_SNAPSHOT_CHANGED`; do not mix generations or silently restart at page one.
- [ ] Make cursors tamper-evident with a dedicated rotatable cursor-signing key (never reuse the
  security-audit HMAC key), while still re-running permission/read-scope checks on every page. Reject
  malformed, unknown-key-version or modified cursors without reflecting decoded internals.
- [ ] Rank text/control matches first, then validation/binding quality only as deterministic
  tie-breakers. Return ordinal score components for debugging, never a fabricated percentage.
- [ ] Return projection status, target/current fingerprint, generated time, stale reason and omitted
  counts with every page.
- [ ] Run hit, total and facet reads under the existing `REPEATABLE READ` dependency with the launch
  statement timeout. Group by `suggestion_id` before pagination/counting, emit one hit only when all
  authorized current anchor memberships converge on one revision, and withhold/repair every mixed
  revision conflict. Detail lookup uses the identical rule.
- [ ] `column_ref` and `table_ref` filters execute server-side through operand/anchor indexes. The
  browser does not fetch a whole table merely to find one column.
- [ ] Revalidate current visibility for every bound operand and relationship endpoint before hits,
  totals and facets using set-based predicates/joins, not one query per hit. Forbidden suggestions
  contribute to none of them.
- [ ] Apply the three dependency classes set-wise: hard drift excludes; validation drift removes
  design-checked/readiness claims and marks stale; semantic drift may serve authorized stale prose.
  Facet/status counts must describe exactly the cards returned under those rules.
- [ ] Keep `catalog:read` for this no-hypothesis catalog-derived discovery surface, matching P4 and
  preserving the data-owner curation journey. Record the derived-feature-content trade in the
  security review; do not grant `feature:read`, expose registered-feature content or broaden any
  role merely to reach this endpoint.
- [ ] Derive the read-scope key from the authenticated identity on every request. A valid supported
  scope with no completed target returns pending/partial; a scope outside the startup-validated
  policy lattice is a configuration 503, not an implicit public/max-scope fallback.
- [ ] Bound query length, repeated filter counts, facet buckets, limit and response bytes to the
  Minimum Production Envelope; reject duplicate/unknown taxonomy IDs and contradictory cursor plus
  sort parameters with typed 422 errors.
- [ ] Apply the deployment's authenticated read rate/admission limit and emit rejected/timeout
  metrics. If the application has no shared limiter, make the gateway limit a documented activation
  prerequisite rather than implementing an unreviewed in-memory per-process limiter here.
- [ ] Do not log raw search text, filter values, cursor bodies, suggestion prose or operand refs in
  metrics/errors. Log query/filter hashes and counts; configure access-log/query-string handling and
  `Referrer-Policy: no-referrer` so metadata searches are not copied into downstream logs/referrers.
- [ ] Add Pydantic request/response/error models and OpenAPI contract snapshots. List-level
  pending/partial/failed states return 200 with typed state; hidden and absent detail IDs both return
  404; statement timeout/unavailable storage returns typed 503; cursor drift returns 409.
- [ ] Add ETag/current-fingerprint handling bound to query, scope and projection. Send
  `Cache-Control: private, no-cache` and appropriate `Vary` headers; an ETag from one visibility
  scope or identity session can never validate another scope's response.
- [ ] Keep v1 route bytes through the V2 adapter for one migration window. It may read the projection
  when current, but cannot silently return stale rows as v1-current.
- [ ] Prove list/detail/table/column surfaces return the same suggestion/revision identity and
  semantic fields.
- [ ] Query-count/plan tests: global page, facets and detail have fixed ceilings independent of
  result count, use the intended indexes at reference cardinality and never call the grounding
  engine. Add concurrent refresh/page tests proving snapshot consistency.

**Exit:** users and UI can search the complete authorized suggestion projection with stable facets
and honest currentness.

### Task 7 — Add the first-class Suggested Features discovery UI

**Modify/Create:** application navigation/routes, `SuggestedFeaturesScreen`, reusable filters/card/
detail components, API client/types, CSS and focused tests.

- [ ] Add a visible **Suggested features** navigation destination. Preserve table deep links and
  route them into the same global screen with a table filter.
- [ ] Put free-text search and category/domain/use-case/entity/status filters at the top. Store
  filters and cursor-safe navigation state in the URL so a result view is shareable.
- [ ] Show summary total plus active-filter chips and projection currentness. Counts must reflect
  authorization and active filters.
- [ ] Show post-upload preparation state and refresh ID on the ingestion result/table entry point:
  queued/building is “suggestions are being prepared,” never “generated successfully.”
- [ ] Reuse the Release-A semantic card. Do not create a smaller global card that drops warnings or
  source identity.
- [ ] Add a detail drawer/page with the complete UI information contract and direct links to visible
  catalog/table/column assets.
- [ ] Group or sort by user selection without changing server truth. Defaults must be deterministic
  and must not claim a relevance percentage.
- [ ] Handle loading, no match, unclassified, first-build pending, partial, stale, failed, forbidden,
  cursor end and newer-projection-available states separately.
- [ ] When stale authorized data is displayed, show “last generated at” and why refresh is pending.
  Never silently mix current facet counts with stale cards.
- [ ] On identity/visibility-scope change, abort in-flight requests, discard cards/facets/cursors and
  restart from the first page. Do not retain a privileged detail drawer in browser history/state.
- [ ] Treat 409 cursor drift as “results changed; reload” with an explicit user action and focus
  restoration; do not append a new generation beneath old cards.
- [ ] Add accessible filter labels, keyboard focus restoration, live result-count announcement,
  expandable-card semantics and color-independent status/warning text.
- [ ] Test long domain/use-case labels, many badges, multi-source operands, hidden operands, stale
  data and mobile/narrow rendering.

**Exit:** suggested features are a searchable product surface, and every important semantic,
technical and safety fact is visible from the UI.

### Task 8 — Unify consumers and add the explicit feature-design handoff

**Modify:** existing table/column suggestion callers; feature-assist presentation/ranking adapters;
metadata snapshot integration where the hypothesis flow persists a considered set.

- [ ] Move the table screen to the V2 search API with exact table-anchor filtering after the
  projection is current. Preserve the old on-demand path behind the rollout fallback only.
- [ ] Move the column dossier to server-side `column_ref` lookup. Delete browser-side whole-table
  filtering after parity is proven.
- [ ] Make table, column and global cards consume the same TypeScript type/component and link to the
  same `suggestion_id`.
- [ ] Make hypothesis-driven candidate explanations import the template discovery registry and
  attributed label renderer. Do not re-map legacy tags in `feature_assist.py`.
- [ ] Use canonical use-case/domain/category metadata as deterministic ranking/explanation inputs
  only after the existing eligibility/safety gates. Unknown metadata is neutral, not a rejection.
- [ ] When a hypothesis considered-set snapshot persists a recipe candidate, pin
  `recipe_revision_id`, `discovery_metadata_revision_id` and semantic/profile hashes. It does not pin
  or trust a search-projection row as execution authority.
- [ ] Extend the existing considered-set dependency union through its owner rather than passing new
  fields through a permissive builder. Every canonical `item_hash` includes `item_kind`, serializers
  reject unknown kinds, and the comparator dispatches by kind rather than re-deriving every item as
  `column_field`. If the baseline lacks this typed scheme, land its versioned migration and one-time
  stored-hash re-baseline before writing suggestion-originated pins.
- [ ] Prove identical recipe bindings carry identical semantics across no-hypothesis discovery and
  hypothesis consideration; expected differences are hypothesis rationale/relevance only.
- [ ] Preserve P4 read-only behavior. Merely viewing or searching never creates intent, evidence,
  feature version or governance events.
- [ ] Add a visible **Use in feature design** action. The action submits a
  `SuggestionHandoffV1(suggestion_id, suggestion_revision_id, optional_user_objective)` only after an
  explicit click; page load and detail open remain GET-only.
- [ ] Resolve the selected current suggestion under the caller's current scope, rerun the existing
  grounding/gauntlet/path flow and compare logical identity plus revision. A disappeared/changed
  suggestion returns a typed drift result for user review; it is never silently substituted.
- [ ] After successful re-grounding, seed the existing feature-assist/contract journey with recipe,
  operands, path and semantic context. Existing intent/considered-set/confirmation ownership remains
  unchanged; this task creates no parallel acceptance or registration path.
- [ ] Prove a forged/stale suggestion ID, hidden operand, demoted relationship or changed path cannot
  enter a considered set from the search projection alone.

**Exit:** discovery is actionable, and discovery/hypothesis journeys share meaning without sharing
stale read-model authority or creating duplicate feature identities.

## Release C — Evaluation, Rollout and Operations

### Task 9 — Gold, integration, data and adversarial gates

**Create:** a versioned suggested-feature discovery gold corpus and mutation harness; extend focused
backend/frontend integration suites.

- [ ] Gold categories include balance/stock, activity/frequency, recency, trend, ratio,
  volatility, event/flag, lifecycle, network/concentration and explicit unclassified cases.
- [ ] Gold business domains/use cases cover single-domain, multi-domain, cross-catalog and proposed
  profile context without collapsing provenance.
- [ ] Contract parity tests prove the complete V2 collection preserves V1 `table_known`, summary,
  grouping, typed rejections and neighbourhood/truncation states, and that the explicit V1 adapter
  reproduces the frozen legacy response bytes.
- [ ] Grounding-trace gold cases cover accepted and rejected candidates, same-catalog and
  cross-catalog paths, composite grains and every hard/validation/semantic dependency class. A card
  may not claim `DESIGN_CHECKED` when the exact consumed trace is incomplete.
- [ ] Gold source profiles cover event fact, snapshot fact, dimension/master, reference, crosswalk
  and unknown/proposed roles; the UI exposes them while execution/source-selection behavior remains
  unchanged.
- [ ] Gold UI cards prove the user can see what, why, category, domain, use case, grain, window,
  sources, warnings, authority and currentness.
- [ ] Prove `DESIGN_CHECKED` never renders “production ready”, “validated on data” or an equivalent
  claim.
- [ ] Prove deterministic recipe suggestions never render as LLM-authored and LLM-proposed labels
  never render as governed. A structurally valid controlled-ID LLM proposal remains discoverable
  without human confirmation, but cannot contribute physical type, grain, PIT or relationship
  safety authority.
- [ ] Prove one attributed value can retain simultaneous source, LLM and human evidence without
  collapsing it into one winning authority, and that operational influence is read explicitly rather
  than inferred from evidence strength or review status.
- [ ] Data lifecycle tests: re-upload, column removal/rename, semantic correction, profile change,
  sensitivity increase, relationship demotion, referenced recipe/discovery revision, referenced
  taxonomy-label content change, refresh crash,
  duplicate refresh, stale-current race, catalog withdrawal, hard erasure, legal hold, compaction and
  referenced-revision retention. An unrelated recipe/taxonomy edit and provenance-only snapshot or
  producer-commit change do not churn unaffected semantic revisions.
- [ ] Security tests: one hidden operand/endpoint removes the entire suggestion from hit, detail,
  total, facet, autocomplete, source count and stale cache. Hidden and nonexistent IDs are
  indistinguishable; ETag/cursor/browser state cannot cross visibility scopes.
- [ ] Read-scope lattice tests: public and each supported sensitive class combination are grounded
  independently; a broader-scope winning binding never removes a valid narrower-scope alternative;
  prohibited remains unprojectable; raw role order does not change the scope key.
- [ ] Multi-source tests: identity is stable across anchors; ordered logical path changes alter
  logical identity; exact realization/scope revisions are pinned as revision/provenance; relationship
  demotion changes currentness/safety and schedules affected refresh.
- [ ] Atomicity/concurrency tests: unpublished/partial sets are invisible, an empty completed set is
  current, two workers race one scope, a lease expires mid-build, a newer target arrives before CAS,
  conflicting current revisions fail closed, and hits/totals/facets stay one RR snapshot.
- [ ] Worker-routing tests: the generic claimant never takes the suggestion handler; the dedicated
  stage is registered in production composition, heartbeats/fences writes, classifies retry versus
  permanent failure, reaches DLQ at the bounded budget and supports authorized replay.
- [ ] Performance gates: fixed HTTP query ceilings, no grounding on search, bounded worker batch,
  shortlist parity for every scope, indexed plans, response-byte caps, queue fairness and the
  numeric search/refresh/backfill envelope frozen in Task 0P.
- [ ] Migration/rebuild tests: empty database, upgrade, repeat apply, reset/rebuild, failed rebuild,
  old API adapter, typed considered-set pin re-baseline where required, and rollback with projection
  tables retained.
- [ ] Frontend tests: URL filters, keyboard/accessibility, overflow, loading/empty/stale/failure/no
  access, source links, server-side column filtering, identity/scope change, cursor-generation 409
  and post-upload queued/building wording.
- [ ] Mutation sentinels must die when authority, read scope, currentness, warning, taxonomy
  validation, identity input, dependency, cursor fence or UI status explanation is removed.
- [ ] Must-survive mutations: harmless ordering, UI-only formatting and replaying the same refresh do
  not churn logical identity/current pointers. A meaning-bearing taxonomy label/description change
  keeps `suggestion_id` but must create/publish a new revision.
- [ ] Handoff tests prove viewing/searching is GET-only; explicit **Use in feature design** re-grounds
  under the caller's current scope; a forged/stale ID, hidden operand, changed relationship path or
  demoted link cannot seed a considered set from projection bytes alone.

**Exit:** the feature is semantically useful, read-scoped, current, rebuildable and scalable under
the journeys it claims to support.

### Task 10 — Flagged rollout, backfill and rollback

Use one new switch: `FEATUREGEN_SUGGESTION_DISCOVERY_V2`. Do not create flags per field.

- [ ] Verify the named codegen-remediation predecessor is complete before Release-C activation. This
  does not block browsing Release-A cards during development, but an actionable production handoff
  cannot terminate in a known-broken generated execution path.
- [ ] Flag off preserves the current P4 v1 route and UI behavior byte-for-byte, except any separately
  approved correction of the misleading “clean & ready” label.
- [ ] Flag on with semantic/profile flags off still works from recipe-authored metadata and marks
  catalog context unavailable. It never guesses domains to satisfy the screen.
- [ ] Add validated cursor-key/configuration settings and startup checks. V2 search must not start
  with a missing cursor signer, invalid tenancy posture, unregistered dedicated handler or invalid
  semantic/profile flag combination. Projection publication also requires a non-`unset` producer
  commit/version stamp.
- [ ] Use expand/contract deployment: apply additive schema; deploy a worker containing the
  dedicated claimant with enqueue/read flags off; deploy backend writers/refresh emitters in shadow;
  verify no generic-queue DLQs; then backfill, enable API reads and finally deploy/enable UI. Old
  workers must be drained or proven unable to claim the new dedicated handler before emitters turn on.
- [ ] Backfill existing catalogs through durable refresh requests with rate/cap controls. Backfill
  does not require re-upload or live LLM calls. Build public plus every Task-0F-supported sensitive
  scope; enforce fairness, database budget and pause/resume controls.
- [ ] Shadow-compare on-demand P4 results with projected V2 suggestion identities/statuses for the
  fixed corpus and a bounded production sample. Investigating production data requires separate
  approval.
- [ ] Require zero missing suggestions relative to the full-registry correctness oracle; additive
  V2 metadata differences are expected and audited.
- [ ] Publish operational metrics: pending/failed/stale scopes, refresh age/duration, batch size,
  shortlist/full ratio, current suggestions, search latency/query count, hidden-result exclusions
  and adapter traffic.
- [ ] Wire durable backlog/oldest-target/scope-state and dedicated-handler readiness into
  `/metrics` and deployment readiness. In-process counters alone are insufficient after restart.
- [ ] Enable V2 reads only after every numeric Task-0P SLO/capacity gate passes, public/configured
  scope coverage is complete, DLQ/conflict counts are zero, projection age is within target and the
  UI/backend contract versions match.
- [ ] Rollback disables V2 reads and returns to the v1 adapter. Immutable projection history remains
  for diagnosis under the approved retention/access policy; emitters may be disabled independently,
  queued work can be drained safely, and rollback does not delete or rewrite user data.
- [ ] Exercise the runbook in staging: forced lease loss, DLQ replay, full taxonomy rebuild,
  catalog-withdrawal cleanup, cursor-generation conflict, worker rollback and database restore /
  projection rebuild. Record recovery times against the Task-0P objective.
- [ ] Retire the v1 HTTP shape and on-demand page grounding only after measured adapter traffic is
  zero for the agreed compatibility window and a separate removal change is approved.

**Exit:** activation and rollback are controlled, observable and do not require destructive data
operations.

## Execution Order

```text
Task 0F baseline/functional-interface freeze
  -> Task 0S shared canonical/evidence/domain/entity prerequisites
  -> Task 0C logical-ref and scoped-table baseline corrections
  -> Task 1 controlled discovery taxonomy
  -> Task 2A grounding/validation/path trace
  -> Task 2 FeatureSuggestionV2 + stable identity + complete v1 adapter
  -> Task 3 enriched existing UI
  -> Release-A functional checkpoint (use unavailable context honestly where shared context is absent)
  -> land/freeze shared SemanticContext/Profile Release-A adapters before projecting those fields
  -> Task 0P production envelope, migration reservation and worker/interface freeze
  -> Task 4 immutable revisions/scope sets + atomic scope pointers
  -> Task 5 dedicated fenced refresh + scope lattice + invalidation + shortlist parity
  -> Task 6 global search/detail APIs
  -> Task 7 global discovery UI
  -> Task 8 consumer unification + explicit re-grounded feature-design handoff
  -> Task 9 gold/security/data/performance gates
  -> Task 10 approved migration/backfill/activation
```

Tasks 1–3 can deliver visible user value without waiting for the durable projection or production
NFR decisions. Task 2 must not project semantic/profile fields until their real shared adapters exist;
it exposes them as unavailable in the interim. Tasks 4–8 must
land as one compatible release train: a global UI may not call per-table grounding in a loop as a
temporary substitute.

## Definition of Done

1. Every suggestion has stable logical and immutable revision identities; operational provenance,
   refresh IDs, timestamps and producer commits remain outside those identities.
2. Feature category, recipe family, business domain, use case and entity are separate axes with one
   named owner each; this plan does not create a competing domain/entity ontology.
3. Legacy use-case strings never silently become canonical facets.
4. The API/UI expose what the feature measures, why it is useful, entity/composite grain, temporal
   behavior, ordered relationship path, source dataset/data/authority roles, requirements, warnings,
   authority and currentness.
5. Recipe-authored, catalog-derived, human-authored and LLM-proposed values remain distinguishable;
   simultaneous evidence is retained rather than collapsed to one winning authority.
6. `DESIGN_CHECKED` is never presented as predictive validation or production readiness.
7. Suggested and registered features remain visibly and operationally separate.
8. Table, column, global and hypothesis surfaces reuse one semantic vocabulary and stable identity;
   the V2 collection preserves all V1 summary, grouping, rejection and neighbourhood states.
9. Global search performs no grounding work and has bounded, indexed, read-scoped facets and cursor
   pagination.
10. Successful ingest and semantic/profile/safety changes schedule idempotent bounded refreshes;
    stale, pending, partial and failed states are visible.
11. One hidden operand or relationship endpoint removes the entire suggestion from every read and
    aggregate count.
12. Projection revisions pin referenced recipe/discovery content, semantic/profile content and
    relationship/dependency content; exact current evidence/snapshot revisions remain separately
    queryable freshness provenance, and the projection is rebuildable from authoritative sources.
13. Shortlisting is differentially proven to preserve every suggestion produced by the full
    registry oracle.
14. Viewing/searching remains read-only and creates no hypothesis, evidence, decision, feature
    version or activation. Only an explicit handoff action may seed the existing journey, after
    current-scope re-grounding succeeds.
15. The global Suggested Features UI exposes all mandatory information in the UI information
    contract, including accessible warnings and provenance.
16. Grounding/projection is correct for every supported canonical visibility scope; broader-scope
    winners cannot suppress narrower-scope alternatives, and prohibited inputs are never projected.
17. One immutable completed table/read-scope set is CAS-published atomically; partial builds, lost
    leases, newer targets and conflicting current revisions cannot leak mixed truth. All authorized
    current anchors for one `suggestion_id` must converge on one revision before global display.
18. Hard and validation dependency drift changes read behavior immediately, before async refresh,
    and hits/totals/facets are one repeatable-read snapshot.
19. Cursors, ETags, response caches and frontend state are projection- and read-scope-bound; absent
    and unauthorized details are indistinguishable.
20. The dedicated fenced worker is excluded from the generic run-step path, fair to other runtime
    work, bounded, observable, retry/DLQ-safe and exercised by recovery tests.
21. Retention, compaction, catalog withdrawal, hard erasure, legal hold and referenced-revision
    preservation follow an approved audited policy; withdrawn data disappears from search
    immediately.
22. Numeric latency, freshness, capacity, backlog and recovery gates are frozen and passing;
    durable health/metrics and an exercised operator runbook exist before activation.
23. The deployment is either explicitly enforced single-tenant or every catalog/scope/query/key has
    passed a tenant-isolation review and adversarial tests. Optional identity metadata alone is not
    accepted as isolation.
24. Every V2 design decision carries the exact emitted grounding/validation/dependency trace; no
    presentation or projection adapter reverse-engineers a path or safety decision from prose.
25. Logical suggestion identity includes the ordered relationship path and ordered composite grain;
    exact realization revisions and build observations change revision/currentness, not logical
    identity by accident.
26. Structurally valid LLM-proposed discovery mappings can enrich and classify cards without human
    confirmation, while remaining visibly proposed and incapable of authorizing type, grain, PIT,
    cardinality or execution safety.

## Deferred

- Accept/edit/dismiss/promote workflows and durable user preference state.
- Calibrated predictive relevance or usefulness scores backed by outcome evaluation.
- Embedding/vector search after controlled FTS/facet evaluation demonstrates a measured recall gap.
- LLM-generated marketing prose and autonomous, unaudited taxonomy mutation. The bounded audited
  discovery-mapping proposal path in Task 1 is not deferred.
- Production materialization, source selection, row selection and execution safety changes.
- Multi-tenant enablement when Task 0P selects the single-tenant release posture. In that posture,
  startup/configuration must reject a multi-tenant claim until a separate tenant-qualified catalog
  migration and security review land.
