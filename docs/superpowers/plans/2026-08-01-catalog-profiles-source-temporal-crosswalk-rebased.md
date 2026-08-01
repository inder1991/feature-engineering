# Catalog Profiles, Source/Temporal Choice, and Crosswalks — Rebased Implementation Plan

Date: 2026-08-01  
Status: **RELEASE A READY ONLY AFTER JOINT TASKS 0, 0.5 AND SHARED PREREQUISITE FIXES; RELEASES B/C HAVE NAMED EXECUTION GATES**  
Supersedes: `2026-07-30-catalog-profiles-crosswalk-and-temporal-policy.md`

Binding addendum:
`docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md`. If this plan and that
ledger disagree, stop and reconcile both before coding.

This is a functionality-first plan. It deliberately postpones OpenMetadata integration, general
security/NFR work, schedulers, a new graph database, arbitrary transformations, and large-scale
operational hardening. It preserves the product capabilities from the July 30 programme but puts
them in the order in which they produce trustworthy user value.

The implementation must run in a new worktree from the current `origin/main`. The root checkout is
not an implementation baseline and contains unrelated user changes.

---

## 1. Architectural Recommendation

Build this in three value releases, not as one monolithic programme:

1. **Dataset understanding** — show and use what each catalog and table means.
2. **Source and time correctness** — select the right declared dataset and the right historical
   rows for a question.
3. **Crosswalk relationships** — traverse mapping tables when identifiers are not directly equal.

This is sequencing, not product-scope removal. Profiles, source selection, temporal selection and
crosswalks remain in the programme. The sequencing prevents a large crosswalk/execution project
from delaying metadata that immediately improves search, feature generation and the data agent.

### What adds value now

| Capability | Product value | Decision |
| --- | --- | --- |
| Table business description/context | Makes technical names understandable to people and LLMs | Build in Release A |
| Data role: event fact, snapshot fact, dimension, reference, crosswalk | Improves retrieval, feature suggestions and modelling warnings | Build in Release A |
| Primary entity | Helps questions and features find their grain | Reuse and expose in Release A |
| Authority role: system of record, replica, derived, etc. | Prevents silently selecting a convenient but wrong copy | Build in Release A; operational use in Release B |
| Temporal storage model: current, SCD2, snapshot, event log | Prevents joining today's customer segment to historical activity | Build in Release A; operational use in Release B |
| Profile-aware LLM synthesis and criticism | Fills metadata at scale and makes uncertainty explicit | Build in Release A |
| Profile search, Context Graph, feature and agent consumption | Turns collected metadata into product behaviour | Build in Release A |
| Explicit population/source policy and temporal row policy | Prevents plausible-but-wrong analysis | Build in Release B |
| Mapping-table crosswalk | Connects genuinely different identifier schemes | Build in Release C after observations exist |

### What does not add enough value now

The following are not deleted from the long-term programme, but are excluded from this implementation
because the platform has no reliable source or immediate consumer for them:

- OpenMetadata ingestion;
- owner/steward directories with no authoritative identity source;
- catalog-wide default data or authority roles—real catalogs are mixed;
- a source-delivery SLA inferred from upload or catalog scan times;
- free-text executable dependencies;
- arbitrary SQL transformations;
- automatic many-to-many allocation;
- full ER/ontology exploration screens;
- a second graph database or a second execution interpreter;
- operational/audit serving-purpose variants with no current consumer.

These may be added through explicit adapters later. Absence must be shown as `not_supplied`, never
filled with an LLM guess.

---

## 2. Verified Rebase Baseline

The plan was rebased against `origin/main@fa9a20b0` on 2026-08-01. Task 0 must resolve a newer head
if main moves.

### Already present—reuse, do not rebuild

“Present” means a typed library/test seam exists, not that a production route invokes it. On the
verified baseline the materialization chain, `run_analysis`, relationship-observation persistence
and bridge admission have no production caller. Their wiring is explicit work below; they must not
be used as evidence that an exit criterion already executes.

- schema-preserving logical references in `overlay/upload/object_ref.py`;
- immutable per-field evidence in migration `0983_field_evidence.sql`;
- per-field authority and conflict policies in `field_policies.py` and `field_authority.py`;
- table-level `table_role`, `primary_entity`, and `event_or_snapshot` LLM evidence from
  `table_synth.py`;
- table-role normalization in `table_vocab.py`;
- source capability profiles for upload parsing—these are ingestion profiles, not semantic
  catalog profiles;
- the shared content-addressed `structured_result` store;
- `PhysicalObjectIdentityV1`, `PhysicalDatasetBindingV1`, binding revisions, connections and
  catalog-engine routing;
- identifier-link assessments, directional bridge realizations, lifecycle readers and relationship
  observations;
- direct cross-catalog join planning and generated Kedro/PySpark precondition gates;
- data-agent retrieval, clarification, analysis learning and point-in-time dimension fixture logic;
- materialization IR, generated Kedro project and metadata snapshots;
- the column dossier, Entity Map and governance queue;
- the richer feature-context seam behind `FEATUREGEN_FEATURE_CONTEXT`.

### Still absent

- catalog narrative revisions and current pointers;
- an assembled effective dataset semantic profile;
- `authority_role` and `temporal_storage_model` evidence/policies;
- profile-aware upload fields, profile API and profile UI;
- profile facets and table-profile text in search;
- an evidence-bound dataset-profile LLM critic;
- a request-level source-selection policy and decision;
- a reusable dataset temporal-row policy and decision;
- a mapping-table crosswalk definition, observation and two-leg compiler;
- snapshot/lineage items for profile, source, row policy and crosswalk decisions.

### Migration reality

Source migrations currently reach `1043_semantic_binding_fixed_currency.sql`; the inspected live
Kind deployment reaches `1041_catalog_engine`. The old plan's instruction to allocate from `1040`
is invalid. Numeric prefixes are already duplicated at 0973, 0974, 1034, 1036, 1037, 1038 and
1040. The full filename/checksum set—not only a numeric head—is authoritative. Reservations
1044–1049 are owned by the binding addendum; no task independently allocates “the next number.”

---

## 3. Ownership and Relationship to Other Plans

This plan does not execute independently from the current product roadmap.

| Concern | Owner |
| --- | --- |
| Column semantic context, ontology identity, Context Graph V1, feature/data-agent context adapters | `2026-08-01-semantic-enrichment-context-consumption.md` |
| Catalog narrative and effective dataset semantic profile | This plan |
| Direct identifier assessment, directional cardinality and safety | Bridge remediation programme/current bridge modules |
| Physical bindings and profile/relationship observations from Hive/ODS | Data-powered ontology/data-agent programme |
| Generated code correctness | `2026-07-31-codegen-review-remediation.md` Tasks 1–26 |
| Materialization production wiring | separate reviewed Phase-G slice; absent on current main |
| Source/temporal selection and mapping-table crosswalk | This plan |

Dependency rule:

- Joint Tasks 0 and 0.5 and the semantic plan's shared Task 0.6 finish before product code.
- Release-A Tasks 1–3 may then start alongside semantic Tasks 1–2 only when their shared file
  ownership does not collide; they no longer wait on an undefined “bundle frozen” milestone.
- Release-A Task 4 is implemented atomically with semantic Tasks 3–4 so payload, schema and replay
  identity change together;
  two parallel edits to `table_synth.py`/`enrich_llm.py` are forbidden.
- Release-A Task 5 extends the semantic plan's Context Graph and consumer tasks after those contracts
  exist; it does not build competing context/search adapters.
- Release B may start after effective dataset profiles exist.
- Release C discovery may start after Release A, but compilation/execution cannot start until
  codegen-remediation Tasks 1–26 and a reviewed materialization-wiring slice are complete. Execution
  also cannot complete until physical
  bindings and relationship observations exist for the mapping dataset and both legs.

There must be no parallel profile resolver, LLM replay store, relationship safety model, metadata
snapshot, physical identity, or execution interpreter.

---

## 4. Corrections to the Superseded Plan

1. **Do not add dataset profile revision/current tables.** Dataset semantic fields already live as
   immutable, producer-scoped `field_evidence` at table logical refs. A dataset profile is a typed
   read model over those fields, specialized governed facts and physical-binding state.
2. **Do add a small catalog-profile store.** Catalog narrative has no object-level evidence key or
   current-pointer source today. It is genuinely new state.
3. **Do not inherit catalog data role or authority role.** A catalog can contain an event fact, a
   customer dimension and a non-authoritative extract at once. Inheriting either classification
   silently lies about individual tables.
4. **Do not duplicate `table_role`.** Derive `DataRole` from the existing normalized table role;
   map legacy `bridge` to display role `crosswalk` without rewriting old evidence.
5. **Do not let `authority_role` choose a population.** The current analysis contract correctly
   requires an explicit population table because completeness cannot be inferred from metadata.
   Authority ranks copies only after the required dataset function is known.
6. **Key source policy by dataset need, not only entity and purpose.** “customer + analytical” is
   insufficient: a customer population, customer dimension and customer-event fact may all match.
7. **Remove freshness ranking from v1.** Catalog drift freshness and upload time are not data-arrival
   SLAs. No source-delivery SLA exists, so the system must not fabricate one.
8. **Do not re-key existing bridges with `relationship_kind`.** Current bridge fact keys and
   directional realization identities are load-bearing. Direct equality remains the existing bridge
   family; crosswalks get a separate definition referencing the same endpoint and realization types.
9. **Do not claim transformed-link execution exists.** Current structured predicates support fixed
   values, as-of intervals and missing-key requirements—not general normalization. Transformed and
   semantic-only links remain discoverable until a closed transformation contract is separately
   implemented.
10. **Do not call a table role an executable crosswalk.** `data_role=crosswalk` is a discovery hint;
    a crosswalk becomes executable only after its mapping keys, row policy and directional fan-out
    are established.
11. **Remove OpenMetadata work.** It is explicitly outside the current product direction.
12. **Every stored field needs a named consumer.** Metadata that only decorates a screen is not part
    of this slice.

---

## 5. Functional Rules

1. LLM output may supply descriptions, classifications, alternatives and missing-context signals.
   It never attests physical type, uniqueness, overlap, cardinality, population completeness,
   delivery SLA or row-history correctness.
2. LLM-proposed profile values are usable for search, retrieval, feature suggestions and marked
   sandbox analysis without human approval. Their authority travels with them.
3. Production source choice may use only an explicit serving policy or a load-bearing authority
   value. Human approval is not a bridge-availability gate and does not make an unsafe join safe.
4. The population dataset remains explicit in an analysis request or serving policy. The LLM may
   propose a clarification candidate; it may not silently decide completeness.
5. A current request and a historical request are different. Historical requests never fall back
   to today's SCD/current-only row.
6. SCD intervals are half-open `[effective_from, effective_to)`. Overlap or ordering ties refuse.
7. Source selection and row selection are separate decisions and are separately explainable.
8. Crosswalk availability and execution safety are separate. An unreviewed candidate may be shown
   and planned for sandbox; exact execution runs the current deterministic gate.
9. Every decision records the candidates considered and why they were admitted, rejected or tied.
10. Every typed refusal becomes a data-agent clarification when interactive and an analysis-learning
    gap otherwise.
11. Feature materialization continues through generated Kedro/PySpark. Data-agent analysis continues
    through its typed `AnalysisExecutionIRV1` and dialect compiler. No LLM-written SQL, second
    feature interpreter or ad-hoc crosswalk executor is introduced.
12. No deploy, upload, re-upload, live LLM run or Hive/ODS query occurs without approval for that
    exact action.
13. Use only three new rollout switches: `FEATUREGEN_DATASET_PROFILES`,
    `FEATUREGEN_SOURCE_TEMPORAL_SELECTION`, and `FEATUREGEN_CROSSWALK_EXECUTION`. Existing LLM and
    feature-context switches remain the owners of their paths; do not create a flag per field.
14. New contract identities use the shared versioned RFC-8785/JCS hasher. Existing evidence hashes
    are not migrated. The canonical profile name is `dataset_profile_hash` everywhere.
15. Cross-catalog discovery is not gated by human review or signed activation. Production
    cross-catalog execution additionally requires the existing durable/signed live-activation
    interlock; deterministic safety remains separately required.
16. The flag dependency is `CROSSWALK -> SOURCE_TEMPORAL -> DATASET_PROFILES`. An invalid
    combination fails configuration validation instead of running a half-enabled path.

---

## 6. Contracts to Freeze

### 6.1 Profile vocabulary

```python
class DataRole(StrEnum):
    EVENT_FACT = "event_fact"
    SNAPSHOT_FACT = "snapshot_fact"
    FACT = "fact"
    DIMENSION = "dimension"
    REFERENCE = "reference"
    CROSSWALK = "crosswalk"        # derived from existing table_role="bridge"
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
```

One normalizer owns each vocabulary. `DataRole` adapts `table_vocab.normalize_table_role`; it does
not introduce a competing normalization path.

### 6.2 Catalog narrative

```python
@dataclass(frozen=True, slots=True)
class CatalogProfileRevisionV1:
    catalog_source: str
    display_name: str | None
    description: str | None
    business_context: str | None
    business_domains: tuple[str, ...]
    producer: EvidenceProducer
    strength: AssertionStrength
    lifecycle: EvidenceLifecycle
    producer_ref: str               # subject or versioned system producer
    ingestion_run_id: str | None
    content_hash: str
    revision_id: str

@dataclass(frozen=True, slots=True)
class CatalogProfileCurrentV1:
    catalog_source: str
    revision_id: str
    pointer_version: int
```

The canonical JCS content includes values plus the real producer/strength/lifecycle axes.
`producer_ref`, `ingestion_run_id` and
`created_at` are persistence provenance, not content identity. An upload run is provenance, not an
invented bridge `EvidenceRefV1`: the bridge evidence vocabulary is not reused to mislabel an upload
declaration. Catalog profile values are descriptive; they never default a dataset's role, authority
or temporal model. Issuer scope remains owned by the semantic-context plan's catalog semantic-scope
contract. `catalog_profile_revision_id` is authoring provenance/current-pointer identity; only the
semantic narrative `content_hash` enters a dataset profile hash, so byte-identical re-authoring does
not re-key every dataset.

### 6.3 Effective dataset profile

No new dataset-profile source-of-truth table is created.

```python
@dataclass(frozen=True, slots=True)
class EffectiveProfileFieldV1:
    display: SemanticValueV1 | None
    load_bearing: SemanticValueV1 | None
    state: str  # display_only | load_bearing | no_evidence | needs_data_observation |
                # structurally_unsuitable | conflict | projection_unavailable
    reason_codes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class GovernedFactHeadV1:
    fact_key: str
    folded_status: str
    confirmed_event_id: str | None

@dataclass(frozen=True, slots=True)
class DatasetSemanticProfileV1:
    dataset_logical_ref: str
    catalog_profile_revision_id: str | None
    description: EffectiveProfileFieldV1
    business_context: EffectiveProfileFieldV1
    domains: EffectiveProfileFieldV1
    data_role: EffectiveProfileFieldV1
    primary_entity: EffectiveProfileFieldV1
    authority_role: EffectiveProfileFieldV1
    temporal_storage_model: EffectiveProfileFieldV1
    event_or_snapshot: EffectiveProfileFieldV1
    grain_fact: GovernedFactHeadV1 | None
    availability_fact: GovernedFactHeadV1 | None
    missing_context: tuple[str, ...]
    dataset_profile_hash: str
```

The builder reads current field resolution, specialized grain/availability facts and current
catalog narrative in one repeatable-read transaction. It preserves the authority engine's existing
display-versus-load-bearing split—an LLM value can be useful context without being execution
authority. `dataset_profile_hash` uses the shared versioned JCS hasher and includes both
resolutions, real evidence axes/IDs, relevant fact heads and catalog narrative content hash; it
excludes the narrative revision ID, physical bindings, environment, current time, job
state and projection timestamps. Physical binding identity belongs to
`DatasetSourceSelectionV1`, so changing environments never re-keys semantic meaning.

`EffectiveProfileFieldV1` is a typed re-wrapper around the existing `FieldResolution`; it is not a
second resolver. Recommendation-ceilinged fields legitimately have `state=display_only`, not a
failure-shaped unresolved status. `no_evidence`, `needs_data_observation` and
`structurally_unsuitable` remain distinguishable.

Field ownership:

| Profile field | Existing/new source | Operational use |
| --- | --- | --- |
| description | existing table `definition` evidence | prompts, search, UI |
| business_context | new advisory table field | prompts, search, retrieval |
| domains | existing table `domain` evidence | retrieval and filtering |
| data_role | derived from existing `table_role` | retrieval ranking, planner warnings, crosswalk discovery |
| primary_entity | existing table field | retrieval and dataset-need matching |
| authority_role | new field evidence | source ranking only when load-bearing |
| temporal_storage_model | new field evidence | row policy only when load-bearing |
| event_or_snapshot | existing advisory field | feature/data-agent context |
| grain/availability | specialized governed facts | unchanged operational gates |

Technical CSV ingestion currently writes no source-attested table narrative. For those catalogs,
`business_context`/an explicit profile edit or a labelled LLM proposal is the only table prose until
a real source supplies one; the UI must not imply the connector attested it.

### 6.4 Dataset need and source selection

```python
class DatasetNeedRole(StrEnum):
    POPULATION = "population"
    EVENT_SOURCE = "event_source"
    DIMENSION_SOURCE = "dimension_source"
    REFERENCE = "reference"
    MAPPING = "mapping"

class ServingPurpose(StrEnum):
    FEATURE_SERVING = "feature_serving"
    ANALYTICAL = "analytical"

@dataclass(frozen=True, slots=True)
class PolicyProvenanceV1:
    evidence: tuple[EvidenceAuthorityV1, ...]
    decision_refs: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class DatasetNeedV1:
    entity_id: str
    need_role: DatasetNeedRole
    serving_purpose: ServingPurpose
    execution_tier: ExecutionTier       # reuse bridge_realization.ExecutionTier
    required_concepts: tuple[str, ...]
    explicit_dataset_ref: str | None

@dataclass(frozen=True, slots=True)
class CandidateDecisionV1:
    dataset_ref: str
    dataset_profile_hash: str
    binding_revision_id: str | None
    disposition: str                    # selected | eligible | rejected | tied
    reason_codes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class DatasetServingPolicyRevisionV1:
    entity_id: str
    need_role: DatasetNeedRole
    serving_purpose: ServingPurpose
    eligible_dataset_refs: tuple[str, ...]
    preferred_dataset_refs: tuple[str, ...]
    provenance: PolicyProvenanceV1
    revision_id: str

@dataclass(frozen=True, slots=True)
class DatasetSourceSelectionV1:
    need: DatasetNeedV1
    selected_dataset_ref: str
    selected_dataset_profile_hash: str
    selected_binding_revision_id: str
    serving_policy_revision_id: str | None
    authority_role: str
    authority_basis: str
    selection_basis: str
    considered_candidates: tuple[CandidateDecisionV1, ...]
    content_hash: str
```

An explicit population declaration wins. The source selector never turns `primary_entity=customer`
into “this is the complete customer population.” No freshness field appears until a real delivery
SLA/observation contract exists.

### 6.5 Temporal policy and row decision

Release B supports only policies the current product needs: current record, SCD2 valid at report
cutoff, and latest snapshot at or before cutoff.

```python
class TemporalSelectionKind(StrEnum):
    CURRENT_RECORD = "current_record"
    VALID_AT_REPORT_CUTOFF = "valid_at_report_cutoff"
    LATEST_SNAPSHOT_AS_OF = "latest_snapshot_as_of"
    EXPLICIT_ONLY = "explicit_only"

@dataclass(frozen=True, slots=True)
class DatasetTemporalPolicyRevisionV1:
    dataset_logical_ref: str
    temporal_storage_model: TemporalStorageModel
    current_selection: TemporalSelectionKind
    historical_selection: TemporalSelectionKind
    effective_from_ref: str | None
    effective_to_ref: str | None
    snapshot_ref: str | None
    current_flag_ref: str | None
    availability_ref: str | None
    tie_break_refs: tuple[str, ...]
    provenance: PolicyProvenanceV1
    revision_id: str

@dataclass(frozen=True, slots=True)
class DatasetRowSelectionV1:
    dataset_logical_ref: str
    dataset_profile_hash: str
    temporal_policy_revision_id: str
    selection_kind: TemporalSelectionKind
    cutoff_value_ref: str | None
    predicate_payloads: tuple[dict[str, object], ...]
    content_hash: str
```

The policy references normalized logical column refs only. Runtime values such as a report date are
parameters, never concatenated SQL. The analysis adapter emits the existing
`data_agent.dimensions.DimensionAttributionPolicyV1` for SCD2. `CURRENT_RECORD` and
`LATEST_SNAPSHOT_AS_OF` are new engine adapters owned by Task 8; they are not described as existing
analysis functionality. Feature/materialization adapters reuse their existing time/PIT contracts
only where those contracts can represent the declared policy.

### 6.6 Crosswalk definition

Existing bridge facts remain direct-equality relationships and keep their identities unchanged.

```python
# RelationshipKind is imported from semantic-context Task 1; this plan does not redefine it.

@dataclass(frozen=True, slots=True)
class LogicalMappingPairV1:
    endpoint_member_ref: str       # schema-preserving logical column ref
    mapping_member_ref: str        # schema-preserving logical column ref

@dataclass(frozen=True, slots=True)
class CrosswalkDefinitionRevisionV1:
    source_endpoint: IdentifierEndpointV1
    mapping_dataset_ref: str
    source_to_mapping_pairs: tuple[LogicalMappingPairV1, ...]
    mapping_to_target_pairs: tuple[LogicalMappingPairV1, ...]
    target_endpoint: IdentifierEndpointV1
    mapping_temporal_policy_revision_id: str | None
    evidence: tuple[EvidenceAuthorityV1, ...]
    definition_id: str
    revision_id: str

@dataclass(frozen=True, slots=True)
class JoinLegPinV1:
    leg_kind: str                     # same_catalog | cross_catalog
    from_dataset_ref: str
    to_dataset_ref: str
    from_binding_revision_id: str
    to_binding_revision_id: str
    plan_content_hash: str
    read_set_hash: str
    approved_join_fact_keys: tuple[str, ...]
    realization_revision_id: str | None
    dependency_snapshot_id: str | None
    predicate_content_hashes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class CrosswalkExecutionRevisionV1:
    crosswalk_definition_revision_id: str
    mapping_binding_revision_id: str
    source_leg: JoinLegPinV1
    target_leg: JoinLegPinV1
    mapping_temporal_policy_revision_id: str | None
    applicability_scope: RealizationApplicabilityScopeV1
    leg_observation_revision_ids: tuple[str, ...]
    composition_observation_revision_id: str
    combined_cardinality: DirectionalCardinalityVerdictV1
    safety_status: SafetyStatus
    execution_revision_id: str
```

The definition accepts logical/unbound, schema-preserving endpoints/pairs only; it does not reuse
the bridge-only flat-public `ColumnPairV1`. Physical identities and binding revisions belong in
execution. Identity includes the two endpoint identities and mapping dataset. Revision identity additionally
includes ordered tuple mappings, temporal policy and evidence. It is environment-independent. The
execution revision then pins the physical mapping binding, the two governed leg plans, side-specific
bindings, predicates/read sets, scope, per-leg observations and one exact composed observation
after temporal filtering. A bare plan hash is never sufficient. `SafetyStatus`, `ExecutionTier`, cardinality and applicability are reused from the
bridge family; this is a composition carrier, not a competing safety model. Human review is
intentionally absent from all execution identities.

V1 conflict behaviour is only `refuse_on_multiple`. A temporal/current filter may be applied when a
governed policy makes the mapping unique. No deduplication, first-row selection or many-to-many
allocation is allowed.

---

## 7. Release A — Dataset Understanding

### Task 0 — Freeze the executable baseline (read-only)

**Inspect:** current `origin/main`, migration directory, live migration ledger, relevant flags.  
**Modify:** only an execution record in this plan when implementation starts.

- [ ] Create a new worktree/branch from current `origin/main`; record its SHA.
- [ ] Record root and worktree dirt; do not copy unrelated user edits.
- [ ] Record the source and live applied migration filename+checksum sets separately.
- [ ] Verify the plan-specific modules remain absent and record any parallel implementation.
- [ ] Run focused baseline suites for field evidence/resolution, table synthesis, structured
  results, search, asset detail, feature context, analysis retrieval, bridge realization and
  materialization joins.
- [ ] Record exact test counts and commands.
- [ ] Do not deploy, upload, re-upload, call a live LLM or connect to Hive/ODS.

**Exit:** one reproducible baseline and zero mutation.

### Task 0.5 — Import the shared verified-interface ledger

Execute this jointly with semantic Task 0.5; do not create a profile-specific copy.

- [ ] Confirm migration reservations 1044–1049, shared JCS hashing, evidence axes,
  `RelationshipKind`, `dataset_profile_hash`, `JoinLegPinV1`, observation shape, six Release-B
  snapshot kinds, flags and inter-plan completion gates.
- [ ] Complete semantic Task 0.6's table-ref, table-visibility, graph-reprojection, cycle and
  kind-aware snapshot fixes before profile authoring/search code.

**Exit:** this plan consumes the binding addendum verbatim.

### Task 1 — Freeze profile vocabularies and effective-profile assembly

**Create:**

- `src/featuregen/overlay/upload/profile_vocab.py`
- `src/featuregen/overlay/upload/dataset_profiles.py`

**Modify:**

- `field_policies.py`
- `field_resolution.py` only for supported table projections
- `field_correction.py` for the bounded proposal action and closed validators

**Tests:** `tests/featuregen/overlay/upload/test_dataset_profiles.py`

- [ ] Implement the enums and mappings in §6.1.
- [ ] Map existing `table_role=bridge` to `DataRole.CROSSWALK`; do not rewrite evidence.
- [ ] Accept `crosswalk` as an input alias that normalizes to the existing canonical
  `table_role=bridge`; profile output still displays `DataRole.CROSSWALK`.
- [ ] Add field policies for `business_context`, `authority_role` and
  `temporal_storage_model`.
- [ ] `business_context`: recommendation/display field, LLM/source/human visible.
- [ ] `authority_role`: LLM visible for exploration; load-bearing only from source-attested or
  human-confirmed evidence. HUMAN/PROPOSED is also displayable but not load-bearing.
- [ ] `temporal_storage_model`: LLM/uploader visible; load-bearing in this release only from
  source-attested or human-confirmed evidence. Deterministic profiler-supported classification is
  deferred to the approved Hive/ODS observation slice; do not claim a producer that does not exist.
- [ ] Add one validated, bounded table-profile command. Field-specific enum/entity validation runs
  before any write.
- [ ] Add a dedicated `propose_advisory` command for callers with existing `catalog:write`. It
  appends bounded `HUMAN/PROPOSED/ACTIVE` evidence; it never manufactures
  `HUMAN/CONFIRMED`. Scope it only to the three new fields and include `HUMAN/PROPOSED` in their
  display rules—not in operational rules.
- [ ] Register `authority_role` and `temporal_storage_model` with `human_editable=True`. Uploader/LLM
  proposals are displayable but non-load-bearing; the existing platform-admin four-eyes actions are
  the named confirmation path. Do not weaken confirmation for operational influence.
- [ ] Build `DatasetSemanticProfileV1` from current authoritative readers in one transaction.
- [ ] Report direct authority/status/evidence for every field and explicit missing-context codes.
- [ ] Build `EffectiveProfileFieldV1` as a re-wrapper of `FieldResolution` with the closed states in
  §6.3; recommendation fields resolve as `display_only`, not permanently “unresolved.”
- [ ] Property-test stable `dataset_profile_hash` values and sensitivity to every meaning-bearing
  input. Byte-identical catalog narrative revisions do not churn the hash.

**Exit:** every table can produce one explainable semantic profile without a second truth store.

### Task 2 — Persist catalog narrative and profile projections

**Create:**

- `src/featuregen/overlay/upload/catalog_profiles.py`
- `src/featuregen/overlay/upload/profile_store.py`
- reserved migration `1047_catalog_profile_revision.sql`
- `scripts/reconcile_dataset_profiles.py`, modelled on the existing reconciliation scripts

**Modify:**

- `graph.py`
- graph rebuild/reprojection path

**Tests:** catalog-store CAS, immutability, projection and migration replay tests.

- [ ] Add immutable `catalog_profile_revision` and CAS `catalog_profile_current` tables.
- [ ] Enforce bounded text/list fields before persistence.
- [ ] Add table-node compatibility projections for `authority_role` and
  `temporal_storage_model`; projections are rebuildable and never authoritative.
- [ ] Do not add dataset profile revision/current tables.
- [ ] Do not project catalog defaults into dataset operational fields.
- [ ] Add a reconciliation report comparing field evidence/current decisions/profile read model/
  graph projections.
- [ ] Reproject durable table fields unconditionally after `build_graph`, including Pass-B-off
  uploads, and test that a re-upload cannot blank the Profile/search display.

**Exit:** catalog narrative has durable versioned state and table profiles have a rebuildable fast
display projection.

### Task 3 — Add upload and profile-edit surfaces

**Modify:**

- `src/featuregen/api/routes/uploads.py`
- `src/featuregen/api/routes/catalogs.py`
- asset/profile API routes
- `frontend/src/api.ts`
- upload and Asset Detail screens

**Tests:** route validation, transaction atomicity, OCC/CAS and frontend interaction tests.

- [ ] Add optional bounded `catalog_profile_json` to the multipart upload. Missing profile data
  never rejects an otherwise valid catalog.
- [ ] Validate the complete profile before opening writes.
- [ ] Commit an uploader-authored catalog revision only in the same successful transaction as the
  upload; a failed ingest leaves no current pointer change.
- [ ] Add `GET/PUT /catalogs/{source}/profile` with expected pointer version.
- [ ] Add `GET/PUT /catalog/table-profiles/{source}/{object_ref:path}`. Keep the literal segment
  before the greedy path; do not collide with the existing asset route.
- [ ] The table PUT is authorized by existing `catalog:write`, validates all fields and the expected
  aggregate `dataset_profile_hash` first, takes the
  existing source lock plus one table-ref advisory lock, then writes ordinary field evidence/
  decisions in one transaction. It returns the new assembled `dataset_profile_hash`.
- [ ] An uploader can edit descriptions and advisory classifications. Setting an operational
  authority/temporal value uses the current platform-admin confirmation seam; this slice does not
  create a new entitlement programme or pretend upload-catalog source ownership exists. Lack of a
  confirmation does not hide the proposed value or block sandbox use; an explicit Release-B serving
  or temporal policy is the alternative operational declaration.
- [ ] Extend `GET /catalogs` with display name and profile presence; keep counts read-scoped.
- [ ] Prove a table profile write resolves the real table logical ref (public and non-public schema)
  and changes the intended evidence/current projection; zero-row projection is an error, not 200.

**Exit:** users can provide and correct catalog/table meaning without re-uploading data.

### Task 4 — Extend Pass B with evidence-bound profile synthesis

**Depends on:** `SemanticContextBundleV1` from the semantic-enrichment plan.

**Modify:**

- `table_synth.py`
- `enrich_llm.py`
- `ingest.py`
- semantic-context purpose adapters

**Create:** `attest/dataset_profile_critic.py`

**Tests:** schema validation, dispositions, structured-result replay, critic and prompt-golden tests.

- [ ] Extend the existing Pass-B result; do not add an unconditional second LLM call.
- [ ] Register a real Pass-B/table-synthesis v3 output schema and prompt/input version before adding
  fields. The current v2 schema is a byte-alias of v1 with `additionalProperties: false` and will
  reject them.
- [ ] Extend `enrich_llm.py` item/profile/roster allowlists and sanitizers for every new context key;
  add golden egress tests and explicit `EGRESS_BLOCKED` dispositions.
- [ ] Suggest table description/business context, authority role and temporal storage model in
  addition to current table role/primary entity/event-snapshot fields.
- [ ] A source-authored table definition remains current. The LLM fills an absent description and
  may suggest an alternative for review; it does not paraphrase curated text into competing
  evidence.
- [ ] Every suggestion names existing evidence refs from the bounded table context.
- [ ] Persist accepted suggestions as normal `llm/proposed` field evidence and record a total
  per-field disposition: accepted, abstained, invalid, refuted, superseded, not-attempted.
- [ ] Persist accepted Pass-B structured output through the existing `structured_result` store with
  exact input hash and `llm_call` provenance; Release-A replay evaluation must not depend on output
  the current path discards.
- [ ] Run deterministic contradictions first: SCD2 without candidate boundaries, event fact with no
  time column, crosswalk with fewer than two identifier sides, authority claim with no supporting
  source context.
- [ ] Run an independent, proposal-blind critic only for authority, temporal and ambiguous/high-
  impact role claims. Compare the independent result outside the prompt.
- [ ] Keep descriptions and low-impact fields on the normal one-model path.
- [ ] LLM authority role remains proposed. It never becomes a production source-selection fact.
- [ ] Hash the exact context bytes, prompt/schema versions and profile vocabulary fingerprint with
  semantic Task 3 in the same change; a cache bump may not precede its payload.

**Exit:** the system can enrich table meaning at scale and visibly admit when evidence is weak.

### Task 5 — Make profiles visible and consumed

**Modify:**

- `search.py` and search API
- `asset_detail.py`
- Asset Detail Context dossier builder from the semantic-enrichment plan
- `feature_assist.py`
- `analysis/retrieval.py`
- relevant frontend screens

**Tests:** search facet/text, Asset Detail Context dossier, feature prompt and retrieval tests.

- [ ] Add facets for derived data role, authority role and temporal storage model. `data_role` is a
  read-time normalized CASE over the existing canonical `table_role` (including
  `bridge -> crosswalk`), not a second evidence field; extend search deliberately because current
  facets only read literal graph columns.
- [ ] Let table description/business context match table and column search results. Join the table
  projection at read time; do not copy the same prose into every column evidence record.
- [ ] Add table definition/business context to the table search document—the current table FTS slot
  is hardcoded blank. Test technical catalogs whose only table prose is `business_context`.
- [ ] Preserve read scope using the Task-0.6 derived table predicate: profile text is searchable only
  when the caller can see at least one table column and the matched result itself is visible;
  world-visible table nodes never authorize it.
- [ ] Add a Profile section showing value, authority, evidence, status and missing context.
- [ ] Add profile nodes/edges to the Asset Detail Context dossier section.
- [ ] Extend `SemanticContextBundleV1.table_context` with the assembled profile and
  `dataset_profile_hash`.
- [ ] Feature generation consumes data role, primary entity, description, authority label and
  temporal model as context/advisories. Existing numeric, currency, grain, availability and join
  gates remain authoritative.
- [ ] Data-agent retrieval uses description, business context, domain, data role and primary entity
  for semantic expansion and ranking.
- [ ] Data role produces useful warnings: dimension aggregation, snapshot/event mismatch and
  crosswalk-table-as-measure. It does not hard-refuse by itself.
- [ ] Search, feature and analysis payload tests must fail if profile context is disconnected.

**Exit:** every new field changes at least one real product behaviour and is visible with provenance.

### Task 6 — Release-A evaluation and activation gate

- [ ] Add a fixed gold set for event fact vs snapshot fact, customer dimension vs population,
  system-of-record vs replica, SCD2 vs current-only, real crosswalk vs ordinary reference, and
  honestly unknown tables.
- [ ] Compare pre-profile and profile-aware retrieval/feature proposals with deterministic fixtures
  and replayed structured results. A fresh provider/model comparison is a live LLM run and requires
  separate approval before it is attempted.
- [ ] Require improved table selection/retrieval and zero new unsafe physical assertions.
- [ ] Required mutations: collapse data/authority role; inherit catalog authority; let an LLM
  authority become load-bearing; drop profile context from search, feature generation or retrieval;
  use a graph projection as authority; omit a meaning-bearing field from
  `dataset_profile_hash`.
- [ ] Reuse the semantic Task-10 harness: literal count for named focused suites, must-die sentinel,
  must-survive no-op and anchor-cardinality checks. The whole-repository count is not a gate.
- [ ] Flag-off preserves current upload, search, asset-detail, feature-context and retrieval payloads;
  flag-on uses the new profile contract.
- [ ] Add `FEATUREGEN_DATASET_PROFILES` to example/deployment configuration and enforce the shared
  flag matrix. Present deploy/migration/flag details and stop for approval. Deployment approval does not
  authorize re-upload or a live LLM run.

---

## 8. Release B — Source and Temporal Correctness

### Task 7 — Add policy and decision contracts/persistence

**Create:**

- `src/featuregen/selection/__init__.py`
- `src/featuregen/selection/contracts.py`
- `src/featuregen/selection/source.py`
- `src/featuregen/selection/temporal.py`
- `src/featuregen/selection/serving_policy_store.py`
- `src/featuregen/selection/temporal_policy_store.py`
- `src/featuregen/api/routes/dataset_policies.py`
- reserved migration `1048_dataset_serving_temporal_policy.sql`

**Tests:** contract canonicalization, CAS, reference validation and migration replay.

- [ ] Implement §6.4 and §6.5 contracts.
- [ ] Keep reusable selection contracts/resolvers in the neutral `featuregen.selection` package;
  analysis and materialization adapt them. Neither execution stack imports the other merely to get
  a source decision.
- [ ] Persist immutable serving/temporal policy revisions with CAS current pointers.
- [ ] Validate every dataset and column ref exists and is readable when authored.
- [ ] Reconcile `data_agent.binding_store.record_binding` with
  `data_agent.physical.record_binding_revision`: one physical-ID/content-payload implementation,
  one transaction and one deterministic revision. A catalog-engine-derived binding is persisted
  when first selected, before any decision/observation/snapshot references it. Pin tests must prove
  explicit and derived paths cannot fork identity.
- [ ] A serving policy key includes `entity_id + need_role + serving_purpose`.
- [ ] Preferred dataset refs must be a subset of eligible refs. Empty or multiple equally preferred
  candidates remain an explicit ambiguity, never an ordering accident.
- [ ] A serving/temporal policy is itself an explicit operational declaration. It may operate when
  the profile field is absent or proposed; it refuses when a current load-bearing profile value
  contradicts it. This keeps upload-only catalogs functional without promoting an LLM proposal.
- [ ] No free SQL, live cutoff value, upload time or catalog watermark enters policy identity.
- [ ] Add closed refusal codes for population undeclared, source ambiguous, binding missing,
  authority insufficient, temporal model unknown, historical current-only, SCD overlap and snapshot
  tie across all actual consumers: `analysis.intent.UNRESOLVED_CODES`, the wire schema,
  `analysis.clarify`, `data_agent.learning.GAP_CODES` and `REFUSAL_TO_GAP`. Keep SCD overlap a data
  quality refusal, not an ontology-learning gap.
- [ ] Add read-scoped `GET/PUT` policy routes with required body-carried expected pointer version,
  existing platform-admin confirmation and a minimal Profile-screen editor for the
  current serving/temporal policy. A policy store with no authoring surface is dead code.

**Exit:** source and row choices are immutable, typed and separately explainable.

### Task 8 — Resolve dataset source and temporal rows

**Modify:**

- `analysis/plan.py`, `analysis/grounding.py`, `analysis/clarify.py`
- `data_agent/dimensions.py`
- `analysis/execution.py`
- planning adapters only where selection is consumed

**Tests:** selector truth tables and worked analysis fixtures.

- [ ] Population: require `explicit_dataset_ref` or an explicit serving policy. Never infer it from
  `primary_entity`, authority role or table name.
- [ ] Enforce the population declaration again at the plan-to-execution bridge. Remove the current
  `if plan.population_table_ref:` fail-open: a caller-supplied spine without a declaration refuses.
- [ ] Other needs: apply explicit request, then current serving policy, then a single unambiguous
  eligible candidate.
- [ ] Every selected dataset must resolve through `binding_store.resolve_table`; a semantic profile
  without a current authorized physical binding is explainable metadata, not an executable source.
- [ ] Thread `ExecutionTier` through analysis planning/execution (it is currently hardcoded to
  production). Sandbox may rank a unique LLM-proposed authority value with a visible
  `PROPOSED_AUTHORITY_USED` warning. Production may not.
- [ ] If equally eligible candidates remain, refuse/clarify; never break ties by upload time,
  lexical order or newest catalog.
- [ ] Implement current-record and latest-snapshot-as-of adapters explicitly; only SCD2
  report-cutoff attribution exists today. Latest snapshot uses cutoff plus governed tie-breakers and
  refuses ties; current-only never answers a historical request.
- [ ] Reuse half-open SCD semantics in `data_agent.dimensions`; do not create a second interval
  renderer.
- [ ] Preserve separate effective-time and availability-time predicates.
- [ ] Historical current-only/SCD1 without history resolves to `EXPLICIT_ONLY` and asks for another
  source or an explicit limitation.
- [ ] Record only decision/ontology refusals represented by the closed refusal-to-gap map through
  `data_agent.learning`; capability and data-quality failures remain refusals. Fix concurrent
  `record_gap`/repeated `resolve_gap` idempotency before production wiring.

**Worked acceptance:** for “customers whose transaction count decreased last completed month by
segment and sector,” the population remains the declared customer master, transaction events use
the event source and eligible status/reversal logic, and segment/sector use the SCD2 row valid at
the report cutoff. A customer with zero current transactions remains in the result.

**Exit:** the data agent cannot silently choose the wrong copy or today's dimension row.

### Task 9 — Seal, compile and explain Release-B decisions

**Modify:**

- `src/featuregen/api/routes/analysis.py`, analysis preview/execution contracts and
  `data_agent.analysis.run_analysis` caller wiring
- `feature_metadata_snapshot.py`
- materialization identity/run preparation only where feature execution consumes a selection
- lineage/context graph and frontend plan preview

**Tests:** replay, stale-policy, lineage and end-to-end analysis tests.

- [ ] Define `AnalysisExecutionIRV2` as V1 plus the exact source/row decision refs and a versioned
  JCS `plan_hash`; do not retain V1's delimiter-joined unversioned identity.
- [ ] Include exact `dataset_profile_hash`, serving policy revision, source selection, binding revision,
  temporal policy revision and row selection in analysis-plan identity.
- [ ] Persist the validated `AnalysisExecutionIRV2` plus these exact decision refs through the shared
  `structured_result` store; do not add a second analysis-plan replay table.
- [ ] Use the six shared snapshot kinds when feature/materialization consumes the same decision:
  `dataset_profile`, `serving_policy`, `source_selection`, `physical_binding`, `temporal_policy`,
  `row_selection`.
- [ ] Make `_execution_inputs_or_none` assemble validated inputs and call `run_analysis`; a preview
  that never executes does not satisfy Release B. Persist the validated IR/result through the
  existing structured-result store.
- [ ] Revalidate current pointers/bindings immediately before execution using the strict current
  pointer/binding reader pattern; a stale decision refuses.
- [ ] Render the selected source and row semantics in plan preview and result provenance.
- [ ] Explain alternatives considered and why they lost without exposing hidden catalogs.
- [ ] Do not claim historical outputs were corrected when a policy later changes.
- [ ] Present the Release-B pilot and stop for explicit approval before any Hive/ODS run.
- [ ] `FEATUREGEN_SOURCE_TEMPORAL_SELECTION=0` keeps the existing explicit analysis inputs;
  enabling it must not silently rewrite an already-stored plan.
- [ ] Generated feature/materialization acceptance remains blocked on codegen remediation plus the
  separately reviewed materialization-wiring slice. Release B may exit for the data-agent path only
  when the API has a real `run_analysis` caller and fixture execution passes.

---

## 9. Release C — Mapping-Table Crosswalks

### Task 10 — Add crosswalk definition and discovery

**Create:**

- `src/featuregen/overlay/upload/crosswalk.py`
- `src/featuregen/overlay/upload/crosswalk_store.py`
- reserved migration `1049_crosswalk_definition_execution_observation.sql`

**Modify:** profile synthesis/Asset Detail Context dossier/relationship read models.

**Tests:** identity, current pointer, discovery bounds and lifecycle tests.

- [ ] Implement `CrosswalkDefinitionRevisionV1` and the composition-only
  `CrosswalkExecutionRevisionV1` without changing existing bridge fact keys.
- [ ] Persist immutable revisions and a CAS current pointer.
- [ ] Import `RelationshipKind` from semantic Task 1 and use schema-preserving
  `LogicalMappingPairV1`; do not reuse bridge `ColumnPairV1` outside its flat-public contract.
- [ ] Discover candidates from both: a dataset profiled as crosswalk/bridge and an LLM structured
  suggestion grounded in identifier columns.
- [ ] Bound mapping tables and tuple candidates before any pairwise enumeration.
- [ ] `data_role=crosswalk` alone never creates an executable definition.
- [ ] A direct bridge and a crosswalk between the same endpoints remain distinct and visible.
- [ ] Transformed and semantic-only suggestions are discoverable but structurally non-executable.
- [ ] Human confirmation affects review/accountability, not availability or deterministic safety.

**Exit:** the catalog can represent “customer number maps to CIF through this table” honestly.

### Task 11 — Observe and admit exact crosswalk directions

**Modify:**

- `data_agent/relationship_observation.py`
- bridge/crosswalk admission adapters
- observation persistence

**Tests:** exact/approximate evidence, duplicates, overlap, direction and stale-dependency tests.

- [ ] Make the shipped V2 direct-observation executor persist through
  `record_relationship_observation` and feed bridge admission; the current V1 compatibility seam
  must not discard the V2 object.
- [ ] Profile both leg tuples and the mapping table under the exact scope/predicates.
- [ ] Resolve each leg through its real owner: same-catalog legs through `plan_join` and cross-
  catalog legs through one current `BridgeJoinRealizationRevisionV1`. Persist a `JoinLegPinV1` with
  ordered logical refs, side-specific binding revisions, read-set/predicate hashes, fact keys and
  the singular realization/dependency revision where applicable; a bare hash is not enough for
  replay or explainability. Do not pretend every leg is an entity bridge.
- [ ] Measure tuple completeness, duplicates, overlap coverage, unmatched rows and max fan-out in
  each direction.
- [ ] A sample may disprove uniqueness; only complete governed-key/source-constraint/exact evidence
  may establish it.
- [ ] SCD/snapshot mapping rows use the Release-B temporal policy before uniqueness is measured.
- [ ] Persist per-leg observations against exact realization/binding/partition/predicate identity
  and one `CrosswalkExecutionObservationV1` against the definition revision, ordered leg pins,
  mapping binding, temporal row-selection hash and composed fan-out. Do not force a three-table
  observation into the two-endpoint V2 store.
- [ ] Admit sandbox use with unassessed evidence only when the generated project carries an exact
  runtime gate and the result is marked sandbox.
- [ ] Production requires current deterministic safety for both legs and the combined mapping.
- [ ] Multiple active mappings refuse. No `dropDuplicates`, first-row or arbitrary tie-break repair.

**Exit:** crosswalk safety is measured rather than inferred from names or review status.

### Task 12 — Compile and render the two-leg crosswalk

**Hard predecessors:** codegen-remediation Tasks 1–26 and an approved implementation plan that
wires compile → authorize → render → prepare → submit → validate → publish. Neither is optional.

**Modify:**

- `materialize/joins.py`
- `materialize/expression_ir.py`
- render project/join-gate/compute nodes
- data-agent execution adapter

**Tests:** IR identity, read-set authorization, render golden, Spark fixture and mutation tests.

- [ ] Extend the IR deliberately to represent two independent ordered join legs plus mapping row
  selection. The current expression planner supports one same-catalog traversal **or** one
  cross-catalog realization; this is new planner architecture, not glue. Do not add an interpreter.
- [ ] Add every mapping table key/predicate/time column and both legs' endpoints/predicates to the
  physical read set. Gate 2 uses `visible_requires`, not raw sensitivity, and authorizes each
  catalog under its own identity.
- [ ] Pin both bridge/leg realization revisions, crosswalk definition revision, temporal policy,
  mapping binding and observation revisions.
- [ ] Render two joins; never replace them with direct endpoint equality.
- [ ] Run the target-tuple uniqueness/fan-out gate after mapping row filters and before feature or
  analysis aggregation.
- [ ] Preserve population-spine left joins and zero-event entities.
- [ ] Refuse reverse direction independently when its fan-out differs.
- [ ] A failure leaves the last published feature partition unchanged.
- [ ] Production cross-catalog execution also passes the existing durable live-activation decision
  and signed artifact check where configured. Discovery and sandbox planning remain separately
  labelled; human review is not substituted.

**Exit:** generated Kedro/PySpark can execute a real mapping-table relationship without silently
multiplying rows.

### Task 13 — Crosswalk UI, lineage, learning and release gate

**Modify:** relationship UI, Asset Detail Context dossier, plan preview, lineage, analysis learning.

- [ ] Label direct equality, crosswalk, transformed and semantic-only relationships distinctly.
- [ ] Show both crosswalk legs, mapping dataset, row policy, evidence, direction-specific
  cardinality and safety.
- [ ] Show “what already depends on this,” never the false claim “approval unblocks it.”
- [ ] Record feature/analysis use and every pinned revision in lineage.
- [ ] Route missing business decisions to clarification/open gaps. Fan-out/duplicates/overlap are
  data-quality or safety refusals, not automatic ontology gaps.
- [ ] Required mutations: treat crosswalk label as executable; render endpoint equality; omit one
  leg from read authorization; measure uniqueness before time filtering; invert cardinality;
  deduplicate; let review substitute for safety; omit mapping revision from identity.
- [ ] Hand-reconcile one direct bridge and one real mapping-table crosswalk before activation.
- [ ] Persist sandbox exact-probe/composition observations so a later production assessment can use
  them; do not discard the graduation evidence.
- [ ] `FEATUREGEN_CROSSWALK_EXECUTION=0` keeps crosswalks discoverable but structurally
  non-executable.
- [ ] Enforce `CROSSWALK -> SOURCE_TEMPORAL -> DATASET_PROFILES` at configuration/startup. Add all
  flags to the deployment/example surfaces before approval.
- [ ] Stop for explicit approval before a live profile, generated Hadoop run or catalog mutation.

---

## 10. Execution Order

```text
Joint Semantic/Profile Task 0
  -> Joint Task 0.5 verified interfaces
  -> Semantic shared Task 0.6 prerequisite seam fixes
  -> Semantic Tasks 1-2 + Release-A Tasks 1-3 (non-colliding files only)
       profile contracts, evidence fields, catalog narrative and edit surfaces
  -> jointly: semantic Tasks 3-4 + Release-A Task 4
       exact payload, schema registration and replay identity
  -> semantic plan Tasks 5-6
       adjudication, supersession and projection consistency
  -> jointly: Release A Task 5 + semantic plan Tasks 7-9
       one Context Graph, feature-context and data-agent-context implementation
  -> Release A Task 6 + semantic plan Task 10 evaluation
  -> Release B: Tasks 7-9
       explicit dataset need -> source choice -> temporal rows -> real run_analysis caller
  -> Hive/ODS observation capability for pilot datasets
  -> codegen-remediation Tasks 1-26
  -> separately reviewed materialization wiring slice
  -> Release C: Tasks 10-13
       crosswalk definition -> measurement -> two-leg generated execution -> UI/lineage
```

Release A is the immediate implementation target. Release B begins after Release-A
`dataset_profile_hash` values and authority semantics are frozen. Release C execution begins only
when real or fixture mapping
data can exercise duplicate, overlap and directional fan-out cases and both codegen/wiring
predecessors are complete.

---

## 11. Definition of Done

### Release A

1. Every visible table returns one effective semantic profile assembled from existing evidence and
   specialized facts.
2. Catalog narrative is versioned; dataset fields do not gain a duplicate revision store.
3. Data role and authority role remain separate and visible with provenance.
4. LLM suggestions improve profile coverage but never become hidden physical or source authority.
5. Profile text and classifications are consumed by search, the Asset Detail Context dossier,
   feature generation and
   data-agent retrieval.
6. Users can correct profile fields without re-uploading a catalog.

### Release B

7. Dataset need distinguishes population, event, dimension, reference and mapping roles.
8. Population is explicit; authority role never silently decides completeness.
9. Source and row selection are separate, deterministic and explainable.
10. Historical analysis never falls back to current-only rows.
11. The worked decreasing-transactions question preserves zero-event customers and uses PIT segment
    and sector attribution.
12. Selection/profile/policy revisions are sealed into replay and lineage where consumed.
13. The analysis API assembles execution inputs and invokes `run_analysis`; Release B is not a
    preview-only path.

### Release C

14. Existing direct bridge identities remain unchanged.
15. A crosswalk explicitly names its mapping dataset, both tuple legs and temporal row policy.
16. LLM-only crosswalk candidates remain discoverable and sandbox-plannable without human approval.
17. Production execution depends on current deterministic, direction-specific evidence—not review.
18. Multiple mappings/fan-out refuse instead of being silently deduplicated.
19. After codegen remediation and production wiring, generated Kedro/PySpark reads the mapping
    dataset and applies both join legs.
20. UI and lineage distinguish direct, crosswalk, transformed and semantic-only relationships.
21. Production cross-catalog execution passes deterministic safety plus the existing durable/
    signed activation interlock; discovery remains available without human approval.

### Global

22. No OpenMetadata, new graph database, second LLM replay store, second physical identity, second
    relationship safety model or second execution interpreter is introduced.
23. No deployment, catalog upload/re-upload, live LLM call or Hive/ODS query occurs without the
    user's explicit approval.
24. Profile and binding revisions replay metadata/configuration, not immutable source contents,
    unless an exact source snapshot/partition digest is separately pinned.

---

## 12. Explicitly Deferred

- OpenMetadata and other external catalog connectors;
- OpenMetadata table-description import—the current connector drops it, but the connector is outside
  this functionality-first slice;
- source delivery-SLA facts and automatic dependencies-ready triggering;
- period-end-per-period and transaction-event-time dimension attribution beyond the initial current,
  SCD2-report-cutoff and snapshot-as-of policies;
- transformed-link execution until a closed, versioned normalization vocabulary is designed;
- many-to-many allocation policies;
- automatic ontology mutation from LLM gap suggestions;
- operational/audit serving-purpose variants until a real consumer and system-of-record contract
  exist;
- a general typed dependency-kind/invalidation vocabulary beyond the exact source/row/crosswalk
  pins defined here;
- demand ranking over `open_gaps` until the analysis path has a production gap producer;
- fuzzy entity resolution using names, addresses or personal data;
- scheduler/worker, retries/outbox, multi-tenancy, quotas, mTLS, secret-manager and disaster recovery;
- automatic restatement/demotion of already-published artifacts;
- ownership, usage, conversation, memory and data-product adapters until real sources exist;
- full ontology/ER exploration product and unrestricted multi-hop traversal.
