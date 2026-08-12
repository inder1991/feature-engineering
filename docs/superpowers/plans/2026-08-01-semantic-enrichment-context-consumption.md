# Semantic Enrichment Completion and Context Consumption — Implementation Plan

> **Status:** READY FOR RELEASE-A IMPLEMENTATION ONLY AFTER JOINT TASKS 0 AND 0.5.
> This is a functionality-first plan. It does not add OpenMetadata ingestion, a graph database,
> scheduled profiling, security redesign, or general NFR work.
> The binding cross-plan interfaces live in
> `docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md`; implementation must stop
> if this plan drifts from that ledger.

## Goal

Turn the semantic metadata the platform already generates into one correct, explainable and
consumable context flow:

```text
uploaded technical metadata / glossary
        -> source facts + deterministic semantics
        -> context-aware LLM enrichment and criticism
        -> resolved evidence-backed semantic context
        -> Context Graph V1 read model
        -> feature generation + data-agent planning + column UI
```

The implementation must correct the remaining ontology/replay defects, give the LLM more relevant
table and glossary context, generate useful uncertainty and ontology-gap outputs, and make every
consumer read the same current interpretation. It must not pretend that semantic inference proves
physical type, value overlap, uniqueness, cardinality, or source-data quality.

## Current Baseline and Ownership

The original draft used `origin/main 17a29f02`. The adversarial re-review verified the executable
baseline at `origin/main fa9a20b0` (2026-08-01). It
already contains:

- source capability profiles for technical CSV and FTR glossary ingestion;
- Pass-A concept/definition/domain/synonym/unit enrichment;
- Pass-B table synthesis;
- the refute-oriented concept critic and structured-result store;
- source glossary evidence, full-dossier summary drafting and display-axis projection;
- the column dossier and Entity Map v0;
- namespace-grouped bridge candidate enumeration;
- attested physical-type observation code;
- the rich feature-context implementation behind `FEATUREGEN_FEATURE_CONTEXT`.

Task 0 must re-resolve the baseline because parallel work continues on `main`. Do not implement
against the older root checkout (`b66b33dd`) or assume the task checkboxes in the 2026-07-31 plan
accurately describe what has landed.

This plan owns:

- `SemanticContextBundleV1` and its upload/store builders;
- concept-vocabulary identity and LLM replay correctness;
- issuer-aware namespace context and semantic role clarity; it does **not** re-key historical
  `counterparty_id` evidence or bridge facts in this slice;
- richer LLM input, targeted semantic adjudication and ontology-gap suggestions;
- one Context Graph V1 read model over existing stores;
- feature-generation and data-agent consumption of that context;
- a gated activation and re-enrichment run.

This plan does **not** own:

- relationship cardinality and directional bridge-realization contracts — the bridge remediation
  programme owns those;
- Hive/ODS profiling execution — the data-powered ontology/data-agent programme owns that next
  slice, and will append observations to this plan's context contract;
- OpenMetadata — explicitly out of current scope;
- a new graph database — Context Graph V1 is a typed read model over current PostgreSQL stores;
- human approval as an availability gate for proposed semantics or identifier links. Authority and
  review status remain visible; deterministic execution safety remains a separate concern.

This plan is one release train with two other plans; it is not independently complete:

- `2026-08-01-catalog-profiles-source-temporal-crosswalk-rebased.md` owns catalog narrative,
  effective dataset profiles, uploader profile declarations, source/temporal decisions and
  mapping-table crosswalks;
- `2026-07-29-bridge-cardinality-and-link-trust-remediation.md` remains the only owner of direct
  identifier-link lifecycle, directional realization, governed-grain/measurement cardinality and
  production relationship safety.
- `2026-07-31-codegen-review-remediation.md` Tasks 1–26 are a hard predecessor of Task 8's
  generated-project acceptance and all Release-C execution. That plan does not wire the orphaned
  materialization chain; a separately reviewed compile→render→submit→publish wiring slice is also
  required before any production materialization claim.

The integration boundary is mandatory:

- joint Tasks 0 and 0.5 freeze first; profile Tasks 1–3 may then proceed alongside semantic Tasks
  1–2 only where the shared file-ownership ledger shows no collision;
- semantic Tasks 3–4 and profile Pass-B Task 4 land atomically because replay identity
  must hash the exact payload that actually egresses;
- semantic Tasks 5–6 then consume that exact stored/replayed payload for adjudication and
  projection consistency;
- semantic Tasks 7-9 and profile Task 5 share one Context Graph, feature adapter and data-agent
  adapter;
- profile Release B supplies request-specific source and row decisions to feature/materialization
  and analysis consumers without placing those decisions inside the reusable column semantic
  bundle;
- profile Release C projects crosswalk definitions/executions through the relationship context
  defined here, while bridge/crosswalk modules remain authoritative for safety.

No task may create a parallel profile resolver, relationship safety model, observation lifecycle
reader, feature snapshot, replay store or execution interpreter to make these plans appear
independent.

The old plan's gated Task 6 re-upload must not run before this plan's pre-live gates. This plan's
Task 11 replaces its activation order; it does not authorize a catalog upload by itself.

## Non-Negotiable Functional Rules

1. Source-attested and human-authored values keep the precedence already encoded in
   `field_policies.py` and `field_resolution.py`. This plan does not invent another precedence
   engine.
2. LLM output is proposed evidence. It may be consumed for exploration and feature generation with
   its authority visible, but it cannot silently become a physical fact.
3. An LLM never attests physical type, uniqueness, overlap, cardinality, arrival SLA or data quality.
4. New semantic reasoning treats `counterparty` as a party role and keeps identifier scheme/entity
   separate. The legacy `counterparty_id` token remains byte-stable in persisted evidence and
   bridge keys until a separate reconciliation explicitly migrates it.
5. A concept namespace names an identifier **scheme**. Cross-catalog equality also needs issuer
   context; scheme alone is not proof.
6. Human confirmation is not required to propose or explore a semantic link. Production execution
   remains keyed to deterministic directional realization evidence, not LLM confidence or a review
   click.
7. Re-enrichment supersedes only prior system/LLM outputs. It never rewrites source-attested or human
   decisions.
8. Every consumer sees one resolved current value plus its evidence, not a privately recomputed
   interpretation.
9. `unclassified`, `uncertain` and `missing context` are valid results. Coverage targets must never
   force a guess.
10. No deploy, catalog upload/re-upload, live LLM run, or Hive/ODS connection without explicit user
    approval for that exact action.
11. Data role, authority role and temporal model are dataset/table facts. They are never inherited
    from a catalog-wide default because one catalog may contain facts, dimensions, mappings and
    replicas together.
12. “Latest master” never means newest upload or newest catalog scan. Source choice uses an explicit
    serving policy or load-bearing authority; row choice then uses the governed temporal policy and
    requested cutoff.
13. Free text and LLM suggestions may explain a dependency, grain or key mapping, but cannot become
    an executable key/join fact. They enter the existing typed grain, bridge or crosswalk proposal
    pipeline with authority and evidence visible.
14. Context exposed to a caller is read-scoped. A hidden column, table or endpoint cannot enter an
    LLM purpose adapter, Context Graph response, search document or data-agent candidate set.
15. New content identities use the shared RFC-8785/JCS contract hasher and canonical names from the
    verified-interfaces ledger; existing evidence hashes are not rewritten.

## Contracts to Freeze Before Coding

### `SemanticContextBundleV1`

One immutable in-memory contract, not a new source-of-truth table:

```python
@dataclass(frozen=True, slots=True)
class EvidenceAuthorityV1:
    producer: EvidenceProducer
    strength: AssertionStrength
    lifecycle: EvidenceLifecycle
    producer_ref: str | None
    evidence_id: str | None

@dataclass(frozen=True, slots=True)
class SemanticValueV1:
    field_name: str
    value: object | None
    evidence: tuple[EvidenceAuthorityV1, ...]
    resolution_status: str
    operational_influence: str | None  # governed | hint | None; read, never inferred

@dataclass(frozen=True, slots=True)
class IdentifierNamespaceV1:
    scheme: str                    # cif | swift_bic | iban | ...
    issuer_scope: str | None       # hdfc | swift | unresolved
    basis: str                     # catalog_scope | global_scheme | unresolved

@dataclass(frozen=True, slots=True)
class NeighbourColumnV1:
    object_ref: str
    column_name: str
    concept: str | None
    party_role: str | None

class RelationshipKind(StrEnum):
    DIRECT_EQUALITY = "direct_equality"
    CROSSWALK = "crosswalk"
    TRANSFORMED = "transformed"
    SEMANTIC_ONLY = "semantic_only"

@dataclass(frozen=True, slots=True)
class DirectionalRelationshipContextV1:
    from_table_ref: str
    to_table_ref: str
    realization_id: str
    realization_revision_id: str
    scope_id: str
    from_binding_revision_id: str
    to_binding_revision_id: str
    dependency_snapshot_id: str
    lifecycle: str
    review_status: str
    safety_status: str
    cardinality: str
    sandbox_eligible: bool
    production_eligible: bool
    evidence_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class RelationshipContextV1:
    relationship_ref: str
    kind: RelationshipKind
    left_endpoint: IdentifierEndpointV1
    right_endpoint: IdentifierEndpointV1
    availability: str
    folded_status: str | None
    ranking_strength: int
    directional_realizations: tuple[DirectionalRelationshipContextV1, ...]
    crosswalk_definition_revision_id: str | None
    crosswalk_execution_revision_id: str | None

@dataclass(frozen=True, slots=True)
class ObservationContextV1:
    # Faithful serialized RelationshipObservationV2 plus current-pointer identity.
    observation_revision_id: str
    realization_revision_id: str
    plan_hash: str
    scope_id: str
    left: EndpointTupleObservationV2
    right: EndpointTupleObservationV2
    matched_left_distinct: int
    unmatched_left_distinct: int
    matched_right_distinct: int
    unmatched_right_distinct: int
    left_orphan_rows: int
    right_orphan_rows: int
    joined_row_count: int
    max_right_matches_per_left_row: int
    max_left_matches_per_right_row: int
    normalization_ids: tuple[str, ...]
    predicate_ids: tuple[str, ...]
    left_source_snapshot_id: str
    right_source_snapshot_id: str
    snapshot_or_as_of: str | None
    execution_principal: str
    method: str
    row_coverage: str
    complete: bool
    observed_at: str
    failures: tuple[str, ...]
    producer: str
    strength: str

@dataclass(frozen=True, slots=True)
class SemanticContextBundleV1:
    contract_version: int
    catalog_source: str
    object_ref: str
    table_ref: str
    source_semantics: tuple[SemanticValueV1, ...]
    resolved_semantics: tuple[SemanticValueV1, ...]
    concept_path: tuple[str, ...]  # selected concept followed by its is_a ancestors
    identifier_namespace: IdentifierNamespaceV1 | None
    table_context: tuple[SemanticValueV1, ...]
    catalog_profile_revision_id: str | None
    dataset_profile_hash: str | None
    neighbouring_columns: tuple[NeighbourColumnV1, ...]
    relationship_context: tuple[RelationshipContextV1, ...]
    observation_context: tuple[ObservationContextV1, ...]  # empty until the Hive/ODS slice
    missing_context: tuple[str, ...]
    content_hash: str
```

`RelationshipKind` is defined here once and imported by the profile plan. Evidence authority uses
the existing producer/strength/lifecycle enums without a lossy projection. `operational_influence`
is the separate `governed|hint` read result; it is never derived from producer identity.
`content_hash` uses the shared versioned JCS contract hasher over the canonical payload, excluding
the hash field, wall-clock/job state and live observations not explicitly pinned.

`RelationshipContextV1` mirrors the shipped link-plus-zero-to-many-directional-realizations model;
it never chooses one direction from a symmetric link. `ObservationContextV1` is a faithful
side-preserving projection of `RelationshipObservationV2`, not a newly invented observation
lifecycle. Crosswalk definition/execution IDs are kind-dependent and remain `None` until Release C.
An observation is applicable only to its exact realization, scope, left/right bindings, predicates
and snapshots. Sampled evidence may refute uniqueness but cannot establish exact uniqueness.

The assembled effective dataset profile and its hash extend `table_context` when profile Release A
is enabled. Request-specific `DatasetSourceSelectionV1` and `DatasetRowSelectionV1` do **not** enter
this reusable bundle; the analysis/feature plan and metadata snapshot carry those separately.

There are two builders for the same value contract:

- `bundle_from_upload(...)` builds it from canonical rows + glossary sidecars + the table cohort
  before `build_graph` exists; relationship/observation fields are absent unless a pre-existing
  read-scoped store value is explicitly supplied;
- `bundle_from_store(...)` builds it from `graph_node`, field evidence, operational facts, available
  links and structured results after ingestion.

The builders receive caller roles/read scope and use canonical logical refs distinct from flattened
graph refs. They may have different evidence available, but equivalent shared facts must serialize
identically. Store reads are batched per table/source; a per-field N×M query loop is forbidden.
Each LLM task receives a purpose-filtered projection of this bundle; there is one fact model, not one
unbounded prompt.

### `SemanticAdjudicationV2`

Used only for ambiguous/high-impact columns, with a closed output shape:

```python
@dataclass(frozen=True, slots=True)
class OntologyGapSuggestionV1:
    proposed_label: str
    parent_concept: str | None
    definition: str
    aliases: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SemanticAdjudicationV2:
    selected_concept: str          # registry member or "unclassified"
    alternatives: tuple[str, ...]  # at most 3 registry members
    confidence_band: str           # high | medium | low; explanatory, never authority
    reason_codes: tuple[str, ...]  # closed vocabulary
    missing_context: tuple[str, ...]
    ontology_gap: OntologyGapSuggestionV1 | None
```

`ontology_gap`, when present, may suggest a label, parent concept, definition and aliases. It is
stored as a structured result and shown for review; it never edits `CONCEPT_REGISTRY` automatically.

### Context Graph V1

Context Graph V1 is assembled at read time. Its first version supports these node/edge types:

```text
dataset -contains-> column
column -described_by-> glossary/source evidence
column -classified_as-> concept -is_a-> concept
column -belongs_to-> domain
column -identifies-> entity
column -has_role-> party role
column -linked_to-> cross-catalog identifier column
column -derives-> feature -governed_by-> contract
column/question -has_gap-> ontology/analysis gap
```

Each inferred edge carries status, authority, evidence IDs and currentness. Ownership, external
usage, conversations and data products remain absent until a real source supplies them; the API
must say `not_supplied`, not manufacture empty objects.

---

## Task 0: Freeze the Real Baseline — Read Only

**Files:**

- Inspect: `docs/superpowers/plans/2026-07-31-ingestion-richness-and-correctness-remediation.md`
- Inspect: `docs/superpowers/plans/2026-07-29-bridge-cardinality-and-link-trust-remediation.md`
- Inspect: `docs/superpowers/plans/2026-07-31-codegen-review-remediation.md`
- Inspect: `docs/superpowers/plans/2026-08-01-catalog-profiles-source-temporal-crosswalk-rebased.md`
- Inspect: `docs/architecture/2026-08-01-plan-review-semantic-context-and-catalog-profiles.md`
- Inspect: `docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md`
- Inspect: `src/featuregen/overlay/upload/{enrich.py,enrich_llm.py,concepts.py,ingest.py}`
- Inspect: `src/featuregen/overlay/upload/attest/concept_critic.py`
- Inspect: `src/featuregen/overlay/upload/{feature_assist.py,asset_detail.py,entity_map.py}`
- Inspect: `src/featuregen/analysis/retrieval.py`
- Modify: this plan's executed-baseline record only

- [ ] Record the implementation SHA from the worktree that will execute the plan and prove it
  contains `origin/main 17a29f02` or a reviewed successor.
- [ ] Record all uncommitted files and exclude unrelated user changes from the implementation.
- [ ] Run the focused enrichment/context suites before editing:

  ```bash
  uv run pytest -q \
    tests/featuregen/overlay/upload/test_enrich.py \
    tests/featuregen/overlay/upload/test_enrich_llm.py \
    tests/featuregen/overlay/upload/attest/test_concept_critic.py \
    tests/featuregen/overlay/upload/test_concept_critic_acceptance.py \
    tests/featuregen/overlay/upload/test_feature_menu_enrichment.py \
    tests/featuregen/overlay/upload/test_asset_detail_dossier.py \
    tests/featuregen/overlay/upload/test_entity_map.py \
    tests/featuregen/analysis/test_retrieval.py
  ```

- [ ] Inspect `scripts/verify_catalog_richness.sql` and record its known revision/count defects. Do
  not edit or run it as part of the read-only task. Its repair belongs to Task 0.6; a live audit is a
  separate deployment/data approval.
- [ ] Record live configuration flags read-only. In particular, record
  `FEATUREGEN_FEATURE_CONTEXT` and all Pass-A/Pass-B feature switches.
- [ ] Record the exact bridge/profile task heads this implementation depends on and reconcile their
  shared file lists. Do not start parallel edits to `table_synth.py`, `enrich_llm.py`, Context Graph,
  feature context or metadata snapshots.
- [ ] Do not deploy or re-upload anything.

**Exit:** a reproducible baseline, zero mutation, and no ambiguity about which existing tasks have
already landed.

## Task 0.5: Reconcile Verified Interfaces — No Product Code

**Modify only:**

- `docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md`
- this plan and the rebased profile plan if the recorded baseline changes any contract

- [ ] Record the full applied migration filename+checksum set, not only the numeric head, and
  confirm reservations 1044–1049 remain collision-free.
- [ ] Freeze the shared JCS hasher, `RelationshipKind`, `dataset_profile_hash`, evidence authority,
  faithful relationship/observation projections, `JoinLegPinV1` ownership and the six Release-B
  snapshot kinds.
- [ ] Freeze the real task-completion gates: codegen remediation plus a separate materialization
  wiring slice before generated-project/Release-C execution.
- [ ] Freeze the rollout flag matrix and the distinction between discoverable cross-catalog UI and
  signed-gated production execution.
- [ ] Re-run file/symbol searches for every shared owner. A symbol with a new production owner on a
  reviewed successor of main is reconciled here; it is not duplicated in implementation.

**Exit:** both active plans import one written contract ledger and no implementer must choose a
migration number, hash scheme, authority vocabulary or relationship shape from memory.

## Task 0.6: Repair Shared Prerequisite Seams

**Modify:**

- create `src/featuregen/canonical.py`; make `materialize/canonical.py` delegate byte-identically
- `src/featuregen/overlay/upload/column_authority.py`
- `src/featuregen/overlay/upload/graph.py`
- `src/featuregen/overlay/upload/search.py` and scoped table readers
- `src/featuregen/overlay/upload/concepts.py`
- `src/featuregen/overlay/upload/feature_metadata_snapshot.py`
- `scripts/verify_catalog_richness.sql`

**Tests:** focused table-ref, read-scope, graph reprojection, concept-cycle and snapshot-kind suites.

- [ ] Fix `logical_ref_of` so `public.orders` is a table ref, not the phantom column
  `public.public.orders`; test table and column anchors under public and non-public schemas.
- [ ] Add one canonical table-visibility predicate based on at least one visible column. Apply it to
  table search/profile/context reads; a world-visible table graph node is not an authorization
  anchor.
- [ ] Make durable table field evidence reproject after every graph rebuild even when Pass B is
  disabled.
- [ ] Fix registry `is_a` cycle detection before exposing `concept_path`.
- [ ] Add and pin shared `jcs_sha256`/`contract_hash_v1` RFC-8785 vectors. Existing
  `materialize_hash` delegates only to `jcs_sha256` and returns identical digests for its unchanged
  payloads; existing field-evidence hashes are untouched.
- [ ] Make metadata snapshots reject unknown kinds, include `item_kind` in identity/dedup and use a
  kind-aware current comparator as specified in the shared interface ledger.
- [ ] Make `verify_catalog_richness.sql` revision-tolerant and use non-blank counts. Do not run it
  against a deployment without separate approval.
- [ ] Required mutations: two-part table ref treated as a column; hidden table text searchable;
  Pass-B-off re-upload erases table projection; unknown snapshot kind silently skipped; comparator
  rebuilds a non-column item as `column_field`.

**Exit:** the shared profile/context surfaces do not write phantom evidence, leak table existence,
lose durable projections or create permanently drifting snapshot pins.

## Task 1: Build `SemanticContextBundleV1`

**Files:**

- Create: `src/featuregen/overlay/upload/semantic_context.py`
- Modify: `src/featuregen/overlay/upload/concepts.py` only for a public ancestor helper
- Modify: shared scoped/batched readers used by the bundle; do not loop over scalar readers
- Reuse: `field_resolution.py`, `column_authority.py`, `operational_facts.py`,
  `available_identifier_links`, bridge/crosswalk lifecycle readers, `structured_results.py`
- Test: `tests/featuregen/overlay/upload/test_semantic_context.py`

- [ ] Write failing tests for stable ordering, JCS canonical hashing, source-vs-resolved separation,
  complete producer/strength/lifecycle attribution, read scope, missing-context codes, bounded
  neighbouring columns and a query-count ceiling.
- [ ] Add cycle-safe `concept_path(name)` returning the selected concept and every `is_a` ancestor.
  Registry validation should make cycles impossible; the reader still refuses a corrupt cycle.
- [ ] Implement the pure dataclasses and shared versioned JCS serialization. Define closed
  `missing_context`/profile-state vocabularies here before either plan consumes them; arbitrary
  free-text reasons are forbidden.
- [ ] Implement `bundle_from_upload` using only `CanonicalRow`, glossary metadata and rows from the
  same table. It must not query a half-built graph.
- [ ] Implement `bundle_from_store` using read-scoped, batched authoritative readers. It must not
  infer authority from a non-null `graph_node` display value or run readiness/relationship queries
  once per field.
- [ ] Resolve relationship context through `available_identifier_links()` and the shared current
  realization/assessment readers. Never read a leftover bridge projection or re-fold lifecycle
  events locally. A discoverable semantic link with no directional realization remains explicitly
  non-executable.
- [ ] Project observations only when their direction, applicability scope, binding revisions and
  relationship revisions match the relationship being described. Stale, expired, sampled or
  wrong-direction observations remain visible as history but cannot support current safety.
- [ ] Add the profile Release-A extension point: profile fields join `table_context`, while
  `catalog_profile_revision_id` and `dataset_profile_hash` identify the exact assembled profile.
  Flag-off leaves both absent and preserves the pre-profile bundle bytes.
- [ ] Treat `unclassified` as an explicit sentinel with `concept_path=()`; never look it up as a
  registry member.
- [ ] Add purpose adapters:
  `for_concept_enrichment`, `for_critic`, `for_summary`, `for_feature_generation`,
  `for_analysis_planning`. Each is bounded and runs through the Task-4 purpose-specific egress
  allowlist/sanitization seam; the current closed whitelist is not sufficient.
- [ ] Prove equivalent shared facts have identical bytes from both builders by comparing canonical
  logical refs; upload-side real schema and store-side flattened graph refs must not fork identity.
- [ ] Mutation tests: swap relationship direction, drop scope, reuse an observation across bindings,
  treat a sampled uniqueness observation as proof, read a stale projection, or omit profile identity;
  each mutation must die.

**Exit:** one typed context object can represent what the platform knows before and after graph
persistence without becoming a second truth store.

## Task 2: Correct Entity, Role, Alias and Issuer Semantics

**Files:**

- Modify: `src/featuregen/overlay/upload/concepts.py`
- Modify: `src/featuregen/overlay/upload/party_vocab.py`
- Modify: existing concept alias/namespace helpers; do not create a third namespace truth surface
- Modify: `src/featuregen/overlay/upload/bridge_candidates.py`
- Modify: `src/featuregen/overlay/upload/attest/bridge_grounding.py`
- Modify: `src/featuregen/overlay/upload/entity_map.py`
- Modify: `src/featuregen/api/routes/data_sources.py` with the focused
  `GET/PUT /data-sources/catalogs/{catalog_source}/semantic-scope` configuration surface
- Migration: reserved `1045` in the shared interface ledger
- Tests: registry, namespace-pairing, Entity Map and route tests

- [ ] Extend the existing `_LEGACY_ALIASES`/canonical-concept seam; do not add a parallel alias map.
- [ ] Preserve the current `counterparty_id` registry member and its persisted entity semantics in
  this slice. Changing `entity_link` would re-key bridge fact keys and orphan confirmation/history.
  Record a separate reconciliation deferral if product owners later choose that migration.
- [ ] Keep `party_role=counterparty` as the row role and teach prompts/retrieval to distinguish role
  from identifier scheme. Do not change bridge endpoint identity merely to improve wording.
- [ ] Keep `Concept.namespace` as the identifier **scheme**.
- [ ] Add a one-time catalog semantic scope with `issuer_scope`; never hardcode HDFC, CIB or FTR in
  application logic or migrations.
- [ ] Produce `IdentifierNamespaceV1(scheme, issuer_scope, basis)` through the existing concept
  namespace plus governed `identifier_namespace`/bridge-grounding path. Issuer logic must be folded
  into `assess_grounded_identifier_link`; a standalone helper may not bypass its hard conflicts.
- [ ] Pairing truth table:

  | Left/right | Candidate behaviour |
  | --- | --- |
  | same scheme + same known issuer | normal candidate |
  | same scheme + different known issuer | refuse candidate |
  | same scheme + either issuer unresolved | advisory candidate with `issuer_unresolved`; never claim equality proof |
  | different scheme | refuse candidate |

- [ ] Human confirmation must not be added to this truth table. Directional realization safety
  remains the execution gate.
- [ ] Backfill/rebuild only the new issuer-scope projection. No historical concept/evidence/bridge
  fact is rewritten; test candidate/fact identities byte-for-byte before and after.
- [ ] Mutation tests: BIC↔CIF dies; same-scheme/different-role survives; same-scheme/different-issuer
  dies; removing `party_role` does not change pairing; missing issuer never appears as verified; a
  registry edit cannot re-key an existing bridge silently.

**Exit:** namespace matches are issuer-aware without creating a third grounding path or re-keying
existing governed relationship history.

## Task 3: Freeze Replay Identity for the Exact Task-4 Payload

**Files:**

- Modify: `src/featuregen/overlay/upload/enrich.py`
- Modify: `src/featuregen/overlay/upload/attest/concept_critic.py`
- Modify only as required: `src/featuregen/overlay/upload/enrich_llm.py`
- Tests: `test_enrich_versioning.py`, `test_concept_critic.py`, new fingerprint property tests

- [ ] Define each purpose adapter's exact rendered field list first. Fingerprint precisely those
  meaning-bearing fields—no field omitted, and no unrendered registry field included.
- [ ] Sort the registry/vocabulary payload before hashing so declaration order does not invalidate
  replay.
- [ ] Make the critic fingerprint cover every registry field used in its payload, including
  description and entity link.
- [ ] Hash the exact purpose-filtered context bytes, prompt ID, prompt version, schema version and registry
  fingerprint. Do not hash live observations, current time, job status or unrelated display fields.
- [ ] Property test: mutating any rendered meaning-bearing field invalidates replay.
- [ ] Property test: mutating a field not rendered to that task does not invalidate replay.
- [ ] Must-survive no-op: reorder equivalent dictionaries/sets/registry declarations and prove canonical identity is
  unchanged.
- [ ] Land the cache/fingerprint bump in the same change as Task 4's payload/schema changes. Do not
  trigger a full paid re-enrichment with a fingerprint for fields that have not yet egressed.

**Exit:** correcting meaning that an LLM actually saw re-evaluates affected system proposals;
unrendered fields and harmless serialization changes do not churn identity.

## Task 4: Feed Full, Relevant Context into Existing LLM Enrichment

**Files:**

- Modify: `src/featuregen/overlay/upload/enrich.py`
- Modify: `src/featuregen/overlay/upload/enrich_llm.py`
- Modify: `src/featuregen/overlay/upload/enrich_batch.py` only for typed bundle transport
- Modify: `src/featuregen/overlay/upload/attest/concept_critic.py`
- Modify: `src/featuregen/overlay/upload/table_synth.py`
- Modify: schema registry/prompt-version registration in `enrich_llm.py` for every affected task
- Tests: existing enrichment/egress/batch/deadline suites plus `test_semantic_context.py`

- [ ] Replace each task's private metadata assembly with the relevant
  `SemanticContextBundleV1` purpose adapter.
- [ ] Extend the closed egress adapters in `enrich_llm.py` for every new top-level/nested key. Each
  key is explicitly prose-sanitized, bounded structural data or a real
  producer/strength/lifecycle evidence wrapper. Unknown keys fail closed; known tuple/list values
  are structurally validated rather than rejected merely for being non-string.
- [ ] Register a real feature-context v4 input contract and a real Pass-B/table-synthesis v3 output
  schema before any caller requests those versions. Keep `additionalProperties: false` and add
  prompt/schema registration tests that fail if `schema_for(id, version)` returns `None`.
- [ ] The concept classifier and critic receive: source definition, business term, declared type,
  domain, synonyms/related terms, BIAN/FIBO paths, process path, table role, primary entity and a
  bounded roster of neighbouring column names/concepts/roles.
- [ ] Definition, domain, synonym and summary enrichment receive current stronger facts and must not
  paraphrase a source definition as if it were new evidence.
- [ ] Table synthesis receives the same per-column semantics and full compact roster; retain the
  existing wide-table two-phase mechanism.
- [ ] Jointly with profile Release-A Task 4, accept bounded advisory suggestions for business
  context, data/authority/temporal role, grain or primary-key candidates and dataset dependencies.
  Every dependency/key suggestion names existing dataset/column refs and evidence refs. Route it to
  the existing typed grain, direct-link or crosswalk proposal path; free-text rationale is display
  context only and never an executable relationship.
- [ ] The catalog/profile upload surface may carry descriptive dependency context and typed
  dependency suggestions. Validate the full payload before writes, preserve uploader authority and
  do not convert a catalog-level statement into a table-level operational default.
- [ ] The critic runs for every high-impact proposal (identifier, monetary, temporal, label/leakage),
  not only identifiers. Name and test the deterministic signal for each contradiction; missing
  operational type is `unknown`, not evidence of a conflict.
- [ ] Preserve maximum call/wall-clock bounds, audit records and PII sanitization. Do not claim the
  same item throughput after increasing bytes: re-budget tokens, record truncation/not-attempted per
  purpose, and prove a blocked item creates an explicit disposition plus stage detail rather than a
  successful zero-output stage.
- [ ] Keep cache identity local to the affected column plus bounded table context. A sibling's
  unrelated reclassification must not invalidate every column in a wide table.
- [ ] Persist accepted Pass-B output through the existing `structured_result` store with exact input
  hash and `llm_call` provenance so deterministic replay evaluation is possible; do not create a
  second replay store.
- [ ] Golden payload tests for:
  `counter_party_bic`, `counter_party_cif_id`, `actual_counter_party_amt`, `tran_crncy`,
  `pstd_date`, `sol_desc`. `pstd_date` is a synthetic fixture unless the Task-0 baseline proves a
  governed repo witness; the suite never depends on a live catalog row.
- [ ] Golden egress tests cover every added key, the `LLM/PROPOSED/ACTIVE` evidence shape, nested
  concept paths and relationship tuples, raw-sample blocking, redaction audit and batch disposition.

**Exit:** existing LLM stages reason over the platform's full relevant metadata without adding a
second enrichment pipeline or sending the entire catalog.

## Task 5: Add Targeted Semantic Adjudication and Ontology-Gap Suggestions

**Files:**

- Create: `src/featuregen/overlay/upload/semantic_adjudication.py`
- Create: `src/featuregen/overlay/upload/semantic_gap.py`
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` for versioned schemas
- Modify: `src/featuregen/overlay/upload/ingest.py` for one honest stage
- Reuse: `src/featuregen/overlay/upload/structured_results.py`
- Modify: `src/featuregen/overlay/upload/stage_report.py` only for detail rendering; do not extend
  its closed stage-state vocabulary with business outcomes
- Migration: reserved `1046`; immutable structured results remain authoritative, with only a
  subject/current CAS pointer for ontology-gap discovery
- Tests: `tests/featuregen/overlay/upload/test_semantic_adjudication.py`

- [ ] Define closed reason codes, confidence bands and missing-context codes. Model confidence is
  explanation only; it never changes evidence authority.
- [ ] Select adjudication targets deterministically:
  `unclassified`, critic-uncertain/refuted, deterministic shape conflict, or source-vs-LLM conflict.
  Do not invent a model-confidence threshold and do not call the adjudicator for every already-clear
  column.
- [ ] Ask for one registry concept, at most three alternatives, reason codes, missing context and an
  optional ontology-gap suggestion.
- [ ] Validate every concept/parent against the registry. Free-text rationale remains audit colour;
  no free-text becomes a concept, join key or feature operand.
- [ ] Persist the validated output as `structured_result` type `semantic_adjudication`, version 2,
  linked to its `llm_call` provenance.
- [ ] A selected correction writes normal `llm/proposed` field evidence through the existing
  proposal path. An ontology gap writes no concept evidence and does not modify the registry; it
  updates a subject/current pointer to the immutable structured result so the UI can find and
  supersede it without scanning JSON payloads.
- [ ] Record `selected|unchanged|unclassified|gap_suggested|invalid|not_attempted` as typed stage
  detail counts. They are outcomes, not new values in the closed ingestion stage-state column.
- [ ] Make the subject/current pointer append-only + CAS and retries idempotent under concurrency;
  use the current-pointer pattern rather than SELECT-then-INSERT.
- [ ] Add an asset-detail read for the alternatives, reasons, missing context and gap suggestion.

**Exit:** the LLM contributes richer, reviewable semantic judgement and identifies vocabulary gaps
without inventing operational facts or automatically growing the ontology.

## Task 6: Close Supersession and Projection Consistency

**Files:**

- Modify: `enrich.py` (including `_write_llm_field_evidence` and every LLM field writer),
  `field_resolution.py`, `ingest.py`
- Modify: `asset_detail.py`, `graph.py` only where projection repair is required
- Tests: enrichment acceptance, asset-detail provenance, graph/search consistency

- [ ] Re-deriving a different LLM value marks the prior LLM result non-current using existing
  producer-scoped staleness/value-diff mechanisms.
- [ ] Re-deriving the same input/value/producer preserves the current evidence ID for every LLM
  field. Replace the current unconditional supersede-and-rewrite path; do not merely special-case
  the critic.
- [ ] A system correction must never create a human-rejection event.
- [ ] Source-attested and human values survive an LLM rerun byte-for-byte.
- [ ] After resolution, evidence, `graph_node`, search, asset detail, Entity Map and semantic context
  all return the same current value and evidence ID.
- [ ] Detect and report projection lag; do not serve a newer semantic value with an older context
  hash as though they were consistent.
- [ ] Mutation: disable stale-value retirement and prove the consistency suite dies.

**Exit:** old decoy semantics remain in history but cannot remain current or leak into consumers.

## Task 7: Expose Context Graph V1 and the Semantic Context UI

**Files:**

- Create: `src/featuregen/overlay/upload/context_graph.py`
- Modify: `src/featuregen/overlay/upload/asset_detail.py` to serve context as one dossier section
- Modify: `src/featuregen/overlay/upload/lineage.py` for explicit per-kind truncation accounting
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/screens/AssetDetailScreen.tsx`
- Tests: backend route/read model and focused frontend context-tab tests

- [ ] Implement a bounded one-hop context read for a table or column anchor by composing the existing
  lineage builder with semantic-context nodes. Do not implement a second BFS or a separate route
  that loses Asset Detail's repeatable-read consistency token/ETag.
- [ ] Reuse `available_identifier_links`, feature lineage, field evidence and structured-result
  readers. Do not re-fold lifecycle state independently.
- [ ] Every inferred/governed edge returns the real evidence producer/strength/lifecycle axes,
  currentness and a concise `why`. Structural `contains` edges carry an explicit structural basis
  and an empty evidence list.
- [ ] Relationship edges use the shared `RelationshipContextV1` projection and distinguish direct
  equality, mapping-table crosswalk, transformed and semantic-only links. Show direction,
  availability, review and deterministic safety separately; a review badge must never imply
  executability.
- [ ] For direct links show the actual candidate revision and each directional realization. Show
  definition/execution and ordered-leg pins only for Release-C crosswalks; never invent those IDs
  for a bridge. Never display fan-out/cardinality without its direction and scope.
- [ ] Jointly with profile Release-A Task 5, add effective dataset-profile nodes/edges identified by
  `catalog_profile_revision_id` and `dataset_profile_hash`; reuse the same assembled profile used by
  feature generation and retrieval.
- [ ] Return `truncated` and per-kind omitted counts; never silently cut the context.
- [ ] Thread caller read scope through bundle, relationship and lineage readers. Update Entity Map
  to use the same availability truth followed by the same visibility projection; a hidden endpoint
  is omitted/redacted rather than leaked by the current unscoped presentation exception.
- [ ] Add a Context tab to Asset Detail showing:
  source meaning, resolved meaning, concept hierarchy, entity/namespace/role, related columns,
  cross-catalog links, features/contracts, uncertainty and ontology gaps.
- [ ] Render missing ownership/usage/data-product context as `not supplied`, not zero or inferred.
- [ ] Keep Entity Map and lineage as focused views backed by the same readers; do not replace them.
- [ ] Cross-catalog links remain discoverable without signed activation. The existing durable/signed
  interlock is shown as an execution state and gates production execution only; review never
  substitutes for it.

**Exit:** the user can finally see how technical metadata, business meaning, evidence and downstream
relationships connect around a column.

## Task 8: Make Feature Generation Consume the Shared Context

**Files:**

- Modify: `src/featuregen/overlay/upload/feature_assist.py`
- Modify: `src/featuregen/overlay/upload/feature_metadata_snapshot.py`
- Modify only if serialization requires: assist API routes
- Tests: feature menu, assist API, snapshot and validation suites

**Generated execution prerequisite:** codegen-remediation Tasks 1–26 plus the separately reviewed
materialization wiring slice. Context-only feature proposal work may land earlier; it may not claim
cluster execution.

- [ ] Replace the feature-specific context assembly with
  `SemanticContextBundleV1.for_feature_generation()`.
- [ ] Include concept ancestry, entity, identifier namespace/issuer state, party role, definition,
  summary, semantic terms, type basis, additivity, unit/currency, grain/time facts, current links and
  missing-context codes.
- [ ] Proposed semantic values are allowed and carry `LLM/PROPOSED/ACTIVE`; validators continue to
  reject physically impossible operations independently. Do not put `llm_proposed` into the
  unrelated `governed|hint` wrapper.
- [ ] Semantic context may nominate datasets and columns, but it never chooses a physical source
  copy or row version. When profile Release B is enabled, every executable feature request follows:
  `DatasetNeedV1(execution_tier=...) -> DatasetSourceSelectionV1 -> DatasetRowSelectionV1 ->
  materialization IR`. An explicit dataset wins; ambiguity or an unsupported historical request
  refuses/clarifies. Execution still requires the named codegen/wiring predecessors.
- [ ] Compile Release-B row selection through the existing feature PIT/time contracts. “Latest” is
  allowed only as the governed latest snapshot at or before the requested cutoff; never rank by
  upload time, catalog scan time or lexical order.
- [ ] When a feature/materialization consumes Release-B decisions, its metadata snapshot and
  lineage pin the `dataset_profile_hash`, serving-policy revision, source-selection hash, physical
  binding revision, temporal-policy revision and row-selection hash. Revalidate current pointers
  immediately before execution and refuse stale pins.
- [ ] After the named prerequisites, add an end-to-end generated-project acceptance proving that the chosen source and row
  predicate reach Kedro/PySpark execution, preserve the declared population spine and remain
  explainable in preview/lineage. Run it through the codegen plan's pinned L0 environments; a live
  cluster run remains a separate approval. A semantic prompt-only test is insufficient.
- [ ] A BIC cannot become a numeric measure; an amount cannot become a join key; a currency-bound
  amount cannot be aggregated across mixed currencies without a conversion policy.
- [ ] Register contract version 4 before enabling it. The rollout flag's OFF branch preserves the
  currently shipped v3 rich-context behavior; do not turn rollback into the obsolete v1 thin menu.
- [ ] Re-budget the 60KB context path: emit one table profile plus a bounded relevant column set and
  per-kind omitted counts. A 111/126-column table must not become a whole-request
  `ContextTooLarge` solely because advisory fields were added.
- [ ] Worked acceptance request:
  “total outgoing counterparty amount by customer for the last 30 days.” The context must nominate
  customer grain, time anchor, monetary measure and currency while keeping BIC as an optional
  grouping dimension, never the customer join key.

**Exit:** feature generation uses the semantics already produced instead of the thin five-field menu;
after profile Release B, executable feature builds also use one explicit, replayable source and row
decision rather than silently choosing a copy.

## Task 9: Make Data-Agent Retrieval Consume the Same Context

**Files:**

- Modify: `src/featuregen/analysis/retrieval.py`
- Create in `src/featuregen/analysis/intent.py`: explicit `AnalysisIntentInputV2`; the current
  `IntentCandidates` is unversioned and must not be described as an existing versioned contract
- Modify: `src/featuregen/api/routes/analysis.py`
- Tests: `tests/featuregen/analysis/test_retrieval.py`, analysis route tests

- [ ] Retain governed grain/as-of columns as leg 1 and lexical relevance as leg 2; this ordering is
  the existing expressibility invariant.
- [ ] Add semantic expansion as leg 3: controlled concept names, aliases, parent concepts, domains,
  entities and related glossary terms from Context Graph V1.
- [ ] Add a bounded one-hop available-link neighbourhood as leg 4. Never traverse the whole catalog;
  report truncation.
- [ ] Always retain required grain and time columns before optional descriptive context.
- [ ] Feed selected `SemanticContextBundleV1` objects to intent extraction, including evidence
  authority and missing-context codes.
- [ ] Preserve the closed offered-candidate guarantee: semantic expansion adds read-scoped refs to
  the offered set before the LLM call; it never lets an output ref bypass `validate_intent`.
- [ ] When the plan cannot proceed, return a typed clarification. Record an analysis-learning gap
  only after profile Release-B wires `run_analysis` and the refusal-to-gap mapping; do not write
  unreachable metadata into a store with no production producer.
- [ ] Do not add embeddings/vector infrastructure in this task. Exact controlled-semantic expansion
  is the first functional improvement; embeddings can be measured later.

**Exit:** a natural-language question can retrieve semantically relevant columns whose physical
names do not contain the question's words, without offering the whole catalog to the LLM.

## Task 10: Gold Evaluation, Adversarial Tests and Mutation Gate

**Files:**

- Create: `tests/eval/semantic_context_gold_v1.json`
- Create: shared semantic/profile mutation harness (the bridge Task-12 harness is not on main)
- Reuse: existing feature-context evaluation machinery, with shipped v3 as the rollback baseline

- [ ] Build a gold set covering at least these defect families:
  BIC vs CIF, internal vs external account, amount vs identifier, branch ID vs branch description,
  event time vs ingestion time, currency vs amount, status/code vs description, sensitive party
  attributes, target/leakage labels, and honestly unclassified columns.
- [ ] Include the six named CIB/FTR witnesses from Task 4 plus synthetic counterexamples; do not make
  the suite depend on live catalog rows.
- [ ] Before deployment, compare current-v3 versus new-v4 behavior using deterministic fakes and
  stored structured results. Record selected-concept accuracy, unsafe feature acceptance, false
  cross-namespace links, grounded retrieval and unclassified precision. Do not call a live model.
- [ ] Move the same-provider/model comparison, ontology-gap usefulness judgement and real
  token/call cost measurement to Gate B after the explicit live-LLM approval STOP.
- [ ] Release bars:
  zero BIC↔CIF candidates; zero physical facts attributed to an LLM; zero source/human overwrite;
  zero unsafe accepted gold features; no regression in grounded acceptance; a measurable semantic
  retrieval lift; zero observation reused across a mismatched direction/scope/binding; zero
  reviewed-but-unsafe relationship displayed as executable; no unexplained zero-output stage.
- [ ] Required must-die mutations:
  remove description from vocabulary fingerprint; remove issuer from namespace identity; treat
  party role as identifier scheme; re-key historical `counterparty_id` without reconciliation;
  accept a free-text concept; drop supersession; let feature generation read
  the thin private menu; remove context truncation reporting; let review imply safety; invert a
  relationship direction; reuse an observation across bindings; treat a sample as uniqueness proof;
  omit source/row decisions from a feature snapshot; rank a source by upload time.
- [ ] Build the harness here with a literal baseline count for named focused suites only, a
  must-die sentinel, a must-survive no-op and anchor-cardinality checks. Do not use the known
  order/environment-contaminated whole-repository count.

**Exit:** the richer LLM usage demonstrably improves meaning and retrieval without creating new
silent operational authority.

## Task 11: Deploy and Activate — Two Explicit Approval Gates

### Gate A: deploy/migrate/configure, no catalog upload

- [ ] Add `FEATUREGEN_FEATURE_CONTEXT` and `FEATUREGEN_DATASET_PROFILES` to documented example and
  approved deployment configuration using the shared truthy parser. Present exact image SHA,
  applied migration filename/checksum set, flag matrix, test report and v3 rollback behavior.
- [ ] STOP and request approval.
- [ ] After approval, deploy; configure catalog issuer scopes through the new command/API; enable
  `FEATUREGEN_FEATURE_CONTEXT=1` only in the approved environment.
- [ ] Run migration-aware smoke tests and the Asset Detail Context dossier section against existing
  data; prove table/endpoint read scope with two roles.
- [ ] Do not upload or re-upload a catalog under this approval.

### Gate B: catalog re-enrichment/re-upload and live LLM spend

- [ ] Present exact catalog sources, source-file hashes, expected LLM calls/cost, current result
  snapshot and the evidence types that can change.
- [ ] STOP and request separate approval.
- [ ] After approval, run the same-provider/model current-v3 versus new-v4 comparison and record
  quality plus actual call/token cost before approving a catalog-wide run.
- [ ] If an immutable stored source snapshot can drive re-enrichment, prefer it. Otherwise a file
  re-upload requires the explicit approval; this plan is not that approval.
- [ ] After approval, process one pilot catalog first, audit it, then request confirmation before the
  second catalog.
- [ ] Reconcile these witnesses manually in the UI:
  `counter_party_bic`, `counter_party_cif_id`, `actual_counter_party_amt`, `cust_swift_cd`,
  `cust_num`, `cif_id`, `sol_desc`.
- [ ] Preserve the ingestion-richness acceptance set rather than replacing it with the seven names:
  verify party-role coverage, all six currency witnesses, both governed table stamps, decoy-link
  closure and byte-identical source/human evidence before and after pilot enrichment.
- [ ] Verify feature generation receives context contract v4 and the worked request from Task 8 is
  grounded correctly.
- [ ] Verify old decoy proposals are superseded rather than deleted or falsely human-rejected.

**Exit:** new semantics are visible and consumed live, with no unapproved catalog mutation.

## Task 12: Handoff to Direct Hive/ODS Data Enrichment

No Hive/ODS call is implemented or authorized in this plan. Do not freeze a second observation
interface: the next slice writes the shipped `RelationshipObservationV2` for direct links and the
Release-C two-leg composition observation for crosswalks, then the context builder projects those
faithfully. Column distribution/type profiles use their existing immutable observation store or a
separately reviewed additive contract; they are not stuffed into relationship observations.

- exact completeness, method, row coverage, side-specific bindings/snapshots and supported/refuted
  claims remain explicit; a sample may disprove uniqueness but never establish it;
- observations override no source meaning, but can corroborate/refute identifier and physical-shape
  claims;
- deterministic temporal-storage profiling remains explicitly deferred to this slice;
- the data agent and feature generator consume observations through the same scoped readers;
- no OpenMetadata dependency is introduced.

The next slice should execute:

```text
manual physical binding
  -> bounded Hive/ODS profile
  -> immutable data/relationship observations
  -> scoped lifecycle validation
  -> context reprojection through shared readers
  -> stronger bridge ranking and analysis planning
```

---

## Execution Order

```text
Joint Semantic/Profile Task 0 (read-only)
  -> Joint Task 0.5 verified interfaces
  -> Shared Task 0.6 prerequisite seam fixes
  -> Semantic Tasks 1-2 + Profile Release-A Tasks 1-3 (only non-colliding files)
  -> jointly: Semantic Tasks 3-4 + Profile Release-A Task 4
       exact purpose payload, schema registration and replay identity
  -> Semantic Tasks 5-6
       adjudication, supersession and projection consistency
  -> jointly: Semantic Tasks 7-9 + Profile Release-A Task 5
       one Context Graph, feature-context and data-agent-context implementation
  -> jointly: Semantic Task 10 + Profile Release-A Task 6
       semantic/profile evaluation and mutations
  -> Task 11A deploy/configure Release A (approval)
  -> Task 11B pilot re-enrichment/re-upload (separate approval)
  -> Profile Release-B Tasks 7-9 + data-agent run_analysis wiring
       dataset need -> source choice -> temporal rows -> executable analysis replay
  -> Task 12 Hive/ODS observation handoff
  -> Codegen-remediation Tasks 1-26
  -> separately reviewed materialization compile/render/submit/publish wiring slice
  -> Profile Release-C Tasks 10-13
       crosswalk definition -> scoped observation -> two-leg compiler -> UI/lineage
```

Semantic Tasks 7–9 and profile Task 5 may be developed in parallel only after their shared profile,
context and current-value contracts freeze. Task 10 and profile Task 6 must finish before the first
live gate. Release B and C retain their own migration/deploy/live-data approval gates; Task 11 does
not implicitly authorize them.

## Definition of Done

1. Every enriched column has one stable `SemanticContextBundleV1` with source facts, current
   resolved semantics, authority, evidence IDs, hierarchy, missing context and content hash.
2. `counterparty` is rendered as a party role. Existing `counterparty_id` semantics and bridge keys
   remain stable until a separate reconciliation is approved.
3. Namespace comparison includes issuer state and cannot propose across known-different issuers.
4. Any change to prompt-visible ontology meaning invalidates the affected replay key.
5. The LLM sees full bounded table/glossary context and produces validated alternatives, reasons,
   missing-context codes and optional ontology gaps for ambiguous cases.
6. Ontology-gap suggestions never edit the registry automatically.
7. Source/human evidence survives every rerun; obsolete system proposals are visibly superseded.
8. Context Graph V1 makes semantic, evidence, link, feature/contract and gap relationships visible
   from a column without a new graph database.
9. Feature generation and the data agent consume the same context contract; neither maintains a
   private semantic interpretation.
10. Relationship context distinguishes semantic availability, review state and each current
    directional realization's deterministic safety. Crosswalk-only definition/execution/ordered-leg
    pins appear only after Release C creates them.
11. Every relationship observation is bound to exact direction, applicability scope, physical
    bindings and relationship revisions; sampled evidence never proves uniqueness.
12. Effective profile context carries catalog-revision provenance and exact
    `dataset_profile_hash`, while
    request-specific source and temporal decisions remain separately pinned in plan/snapshot
    identity.
13. Feature materialization consuming Release B follows dataset need -> source selection -> row
    selection -> generated IR, refuses stale decisions and exposes the exact choices in lineage.
14. Uploaded or LLM-suggested dependencies, grains and key mappings remain advisory typed proposals
    until the existing grain/bridge/crosswalk authority path admits them.
15. Proposed semantics are usable for exploration and feature generation with authority visible;
    human review is not an availability gate.
16. No LLM output is treated as physical type, uniqueness, cardinality, overlap or data-quality
    proof.
17. Adversarial and mutation gates pass before any live action.
18. Deploy approval and catalog re-upload approval remain separate, and no catalog is re-uploaded
    without the user's explicit authorization.

## Explicitly Deferred

- OpenMetadata connector expansion.
- Direct Hive/ODS profiling implementation and scheduling.
- Embedding/vector retrieval; add only after exact semantic expansion is measured.
- Full ontology editor and automatic ontology mutation.
- Historical `counterparty_id` entity migration, bridge-fact re-keying and reconciliation.
- Fuzzy/entity-resolution models over row-level data.
- Ownership, usage, conversation and data-product adapters without real sources.
- New graph database or dual-write architecture.
- Security/NFR programmes beyond preserving existing functional boundaries.
- OpenMetadata table-description import, operational/audit serving purposes, typed dependency-kind
  invalidation and demand ranking over open gaps.
