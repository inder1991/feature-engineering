# Verified Interfaces — Semantic Context, Dataset Profiles and Crosswalks

Date: 2026-08-01  
Code authority: `origin/main@fa9a20b0` until the joint read-only Task 0 records a reviewed successor  
Status: **BINDING ADDENDUM FOR THE TWO 2026-08-01 PLANS**

This document is the output required by joint Task 0.5. It resolves the cross-plan contracts that
must have one owner and records the main-branch limitations the implementation must not describe as
working functionality. If a plan and this document disagree, stop and amend both before coding.

## 1. Verified Baseline Corrections

The following are present as libraries or test seams but have no production caller on the verified
baseline:

- the materialization chain from `compile_ir` through render, prepare, submit and publish;
- `data_agent.analysis.run_analysis`—the API assembly seam returns `None`;
- V2 relationship-observation persistence and bridge admission—the production executor returns a
  V2 value through a V1 compatibility wrapper and does not persist/admit it.

The 2026-07-31 code-generation adversarial findings remain open on this baseline. Therefore:

1. Release A may proceed after Tasks 0/0.5 and the shared prerequisite fixes below.
2. Release B may build its contracts and make the existing data-agent route reach `run_analysis`,
   but cannot claim an executed feature/materialization acceptance until the generated-code
   remediation and execution wiring gates pass.
3. Release C compilation/execution cannot start until
   `2026-07-31-codegen-review-remediation.md` Tasks 1–26 pass and a reviewed materialization-wiring
   slice connects compile → render → validate → submit → publish. The codegen plan explicitly
   excludes this wiring in Phase G; completing that plan alone is necessary but insufficient.

## 2. Shared Contract Ownership

| Contract/decision | Canonical owner | Rule |
| --- | --- | --- |
| `RelationshipKind` | semantic-context Task 1 | Profile Release C imports it; no second enum |
| `dataset_profile_hash` | profile Release-A Task 1 | This exact name is used in bundles, selections, snapshots and lineage |
| `JoinLegPinV1` | profile Release-C Task 11 | Semantic context exposes it only for a crosswalk execution that exists |
| evidence authority | existing `EvidenceProducer` + `AssertionStrength` + `EvidenceLifecycle` | Do not flatten into a new five-value authority enum |
| operational influence | existing `OperationalColumnFacts.authority` (`governed|hint`) | Separate from evidence provenance; never encode one as the other |
| relationship availability/safety | existing bridge assessment/realization readers | Review, availability, safety and lifecycle remain independent axes |
| content hashing for new contracts | shared JCS contract hasher described below | Do not pick an inline `json.dumps` variant |
| migration allocation | this document | No task independently chooses “the next number” |

### Evidence-bearing semantic value

`SemanticValueV1` carries the real axes rather than a lossy authority label:

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
```

`SOURCE/PROPOSED`, parser/profiler/taxonomy/legacy evidence and stale/rejected/superseded evidence
are all representable. LLM proposals travel as `LLM/PROPOSED/ACTIVE`; they remain usable context
without being relabelled as an operational fact.

### Relationship context mirrors the current one-to-many model

```python
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
```

The last two fields are kind-dependent and stay `None` before Release C. They are not fabricated for
direct bridges. One symmetric link can carry zero, one or two current directional realizations.

### Observation context is a faithful V2 projection

`ObservationContextV1` is serialized from `RelationshipObservationV2` and the current-row envelope.
It preserves, without collapsing sides:

- `observation_revision_id`, `realization_revision_id`, `plan_hash` and `scope_id`;
- left and right endpoint observations, including each side's binding revision, columns,
  partitions/snapshot and tuple metrics;
- all matched/unmatched/orphan/amplification metrics;
- `normalization_ids`, `predicate_ids`, source snapshot IDs and `snapshot_or_as_of`;
- `execution_principal`, `method`, `row_coverage`, `complete`, `observed_at`, failures, producer and
  strength.

It does not invent `observation_kind`, a single `direction`, `expires_at`, a lifecycle column or a
combined binding tuple. Currentness comes from the observation-current pointer; applicability comes
from exact equality of the realization, scope and side-specific binding/predicate identity.

Crosswalk composition is not forced into this two-endpoint record. Release C owns a minimal
two-leg `CrosswalkExecutionObservationV1` that pins both leg revisions plus the mapping binding and
measures the composed fan-out after temporal filtering.

## 3. Canonical Hash Scheme

`src/featuregen/canonical.py` owns two helpers: `jcs_sha256(payload)` for raw RFC-8785 JCS bytes plus
SHA-256, and `contract_hash_v1(contract_name, contract_version, payload)` which injects the required
name/version envelope before calling it. All new semantic/profile/source/temporal/crosswalk
contracts use `contract_hash_v1`. Implementation extracts the neutral JCS helper from the existing
materialization implementation and makes `materialize_hash` delegate byte-identically to
`jcs_sha256`; existing materialization payloads therefore do not gain a new envelope. Existing
`field_evidence.canonical_hash` values are not migrated or recomputed.

Meaning-bearing declarations and immutable revision IDs enter hashes. Current time, job state,
projection timestamps, live watermarks and environment-specific observations do not. Physical
binding revision enters request/execution identity, not `dataset_profile_hash`.

Catalog narrative handling is explicit:

- `catalog_profile_revision_id` is provenance/current-pointer identity;
- the narrative semantic `content_hash`, not the revision ID, enters `dataset_profile_hash`;
- re-authoring byte-identical narrative does not re-key all datasets;
- changing narrative meaning does.

Profile/source naming is canonical:

- `dataset_profile_hash`—never `profile_hash` or `selected_profile_hash`;
- `selected_dataset_profile_hash` inside a source-selection decision.

## 4. Snapshot Item Contract

Before any new metadata pin is consumed, `feature_metadata_snapshot.py` must:

1. reject unknown requested item fields/kinds instead of silently dropping them;
2. include `item_kind` and a kind-specific identity payload in `item_hash`;
3. deduplicate by `(item_kind, item_hash)`;
4. dispatch comparison to a kind-aware current reader rather than rebuilding every item as
   `column_field`;
5. report `SNAPSHOT_KIND_UNSUPPORTED` for an unavailable reader, never permanent false drift.

The Release-B pin set has exactly six kinds:

1. `dataset_profile`;
2. `serving_policy`;
3. `source_selection`;
4. `physical_binding`;
5. `temporal_policy`;
6. `row_selection`.

Release C adds `crosswalk_definition`, `crosswalk_execution`, the two ordered `join_leg` pins and
the applicable crosswalk-composition observation.

## 5. Migration Reservations

The migration runner is ledgered by full filename and applies files lexically. Duplicate numeric
prefixes already exist at 0973, 0974, 1034, 1036, 1037, 1038 and 1040. Baseline/live state must be
recorded as the applied filename+checksum set, not only the largest number.

Reservations from the verified `1043` source head are:

| Number | Reserved filename/purpose | Owner |
| --- | --- | --- |
| 1044 | `1044_run_event_ordering.sql` | codegen-remediation Task 15 |
| 1045 | catalog semantic/issuer-scope revision | semantic Task 2 |
| 1046 | semantic adjudication current pointer | semantic Task 5 |
| 1047 | catalog narrative revision/current | profile Task 2 |
| 1048 | dataset serving + temporal policy revisions | profile Task 7 |
| 1049 | crosswalk definition/execution/composition observation | profile Tasks 10–11 |
| 1050 | feature candidate tournament run/result | future multi-LLM tournament Task 6; recheck after 1044–1049 merge |

If `origin/main` acquires any of these names before implementation, stop and update this table and
all three plans together. Do not create another duplicate prefix.

## 6. Functional Authorization Paths

The new fields have explicit paths:

- `business_context` is recommendation-only. A caller with existing `catalog:write` may append a
  bounded `HUMAN/PROPOSED/ACTIVE` advisory value through a dedicated `propose_advisory` action.
  It is visible but never load-bearing; do not mint `HUMAN/CONFIRMED` merely to avoid review.
- `authority_role` and `temporal_storage_model` are registered operational fields with
  `human_editable=True`. LLM or uploader proposals are visible. Existing platform-admin
  four-eyes confirmation can make a value load-bearing.
- Release B serving/temporal policies are also explicit operational declarations. A policy may
  operate when profile authority is absent/proposed; it refuses only when a current load-bearing
  profile value contradicts it. This keeps upload-only catalogs functional.
- Deterministic temporal-profile probes are deferred to the approved Hive/ODS observation slice;
  they are not silently claimed by Release A.

## 7. Egress and Schema Registration

Every purpose adapter is paired with an explicit change in `overlay/upload/enrich_llm.py`:

- classify every new top-level and nested key as prose, bounded structural data or evidence wrapper;
- accept the real evidence-axis shape above rather than a fabricated `llm_proposed` authority token;
- add golden redaction/egress tests for every new key and block raw samples;
- record an explicit per-item disposition and stage detail for every egress refusal;
- register a real feature-context v4 input contract before any caller requests version 4;
- register a real Pass-B/table-synthesis v3 output schema before new fields are accepted;
- persist accepted Pass-B structured output in the existing `structured_result` store if replay is
  used in evaluation.

No plan may say “reuse the sanitization seam” without naming these changes. A blocked batch item is
not retried in the same run, but it is counted and explained; zero output can never appear as a
successful stage.

## 8. Read Scope, Projection and Known Prerequisite Fixes

Before profile edit/search/context work:

1. fix `logical_ref_of` so two-part table refs remain tables instead of phantom columns;
2. derive table visibility from the existence of at least one visible column and apply it to table
   profile reads/search; a world-visible table node is not an authorization anchor;
3. thread caller roles/read scope into stored semantic-context builders and batch readers;
4. reproject durable table evidence after every graph rebuild even when Pass B is disabled;
5. fix concept-cycle validation and make semantic-context reads batched rather than N×M queries.

Gate 2 must use `visible_requires`, authorize every predicate and every leg endpoint, and refuse an
unknown governed ref. This is owned by codegen-remediation Tasks 10/12 plus the Release-C multi-leg
extension, not by a prompt adapter.

## 9. Rollout Flags and Activation

One parser owns truthy flag values. The supported matrix is:

| Profiles | Source/temporal | Crosswalk | Meaning |
| --- | --- | --- | --- |
| 0 | 0 | 0 | current behavior |
| 1 | 0 | 0 | Release-A display/retrieval context only |
| 1 | 1 | 0 | Release-B explicit source/row decisions |
| 1 | 1 | 1 | Release-C crosswalk discovery plus eligible execution |

`CROSSWALK=1` with `SOURCE_TEMPORAL=0`, or either downstream flag with `PROFILES=0`, fails startup
configuration validation. `FEATUREGEN_FEATURE_CONTEXT=0` rolls back to the currently shipped v3
path, not the old v1 thin menu; a separate legacy test may preserve v1 bytes but is not the rollout
switch. All flags must be present in documented example/deploy configuration before an approval
gate can enable them.

Cross-catalog discovery remains visible and honestly labelled without signed activation. Production
cross-catalog execution/promotion must additionally pass the existing
`FEATUREGEN_INTENT_LIVE_CROSS_CATALOG` durable activation and, where deployed, its signed Ed25519
artifact check. Review status is still not an execution gate.

## 10. Executable Evaluation Gates

Pre-deploy gates use deterministic fixtures, fakes and previously stored structured results only.
A same-provider/model live comparison belongs after the explicit live-LLM approval STOP.

The shared mutation harness is created explicitly by semantic Task 10. It has:

- a focused-suite baseline count, not the contaminated whole-repository count;
- one must-die sentinel;
- one must-survive no-op proving the suite actually ran;
- anchor-cardinality checks so one mutation cannot match twice silently.

Release claims name the actual environment: generated-project L0 runs only through the codegen
plan's pinned Kedro/Spark environments, and cluster/Hive acceptance requires a separate approval.

## 11. Non-Claims and Deferred Work (see also §12)

- Profile/binding hashes do not prove immutable source contents; replay is metadata/configuration
  replay unless a source snapshot or partition digest is pinned.
- OpenMetadata, owner/steward directories, general transforms, M:N allocation, delivery SLAs and a
  new graph database remain deferred.
- OpenMetadata table-description loss, sandbox relationship-observation persistence, operational/
  audit serving purposes, typed dependency kinds, demand ranking over open gaps and deterministic
  temporal profiler probes are recorded deferrals—not silently deleted scope.
- No deployment, catalog upload/re-upload, live LLM call, Hive/ODS query or generated cluster run is
  authorized by this document.

## 12. Companion Freeze: Suggested Feature Semantic Discovery (Task 0F, 2026-08-03)

The suggestion-discovery plan's Release-A contracts are frozen in the companion document
`2026-08-03-verified-interfaces-suggestion-discovery.md` (same binding force as this ledger; kept
separate only because its content is larger than this entire document). Cross-plan facts recorded
there that amend or extend this ledger:

- **Baseline successor recorded** (per this document's header clause): branch
  `worktree-suggested-feature-semantic-discovery` at `30f8442b` = `origin/main f3424c36`
  (2026-08-03) + one docs commit; `fa9a20b0` is an ancestor.
- **§5 update**: `1044_run_event_ordering.sql` now exists on main — reservation consumed as
  reserved. 1045–1050 unchanged. The suggestion-discovery plan reserves nothing for Release A
  (in-repo registries, on-demand reads); its Release-B persistence must allocate **≥ 1051** by
  amending §5 first.
- **§2/§3 handoff**: `featuregen/canonical.py` (`jcs_sha256` + `contract_hash_v1`) and
  `EvidenceAuthorityV1` (at `featuregen/contracts/evidence_axes.py`) will be landed by the
  suggestion plan's Task 0S; semantic Task 1 and every later plan **import** those definitions —
  the one-owner rule of §2 stands, with the first-lander owning the file.
- New frozen owners: discovery metadata registry
  (`overlay/upload/taxonomy/discovery_metadata.py`), feature-category and recipe-family
  registries (Task 1), `GroundingDecisionTraceV1` producer seam (feature_assist/_validate_idea +
  join_path + gate1 — `suggestions.py` never reconstructs it), `SuggestionReadScopeV1` derived
  solely from `read_scope.allowed_classes` (3 classes ⇒ 8 canonical tuples, import-gated).
