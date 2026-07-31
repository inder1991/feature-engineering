# Bridge Cardinality and Identifier-Link Trust Remediation

Date: 2026-07-29
Status: Finalized for execution against integration baseline `1fb8f81f`. Implementation begins with
Task 0 and the immediate Task 0B fail-closed patch; downstream contract work is gated on Task 1
freezing the corrected contracts below.
Scope: Functional correctness, production data-correctness and user-visible functionality. Security
and general operational NFR hardening remain deferred.

> **For agentic workers:** execute this plan task by task. Do not implement it independently from
> `2026-07-28-product-roadmap-and-contract-ownership.md`; that document continues to own the product
> release sequence and link-tier policy.

## Goal

Make cross-catalog identifier links useful and trustworthy without requiring a human to approve
every proposed link:

1. LLM-only concepts may still create visible link candidates.
2. The system must distinguish entity type, identifier namespace, population relationship and key
   role.
3. A symmetric identifier link must not silently claim a directional join cardinality.
4. Directional cardinality must come from the complete governed key, exact data evidence, or remain
   explicitly unknown.
5. An active proposed `entity_bridge` is available immediately to discovery, feature generation,
   data-agent planning and marked sandbox work. Human confirmation is never an availability or
   execution prerequisite. Production publication requires an automatically validated directional
   realization with known, non-fan-out cardinality and current deterministic evidence.

This plan is deliberately narrower than the full E3/E5 ontology programme. It fixes the bridge
substrate that feature generation and the data agent already consume. It does not build the optional
ontology explorer, a general entity-resolution engine, or a new execution runtime.

## Relationship to Existing Plans

- `2026-07-28-product-roadmap-and-contract-ownership.md` remains authoritative for contract
  ownership and the three use tiers.
- This plan **replaces the bridge-admission and planner portions** of
  `2026-07-28-e0b-identifier-link-admission.md` where they conflict:
  - replace E0b Task 2's metadata-only bridge grounding with Tasks 2–5 here;
  - replace E0b Task 3's binary population verdict with the population-relation contract in Task 1;
  - replace E0b Task 5's admission fusion with Task 8;
  - replace E0b Task 6's shared `active_bridges` set with the available-link reader introduced in
    Task 1 and the executable-realization reader completed in Task 9.
- For `entity_bridge`, the roadmap's production phrase “VERIFIED link, or policy-based automatic
  attestation” is implemented through the second branch: a proposed generic overlay fact may back an
  executable realization once deterministic policy validates that exact direction and scope. Generic
  overlay human-confirmation provenance is optional review metadata; folded `VERIFIED` may also be
  source-declared and is not itself proof of human review. Neither is an execution gate.
- Keep E0b's governance route, content-addressed LLM replay, provenance requirement and review UI,
  but implement them against the contracts in this plan.
- Reuse the data-observation workflow in
  `2026-07-28-data-powered-ontology-analysis-agent-program.md`; Task 7 extends its relationship
  evidence and does not create a second profiler.
- Honour the roadmap's single-owner rules:
  - the existing layered `PhysicalObjectIdentityV1` + `PhysicalDatasetBindingV1` own physical
    identity and read binding;
  - the bridge family must become the first implemented family of the shared candidate
    identity/revision/currentness substrate, not accumulate another bridge-only mutable ledger;
  - `llm_call` remains the immutable call audit, while Task 8 must identify or add the one shared
    content-addressed structured-result API before using it.

## Verified Deployed Baseline

Verified read-only against the `kind-featuregen` cluster on 2026-07-29:

| Observation | Result |
| --- | --- |
| `OVERLAY_PASS_C` | `1` |
| `OVERLAY_ENTITY_BRIDGES` | `1` |
| Sources | `cib`, `ftr` |
| Tables per source | one each |
| Pass-C candidate rows | `0` |
| Graph join edges | `0` |
| Cross-catalog bridge candidates | `9` |
| Bridge candidates with cardinality | `0` |
| Bridge lifecycle | nine `OVERLAY_FACT_PROPOSED`, no confirmations |
| Projected `entity_bridge_edge` rows | `0` |
| Open bridge human tasks | `9` |

The deployed Customer bridge is:

```text
cib.bo_cib_customer.cust_num
  <->
ftr.comp_financial_tran_repos_dly.cif_id
```

The confirmed CIB grain is:

```json
{"columns": ["business_dt", "cust_num"], "is_unique": true}
```

Therefore `cust_num` alone is not a governed unique key. The honest initial verdict is:

```text
identifier link: proposed
cardinality: unknown / requires a business-date or snapshot predicate
```

The bridge candidate recorded both grain flags as `false` before the CIB grain was confirmed. The
current graph now marks `cust_num` as a grain member. `cross_catalog_links()` works around that one
field by reading grain membership from the live graph, so the displayed grain booleans are no longer
stale. The durable candidate evidence is still never re-evaluated, and its `data_type_family`,
`type_basis` and other evidence remain frozen. The exact cause is in `bridge_propose.py`: a benign
duplicate denial returns before the ledger upsert, and the existing upsert does not update
`evidence_json`, `data_type_family` or `derivation_version`. Task 6A fixes that mechanism rather than
relying on the live-grain display workaround.

Eight deployed Branch candidates also demonstrate that identical entity/type metadata is too weak:
branch codes and branch names were paired with both `tran_branch_sol_id` and `sol_desc`.

## Verified Code Baseline and Immediate Exposure

Verified on 2026-07-29 against `.claude/worktrees/integration` at
`1fb8f81fa5283b3a9058cfb341d5d95bfe7158ae`:

- the active checkout does not contain `data_agent/`, `materialize/`, `analysis/grounding.py` or
  `cross_catalog_links.py`; the integration worktree contains all four, so Task 0's baseline warning
  is valid;
- `public.<table>.<column>` is the platform's intentionally flattened **logical** upload namespace.
  Bridge facts and `overlay_fact_dependency` must keep that namespace. Physical Hive
  database/schema/table identity is resolved later through `PhysicalObjectIdentityV1` and
  `PhysicalDatasetBindingV1`;
- parameterized-type stripping, declared-type fallback and `type_basis` are already implemented in
  `bridge_candidates.py`; this plan preserves and regression-tests them rather than “porting” them;
- `propose_fact` already accepts raw evidence after its denial checks, but that path currently
  defaults the record to `profiler/supported` and omits bridge producer provenance. Task 6A extends
  the payload before switching bridge proposals, so eliminating orphan evidence does not launder its
  authority;
- `active_bridges()` already returns `DRAFT`/proposed and `VERIFIED` links together;
- `_hop_evidence()` still converts every found bridge into `Cardinality.MANY_TO_ONE`. Because the
  available set already includes all nine proposed candidates, this is a live data-correctness
  exposure, not future Task-9 cleanup;
- expiry and drift do not demote `entity_bridge_edge`; `cross_catalog_links._blocked()` omits
  `REVERIFY` and treats an unreadable fact as allowed; `analysis/grounding.py` and contract
  invalidation read the stale VERIFIED projection directly.

Task 0B closes those live fail-open paths before the new contracts are built.

## The Eight Bridge Improvements This Plan Must Deliver

| # | Current problem | Required outcome | Owning tasks |
| --- | --- | --- | --- |
| 1 | Same entity is treated as the same identifier namespace | Separate entity, namespace and population decisions | 1, 3, 8 |
| 2 | LLM concept authority is hidden | LLM-only candidates remain allowed but visibly labelled | 1, 3, 8, 10 |
| 3 | Planner assumes bridge `N:1` without proof | Immediately fail closed, then use directional evidence-backed cardinality | 0B, 1, 5, 7, 9 |
| 4 | No pairwise data evidence | Bounded overlap, uniqueness, orphan and fan-out probes | 7, 8 |
| 5 | Flat logical refs are mistaken for physical Hive addresses | Keep logical bridge/fact/dependency refs flat; resolve database/schema through the physical binding | 1, 2 |
| 6 | Type evidence can be stale even though parameterized and declared-only type parsing is already shipped | Preserve the shipped resolver and refresh versioned type evidence | 2, 6 |
| 7 | Rejected, expired, stale or newly-informed bridges can leave stale projections/evidence | Immediate authoritative-state gate and demotion plus targeted re-assessment | 0B, 6 |
| 8 | Candidate enumeration is unbounded and scalar-only | Bounded shortlist and predicate-aware execution mappings | 4, 5 |

The scalar `entity_bridge` fact remains the identifier-namespace assertion for this slice. Task 5
may attach additional equality/predicate columns to an **execution realization**. It does not
synthesize independent scalar bridge facts into an E3 composite identifier namespace.

## Architectural Decisions

### A. Link and join realization are different objects

An identifier link is symmetric:

```text
cib.cust_num <-> ftr.cif_id
```

A physical join realization is directional:

```text
ftr.(business_dt, cif_id)
  -> cib.(business_dt, cust_num)
cardinality = N:1
```

Do not add `cardinality` to `EntityBridgeRef` or hash it into the symmetric bridge `fact_key`.
Confirming the identifier link confirms namespace equivalence only. It must not implicitly confirm a
join realization or its cardinality. A review action may confirm both only when the UI and command
payload name both decisions and their evidence separately.

### B. Cardinality is not an LLM decision

The LLM may:

- recognize likely namespace equivalence;
- propose a non-authoritative `population_hypothesis` for review;
- point out semantic contradictions;
- suggest one of a closed set of normalizations or predicate candidates for deterministic testing.

The LLM may not select the governed population relation, assert uniqueness, cardinality, fan-out or
operational eligibility. Those come from governed facts and deterministic observations evaluated by
versioned policy. A human may review the business assertion, but human review neither supplies nor
replaces execution safety. An LLM population hypothesis may explain or prioritize a candidate but
may not select a more permissive automatic-validation threshold.

### C. LLM-only links are still created

The following is valid:

```text
status: proposed
strongest_evidence_label: llm_only
namespace_verdict: possible
cardinality: unknown
```

It is available to search, ontology discovery, feature suggestions, data-agent planning and
directional-realization construction. It may run in a marked sandbox immediately. It may also support
production publication after the exact directional realization—not the symmetric link—passes
automatic cardinality, fan-out, scope and dependency checks. It is not silently described as
source-attested, human-reviewed or data-verified.

### D. No single optional semantic field is mandatory

Definitions, synonyms, names, domain and taxonomy are corroborating signals. Missing taxonomy or
synonyms does not reject a link. Many weak positive signals may not cancel a hard contradiction.

### E. Complete-key rule

A column that belongs to a composite grain is not unique by itself. The system may claim endpoint
uniqueness only when the proposed endpoint tuple equals a complete governed unique key, a governed
source constraint proves it, or an exact scoped data observation proves it.

### F. Stable identity, revision identity and live evidence

`realization_id` identifies one logical execution shape and includes:

- bridge fact key;
- direction;
- physical endpoint/binding identities;
- directional equality mappings;
- parameterized predicate contract.

`realization_revision_id` identifies one conclusion about that shape and additionally includes:

- derived cardinality classification and basis;
- derivation/admission policy versions;
- dependency snapshot.

It excludes live job status, current watermark, observation timestamps and observed metric values.
A new classification creates a new revision of the same logical realization. `fanout_observed` is an
evidence metric, not a stable realization field.

### G. Persistence and decision axes

The symmetric link fact, its assessment and a directional realization are separate durable objects:

1. `entity_bridge` is the governed symmetric namespace proposal/assertion. Folded generic overlay
   states `DRAFT`, `PARTIALLY_CONFIRMED` and `VERIFIED` are available; `REJECTED`, `REVERIFY` and
   `STALE` are not. `OVERLAY_FACT_PROPOSED`, `OVERLAY_FACT_EXPIRED` and
   `OVERLAY_FACT_STALED` are event names, not folded statuses.
2. A candidate has immutable assessment revisions plus one CAS-protected current pointer.
3. A directional realization has immutable revisions plus one CAS-protected current pointer.
4. Deterministic safety validation and optional human review are independent decisions over a named
   realization revision. Neither is inferred from a projection row, and human review is never
   required for deterministic validation.
5. Visible and executable projections are rebuildable read models, never the source of truth.

Execution safety, optional review and lifecycle are independent:

```text
safety_status = unassessed | deterministically_validated | unsafe
review_status = unreviewed | human_verified
lifecycle     = active | stale | rejected | superseded
```

Execution eligibility is derived from `safety_status`, lifecycle, evidence currentness, scope and
policy. It never depends on `review_status` and is not a manually writable status. Human verification
can improve ranking and explanation, but it cannot make an unsafe or unassessed realization
executable.

### H. Scoped proof never becomes an unrestricted claim

An exact profile proves only the physical bindings, partitions, predicates, normalization and
snapshot/as-of scope it actually read. A target unique inside one daily partition is not globally
unique. The realization carries a parameterized applicability contract; live partition values stay
in the observation/execution record and do not churn stable identity.

## Core Contracts

Task 1 owns the exact Python definitions, but the shape and separation are fixed here.

```python
IdentifierColumnMemberV1(
    logical_column_ref,
    physical_column_id,      # optional for discovery; required for execution
    data_type_family,
    type_basis,              # attested | declared | mixed
    key_member_role,
)

IdentifierEndpointV1(
    logical_table_ref,
    physical_table_id,       # optional in discovery; includes source + database + schema + table
    binding_revision_id,     # optional in discovery; required on an executable realization
    members,                 # ordered, non-empty IdentifierColumnMemberV1 tuple
    logical_identity_basis,  # platform_flattened
    physical_identity_basis, # governed_catalog | declared_mapping | unresolved
    entity_id,
    concept,
    concept_authority,       # source | human | deterministic | llm | unknown
    tuple_key_role,          # complete_unique_key | composite_member | foreign_key |
                             # alternate_key | non_key | unknown
)

IdentifierLinkAssessmentV1(
    candidate_id,
    candidate_revision_id,
    bridge_fact_key,
    left_endpoint,
    right_endpoint,
    namespace_verdict,       # same | different | possible | unknown
    governed_population_relation, # same | left_subset | right_subset | partial_overlap |
                                  # disjoint | unknown
    population_hypothesis,   # advisory LLM output; never an authority input
    evidence_refs,           # typed, composable references
    hard_conflicts,
    explanation_codes,
    assessment_version,
)

IdentifierLinkAvailabilityV1(
    bridge_fact_key,
    candidate_revision_id,
    availability,           # available | unavailable
    folded_status,          # DRAFT | PARTIALLY_CONFIRMED | VERIFIED |
                            # REJECTED | REVERIFY | STALE
    unavailable_reason,     # rejected | reverify | stale | superseded | unreadable | None
    review_status,          # unreviewed | human_verified; display/ranking only
    overlay_head_event_id,
)

BridgeJoinRealizationRevisionV1(
    realization_id,
    realization_revision_id,
    bridge_fact_key,
    from_endpoint,
    to_endpoint,
    column_pairs,            # ordered equality mappings
    predicates,              # non-equality structured predicates; no free SQL
    applicability_scope,
    cardinality,             # one_to_one | many_to_one | one_to_many |
                             # many_to_many | unknown
    cardinality_basis,       # governed_key | source_constraint | exact_profile |
                             # approximate_profile | metadata_inference | none
    evidence_refs,
    dependency_snapshot_id,
    derivation_version,
    admission_policy_version,
)

BridgeRealizationCurrentV1(
    realization_id,
    realization_revision_id,
    safety_status,           # unassessed | deterministically_validated | unsafe
    review_status,           # unreviewed | human_verified; never an execution gate
    lifecycle,               # active | stale | rejected | superseded
    pointer_version,         # compare-and-set
)

RelationshipObservationScopeV1(
    left_binding_revision_id,
    right_binding_revision_id,
    left_partition_selector,
    right_partition_selector,
    predicates,
    normalization_ids,
    snapshot_or_as_of,
    execution_principal,
    method,                  # exact | approximate
    row_coverage,            # full | sampled | partial
    complete,
    actual_partitions_read,
)

BridgeDependencySnapshotV1(
    snapshot_id,
    source_revision_vector,
    physical_binding_revisions,
    grain_fact_heads,
    concept_and_type_evidence_ids,
    predicate_column_revisions,
    registry_fingerprints,
    normalization_version,
    admission_policy_version,
)
```

Population relation is not binary. A cardholder table may be a subset of Customer while using the
same CIF namespace. That is a valid link with directional containment expectations. The selected
relation must be governed or remain `unknown`; the LLM may only propose a hypothesis.

`IdentifierLinkAvailabilityV1` is derived by folding the generic overlay event stream:

```text
DRAFT | PARTIALLY_CONFIRMED | VERIFIED -> available
REJECTED                               -> unavailable / rejected
REVERIFY                               -> unavailable / reverify
STALE                                  -> unavailable / stale
missing or unreadable stream           -> unavailable / unreadable
```

An explicit alias or supersession decision derives `superseded`; human review does not.

`review_status=human_verified` requires a `VERIFIED` event carrying actual human confirmer
provenance. A source-declared VERIFIED event remains `review_status=unreviewed`; VERIFIED is an
operational folded status, not proof a person reviewed it. No consumer may use `review_status` as an
availability or execution predicate. An available link may produce an unassessed/provisional
realization for sandbox use. The same link may back production once that exact directional
realization is deterministically validated. Expiry folds to `REVERIFY`; it does not remain available.
The `_lifecycle.py` statement that a live `VERIFIED` fact remains usable until its own re-verify flow
means exactly that: once the expiry event moves it to `REVERIFY`, governed reads fail closed.

---

## Task 0 — Select and Record One Implementation Baseline

**Purpose:** The active workspace branch does not contain the deployed `entity_bridges` ingestion
stage, while the integration worktree and Kind image do. Implementation against the wrong branch
would recreate the prior baseline failure.

**Files:**

- Create: `docs/superpowers/verified-interfaces/bridge-cardinality-kind.md`
- Create: `scripts/verify_bridge_cardinality.sql`
- Modify: this plan only if file paths or interfaces differ on the selected baseline

**Steps:**

- [ ] Select one integration commit containing:
  - `OVERLAY_ENTITY_BRIDGES`;
  - declared-type fallback and parameterized-type normalization;
  - `materialize/`;
  - the data-agent contracts already present in the integration worktree.
- [ ] Use `1fb8f81fa5283b3a9058cfb341d5d95bfe7158ae` as the selected code baseline unless a
  deliberate rebase is recorded first. The worktree's existing unrelated `uv.lock` modification
  must not be included in this programme.
- [ ] Record commit SHA, migration head, Kind image digest and relevant feature flags.
- [ ] Verify the image exposes build provenance linking its digest to the selected commit. If that
  link cannot be proved, record the image as `unverifiable` and build a replacement from the
  selected clean commit before implementation acceptance.
- [ ] Add read-only SQL reproducing the baseline counts in “Verified Deployed Baseline”.
- [ ] Re-run the focused bridge, materialization-join and data-agent relationship tests.
- [ ] Record one migration allocation table after rebasing the selected baseline. The baseline
  already has both `1034_analysis_learning_event.sql` and
  `1034_materialization_control_plane.sql`; treat that as a grandfathered collision and allocate
  every new migration in this programme a distinct numeric prefix above the current maximum
  (`1035` on `1fb8f81f`). Re-scan before creating each file; never use the vague instruction
  “allocate the next number” independently in parallel worktrees.
- [ ] Do not start Task 0B until the code baseline is recorded. Task 11 remains gated on connecting
  the selected commit to a verifiable image; the local fail-closed Task 0B patch need not wait for
  cluster access.

**Acceptance:**

- One command reports, by name and without reading business rows: Pass-C candidate rows, graph join
  edges, cross-catalog bridge candidates, bridge candidates carrying a cardinality field, folded
  bridge lifecycle counts, projected `entity_bridge_edge` rows and open bridge human tasks.
- The verified-interface document names one commit and one image digest.
- The verified-interface document owns the exact new migration filename/prefix allocation, with no
  new duplicate numeric prefix.

---

## Task 0B — Close the Live Cardinality and Lifecycle Fail-Open

**Purpose:** Proposed bridges are already traversable on the selected baseline, while
`_hop_evidence()` labels every traversed bridge `many_to_one`. Expired/stale reviewed projections can
also remain readable. These are live correctness exposures and must be closed before contract work.

**Files:**

- Modify: `src/featuregen/overlay/upload/planner/declarations.py`
- Modify: `src/featuregen/overlay/upload/cross_catalog_links.py`
- Modify: `src/featuregen/overlay/expiry.py`
- Modify: `src/featuregen/overlay/catalog_changes.py`
- Modify: `src/featuregen/analysis/grounding.py`
- Modify: `src/featuregen/overlay/upload/contract/invalidation.py`
- Test: existing bridge projection/lifecycle, declaration, grounding and invalidation suites

**Steps:**

- [ ] Change the bridge branch of `_hop_evidence()` to return
  `(None, CARDINALITY_SOURCE_UNAVAILABLE, (), ...)`. A symmetric bridge—proposed or
  human-reviewed—never supplies physical cardinality.
- [ ] Replace `cross_catalog_links._blocked()`'s deny-list with the exact folded-state allow-list
  `DRAFT|PARTIALLY_CONFIRMED|VERIFIED`. `REVERIFY`, `STALE`, `REJECTED`, missing streams and read/fold
  failures are unavailable. Never swallow an exception into “allowed”.
- [ ] Add the `entity_bridge` demotion branch to both expiry and drift staling so
  `entity_bridge_edge` is removed immediately on `REVERIFY` or `STALE`.
- [ ] Make `analysis/grounding.py` distinguish optional human review from availability. It may show
  `JOIN_IDENTITY_UNCONFIRMED` as a non-blocking review warning, but a stale/unreadable link is
  unavailable and must not be described as merely unconfirmed.
- [ ] Make the bridge dependency signature fold/check the authoritative event stream. A stale,
  `REVERIFY`, rejected, missing or unreadable link changes the signature and fails contract reuse
  even if projection cleanup failed.
- [ ] Update stale tests/comments that still equate `active_bridges` with VERIFIED-only.

**Tests:**

- A proposed bridge remains discoverable/traversable as a link but supplies no cardinality.
- All nine proposed baseline candidates can no longer create an `N:1` declaration.
- `REVERIFY`, `STALE` and `REJECTED` each remove the link from the available set.
- An unreadable/corrupt fact stream fails closed.
- Expiry and drift delete a prior `entity_bridge_edge` row.
- Grounding and invalidation refuse stale state even when a stale VERIFIED edge row is deliberately
  left behind.

---

## Task 1 — Add the Link and Directional Realization Contracts

**Files:**

- Create: `src/featuregen/overlay/upload/bridge_assessment.py`
- Create: `src/featuregen/overlay/upload/bridge_realization.py`
- Create: `tests/featuregen/overlay/upload/test_bridge_assessment_contracts.py`
- Modify: `src/featuregen/overlay/upload/bridge_candidates.py`
- Modify: `src/featuregen/overlay/upload/cross_catalog_links.py`
- Modify: `src/featuregen/overlay/upload/bridge_projection.py`
- Reuse: `src/featuregen/data_agent/physical.py`
- Reuse: `src/featuregen/overlay/upload/taxonomy/entity_relationships.py`

**Steps:**

- [ ] Implement the contracts above as immutable, versioned dataclasses/enums.
- [ ] Canonicalize unordered link endpoints independently from directional realization endpoints.
- [ ] Reuse `PhysicalObjectIdentityV1` and `PhysicalDatasetBindingV1` rather than minting a third
  physical-address model. A discovery endpoint may lack a binding; an executable endpoint may not.
- [ ] Keep the bridge fact's logical endpoint refs in the flattened
  `<catalog>::public.<table>.<column>` namespace. They are not physical addresses. Physical identity
  belongs only to the optional binding fields on an endpoint/realization.
- [ ] Use the taxonomy `Cardinality` vocabulary internally plus a **required**
  `DirectionalCardinalityVerdictV1` wrapper that represents `UNKNOWN`; the existing `Cardinality`
  enum has no unknown member. Keep `1:1|N:1|1:N` only as external adapters for existing
  CSV/approved-join boundaries.
- [ ] Define structured predicates for:
  - fixed partition/snapshot value reference;
  - as-of interval requirement;
  - unresolved additional-key requirement.
- [ ] Keep equality mappings only in `column_pairs`; do not represent the same equality again as a
  predicate.
- [ ] Reject empty tuples, duplicate tuple members, mismatched column-pair lengths and free-form SQL.
- [ ] Implement separate canonical payloads for logical realization identity and revision identity.
- [ ] Define candidate identity over candidate family plus canonical unordered logical endpoint pair.
  Entity/concept conclusions, derivation version, evidence and scores belong to the immutable
  candidate revision, not `candidate_id`.
- [ ] Implement the overlay-state-to-`IdentifierLinkAvailabilityV1` mapping above in one shared
  reader. Do not let each consumer reinterpret DRAFT/VERIFIED separately.
- [ ] Introduce `available_identifier_links()` here by adapting the existing
  `cross_catalog_links()` union through that shared availability reader. Tasks 6B, 9 and 10 consume
  it; none creates a second availability interpretation.
- [ ] Treat an `unassessed` realization revision with resolved physical bindings, explicit direction,
  mappings, predicates and scope as a provisional realization. It is eligible for marked sandbox
  execution and automatic probing, not production publication.
- [ ] Keep safety status, optional review status, lifecycle, timestamps and live observations outside
  stable identity.
- [ ] Model evidence as typed references and derive a strongest display label; do not collapse a
  multi-source evidence set into one persisted `evidence_level`.

**Tests:**

- Reversing link endpoints preserves link identity.
- Reversing a directional realization changes its identity and cardinality direction.
- Generic overlay DRAFT, PARTIALLY_CONFIRMED and VERIFIED all map to
  `availability=available`.
- Human confirmation changing DRAFT to VERIFIED changes review metadata and never link
  availability or realization safety.
- A source-declared VERIFIED event is available but remains `review_status=unreviewed`; only real
  confirmer provenance yields `human_verified`.
- REVERIFY, STALE, REJECTED and an unreadable fact stream map to unavailable.
- Adding `business_dt` to the realization mapping changes realization identity.
- Changing only the cardinality conclusion keeps `realization_id` and changes
  `realization_revision_id`.
- Changing live evidence timestamp does not change identity.
- `N:1` and `1:N` realizations never hash identically.
- A physical table in another Hive database never aliases the endpoint.
- Two physical bindings may resolve from the same flat logical endpoint across revisions without
  changing the symmetric bridge fact key; their realization identities remain different.
- Parallel member/type arrays are structurally impossible.

---

## Task 2 — Keep Logical Bridge Identity Flat and Resolve Physical Endpoints Honestly

**Bridge improvements:** #5 and #6.

**Files:**

- Modify: `src/featuregen/overlay/upload/bridge_candidates.py`
- Modify: `src/featuregen/overlay/upload/bridge_propose.py`
- Reuse: `src/featuregen/materialize/inputs.py`
- Reuse: `src/featuregen/data_agent/physical.py`
- Modify: physical-binding persistence selected by Task 0 to expose binding revision/content hash
- Test: `tests/featuregen/overlay/upload/test_bridge_endpoint_identity.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_types.py`

**Steps:**

- [ ] Preserve `public.<table>.<column>` in bridge candidates, `EntityBridgeRef`, fact keys,
  candidate logical identity, drift fingerprints and `overlay_fact_dependency`. This is the platform
  logical namespace, not a fabricated physical schema.
- [ ] Do not create schema-qualified replacement bridge facts or a legacy alias/supersession
  migration. Changing bridge refs alone would fork the logical namespace and disconnect drift
  staling, because both the adapter and dependency index use the flattened refs.
- [ ] Resolve database/schema/table through the existing physical binding. Record the binding
  revision/content hash on an execution realization and on every relationship observation.
- [ ] Reuse `materialize.inputs.resolve_physical_identity()` semantics: catalog `schema_name`, then a
  declared environment mapping, otherwise `PHYSICAL_SCHEMA_NOT_RESOLVED`. Never parse the `public`
  segment as a cluster schema.
- [ ] Refuse ambiguous or unresolved physical identity for automatic probes and production
  execution. Discovery and feature suggestion may retain the flat logical endpoint.
- [ ] Preserve the already-shipped parameter stripping, attested-first `declared_type` fallback and
  `type_basis=attested|declared|mixed`; do not reimplement or “port” them.
- [ ] Make the versioned assessment refresh `data_type_family`, `type_basis` and their evidence refs
  when later structural evidence becomes stronger. Task 6A owns persistence/currentness.
- [ ] Permit cross-family consideration only through an explicit normalization candidate; never
  silently cast identifiers.
- [ ] Strengthen the bridge write gate to verify both endpoints exist, are columns, match the claimed
  sources and still carry an identifier classification plus its authority. An LLM classification is
  valid for a proposed fact and makes the link immediately available, but cannot independently make
  the directional realization deterministically validated or executable.

**Tests:**

- `varchar(30)` and `varchar(150)` share the text family.
- An attested type overrides a conflicting declared type.
- The existing parameterized/declared-type tests pass without changing their shipped resolver.
- A bridge fact and its dependency rows remain keyed by the same flat logical refs the upload
  adapter emits.
- Replacing only a bridge fact endpoint with `banking.<table>.<column>` is rejected by a regression
  test because it would detach dependency staling.
- Two same-named physical tables in different schemas/databases never collide after binding
  resolution; an ambiguous logical endpoint refuses physical execution instead of changing fact
  identity.
- A flat logical endpoint can become deterministically executable only after a unique physical
  binding resolves it.
- A caller cannot propose nonexistent or non-column endpoints.
- Integer/text requires an explicit tested normalization and remains non-operational without it.

---

## Task 3 — Ground Entity, Namespace, Population and Authority Separately

**Bridge improvements:** #1 and #2.

**Files:**

- Create: `src/featuregen/overlay/upload/attest/bridge_grounding.py`
- Modify: `src/featuregen/overlay/upload/bridge_candidates.py`
- Reuse: `src/featuregen/overlay/field_evidence.py`
- Reuse: `src/featuregen/overlay/upload/planner/b_concept_authority.py` for authoritative concept
  semantics, without changing its fail-closed planner policy
- Modify: `src/featuregen/overlay/upload/field_resolution.py` only if a shared evidence/provenance
  read model is added; `is_feature_eligible()` alone is not an authority/value reader
- Test: `tests/featuregen/overlay/upload/attest/test_bridge_grounding.py`

**Steps:**

- [ ] Keep the existing `attest/grounding.py` as the concept-grounding module. Put bridge-specific
  entity/namespace/population grounding in `attest/bridge_grounding.py`; do not replace or overload
  the existing `GroundingV1`.
- [ ] Load the concept together with its resolved authority/provenance. Do not treat flat
  `graph_node.concept` as authority-complete.
- [ ] Add one bridge-grounding read result that can return active source/human evidence or an honest
  LLM-only display candidate. Do not launder an LLM concept through the planner's authoritative
  concept binding.
- [ ] Preserve LLM-only concepts as candidate inputs with `concept_authority=llm`.
- [ ] Ground independently:
  - entity type;
  - identifier namespace;
  - governed population relation;
  - advisory population hypothesis;
  - endpoint key role;
  - type/format compatibility;
  - definitions, names, terms, synonyms, domain and taxonomy.
- [ ] Use hard conflicts only for contradictions that actually disprove the claim:
  - different governed entity;
  - explicit different identifier namespace;
  - nonexistent endpoint;
  - untestable/incompatible representation for an equality mapping.
- [ ] Treat cross-domain links as normal; Payments-to-Customer is not a domain conflict.
- [ ] Keep missing synonyms/taxonomy as `absent`, never `disagree`.
- [ ] Add deterministic negative evidence for code/name/description mismatches without relying on
  substrings alone. Use a closed representation-role vocabulary and corroborate it with type,
  concept, name/definition tokens and observed format when available.

**Deployed regression fixtures:**

- `cust_num <-> cif_id` remains a Customer link candidate.
- `cust_prim_branch_nm <-> tran_branch_sol_id` does not become a strong namespace proposal.
- `cust_pref_branch_cd <-> sol_desc` does not become a strong namespace proposal.
- A cardholder CIF may resolve to `right_subset`, not `different_namespace`.
- An LLM-only Customer concept creates a visible candidate labelled `llm_only`.

---

## Task 4 — Bound Candidate Enumeration and Persist Truncation Honestly

**Bridge improvement:** #8, enumeration half.

**Files:**

- Modify: `src/featuregen/overlay/upload/bridge_candidates.py`
- Modify: `src/featuregen/overlay/upload/ingest.py`
- Modify: `src/featuregen/overlay/upload/ingestion_run.py` or the selected baseline's
  `ingestion_run_stage.detail` writer
- Test: `tests/featuregen/overlay/upload/test_bridge_candidate_bounds.py`

**Steps:**

- [ ] Replace global per-entity nested enumeration with deterministic inverted-index/blocking by:
  - source pair;
  - entity;
  - compatible type/normalization family;
  - namespace hints and key role where present.
- [ ] Rank within a block using deterministic metadata corroboration.
- [ ] Keep at most 20 candidates per endpoint per source pair in v1.
- [ ] Apply the bound while generating/scoring candidates; do not enumerate all O(n²) pairs and cap
  only afterwards.
- [ ] Return and persist `truncated`, `considered_count`, `retained_count` and reason in the ingestion
  stage detail. Define whether `considered_count` means actually scored or cheaply block-matched.
- [ ] Do not create an unbounded suppressed-pair ledger. Persist aggregate reason counts plus a
  bounded top-K of suppressed/ambiguous examples; only retained candidate revisions enter
  governance.
- [ ] Report separate ingestion counts:
  - intra-catalog join candidates;
  - cross-catalog link candidates;
  - suppressed bridge pairs;
  - truncated bridge pairs.

**Tests:**

- Stable ordering produces the same retained candidate IDs across input order.
- A 1,000-column hub fixture stays within the configured bound.
- The fixture asserts the number of pairs actually scored, not only the number retained.
- The strongest valid candidate survives decoy names/descriptions.
- Truncation is visible in the API and ingestion result.

---

## Task 5 — Derive Metadata Cardinality from the Complete Governed Key

**Bridge improvements:** #3 and #8, execution-mapping half.

**Files:**

- Create: `src/featuregen/overlay/upload/bridge_cardinality.py`
- Modify: `src/featuregen/overlay/upload/bridge_assessment.py`
- Reuse/extend: `src/featuregen/overlay/upload/planner/multisource_endpoints.py`
- Reuse: `src/featuregen/overlay/resolve.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_cardinality.py`

**Steps:**

- [ ] Implement `resolve_complete_key(source, table)` by extending/reusing
  `multisource_endpoints.governed_endpoint()` and `resolve_fact(..., "grain")`. Make the extension
  mandatory: the current `GovernedEndpointV1` drops `is_unique`, authority provenance and the fact
  revision. Return a typed result carrying the ordered columns, `is_unique`, authority provenance,
  fact/head revision and dependency identity.
- [ ] Do not modify `catalog_realizations.py` for this concern; it owns intra-catalog declared joins,
  not cross-catalog bridge cardinality.
- [ ] Stop using “first `is_grain` column” as a complete object key.
- [ ] Never reconstruct a complete tuple from flat `graph_node.is_grain` booleans. Legacy flat flags
  without a resolvable governed/source constraint produce `unknown`.
- [ ] Classify an endpoint as `complete_unique_key` only when its ordered tuple equals the complete
  key **and** `is_unique is true`. A complete grain with `is_unique=false` is a complete non-unique
  grain, not a key.
- [ ] Keep authority and review separate. An operational VERIFIED grain may have catalog authority,
  `authority_basis=source_declared`, or human/legacy-confirmed provenance. The automatic no-human
  metadata path exists only for the first two; a DRAFT/advisory `is_grain` flag cannot be promoted
  merely to avoid a human. Reuse the shipped upload `_assert_fact()` source-declared path; do not
  fabricate a confirmer.
- [ ] Implement the directional truth table:

  | From unique | To unique | From → To |
  | --- | --- | --- |
  | no | yes | `many_to_one` |
  | yes | no | `one_to_many` |
  | yes | yes | `one_to_one` |
  | no | no | `many_to_many` risk |
  | unknown | any | `unknown` |

  Here `no` includes a complete grain whose `is_unique=false`; it must never enter the `yes` rows.

- [ ] When a scalar namespace link touches a composite target key, emit a structured
  `additional_key_required` or `snapshot_predicate_required` realization candidate.
- [ ] Permit the realization to map extra contextual columns such as `business_dt`; do not call the
  identifier namespace itself composite.
- [ ] Do not infer an extra mapping because the two columns share a name. Each mapping requires a
  source-declared/governed relation or a deterministic scoped pair observation. Where a history table
  needs an as-of interval, emit that typed predicate instead of equality.
- [ ] Never treat a sampled uniqueness ratio as exact uniqueness.

**Required CIB/FTR test:**

```text
cib key = (business_dt, cust_num), unique
ftr key = (tran_id), unique
namespace link = cust_num <-> cif_id

result for cif_id -> cust_num alone:
  cardinality = unknown
  requirement = additional_key_or_snapshot_predicate
```

Adding a separately governed or data-validated pair `business_dt -> business_dt` may yield
metadata-inferred `many_to_one`, but it is not deterministically validated for production until Task
7 verifies target uniqueness and fan-out under the exact declared data scope.

**Additional tests:**

- `GovernedEndpointV1` cannot silently discard `is_unique=false`.
- A complete target grain with `is_unique=false` cannot yield `many_to_one`.
- A source-declared complete unique key can support the automatic metadata path without a human
  confirmer.
- A DRAFT/advisory complete key cannot support that path.

---

## Task 6 — Add Candidate/Realization Revisions, Re-evaluation and Immediate Demotion

**Bridge improvement:** #7.

**Files:**

- Modify: `src/featuregen/overlay/upload/bridge_propose.py`
- Modify: `src/featuregen/overlay/upload/bridge_projection.py`
- Modify: `src/featuregen/overlay/proposal_commands.py`
- Create: `src/featuregen/overlay/upload/bridge_store.py`
- Modify: `src/featuregen/overlay/confirmation_commands.py`
- Modify: `src/featuregen/overlay/catalog_changes.py`
- Modify: `src/featuregen/overlay/expiry.py`
- Modify: `src/featuregen/overlay/projection.py` or its selected-baseline dependency-index owner
- Modify: `src/featuregen/overlay/upload/table_fact_projection.py`
- Modify: `src/featuregen/overlay/upload/ingest.py`
- Add migration: use the exact candidate/realization-store filename reserved by Task 0
- Test: `tests/featuregen/overlay/upload/test_bridge_reassessment.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_projection.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_store.py`

**Persistence rule:**

- Evolve the current bridge ledger into the bridge typed family of the roadmap's shared candidate
  identity/revision/currentness substrate. Do not add another mutable bridge-candidate table.
- Preserve legacy `entity_bridge_candidate_evidence` rows through an explicit migration/compatibility
  view, but stop treating its mutable `evidence_json` as the current assessment.
- Store immutable candidate assessment revisions and immutable realization revisions separately.
- Store one CAS-protected current pointer per logical candidate and per logical realization.
- `overlay_evidence` may retain generic evidence metrics, but it is not the lifecycle/currentness
  store.
- `entity_bridge_edge` and the new executable-realization projection are rebuildable read models.

**Steps:**

**6A — persistence foundation, executed before Task 4:**

- [ ] Use `propose_fact`'s existing raw-evidence path so denied duplicate proposals do not mint
  orphan evidence, but do not use it unchanged: it currently forwards only metrics and therefore
  defaults `write_evidence()` to `producer=profiler`, `strength=supported` while dropping
  `producer_item_ref`. Extend the closed raw-evidence payload to preserve and validate the bridge's
  `producer=structural_connector`, `strength=proposed`, lifecycle, producer item/configuration and
  evidence spans before switching `bridge_propose.py`.
- [ ] Define additive tables/contracts equivalent to:
  - shared candidate identity;
  - immutable candidate revision;
  - candidate current pointer;
  - immutable bridge realization revision;
  - realization current pointer;
  - typed realization dependency/reverse index.
- [ ] On a new assessment, atomically write the immutable revision and compare-and-set the candidate's
  current pointer. Do the same for realization revisions.
- [ ] Persist a new assessment revision even when generic `propose_fact` returns the expected
  duplicate/non-terminal denial. The current early return before the ledger upsert is the reason
  better evidence never becomes current.
- [ ] During compatibility migration, make the legacy ledger upsert refresh
  `candidate_id`, `data_type_family`, `evidence_json` and `derivation_version`, not only
  `fact_key`, `proposed_event_id` and `updated_at`. The immutable assessment/current pointer remains
  authoritative after migration.
- [ ] Record deterministic safety validation and optional human review as separate explicit decision
  events naming the exact realization revision. Confirming a symmetric link alone never writes a
  realization safety decision, and a safety decision never fabricates a human confirmation.

**6B — lifecycle/reassessment, executed after Task 5:**

- [ ] Feed every new/recomputed assessment into Task 1's `available_identifier_links()` reader.
  Keep the existing VERIFIED projection as optional human-review provenance/ranking and update it
  synchronously in `confirm_fact()`, but never use it as the availability or execution gate. Add
  separate commands for optional realization review and deterministic safety decisions; do not
  overload one command invisibly.
- [ ] Mark dependent realizations stale immediately when:
  - a grain is confirmed, rejected, expired or staled;
  - endpoint physical binding/schema revision, type evidence or concept authority changes;
  - the bridge is rejected, expired or staled;
  - a qualifying relationship observation supersedes its evidence.
- [ ] Index every load-bearing dependency: endpoint columns/tables, concept/type decision or evidence
  IDs, complete-grain fact revisions, physical binding revisions, extra predicate/mapping columns,
  normalization/admission policy versions and relationship observation revision.
- [ ] A newer observation supersedes prior evidence only when it addresses the same relationship and
  applicability scope, compatible binding revisions/principal, and has equal-or-stronger
  completeness/method. A newer partial/sample never displaces an older complete exact observation.
- [ ] On bridge reject/expiry/stale, immediately remove it from the available-link reader and remove
  dependent executable realizations. An unreviewed `DRAFT` link remains available; lack of human
  confirmation is not a demotion condition.
- [ ] Move initial bridge assessment after the ingest projection drain and governed table-fact
  re-projection. Recompute targeted candidates after later grain confirmation/projection.
- [ ] A recompute failure keeps the prior link history but removes stale realizations from the
  executable reader.
- [ ] Executable readers fold/check authoritative current state as a second gate, so a projection
  cleanup failure fails closed.

**Tests:**

- Confirming CIB's composite grain changes the Customer assessment from two false booleans to
  `composite_member/unknown_cardinality`.
- Reject, expiry and catalog drift immediately remove executable realizations without a manual
  `project_verified_bridge` call.
- A newly proposed link appears immediately in `available_identifier_links()` without confirmation.
- A proposed link with a deterministically validated realization can execute in production while the
  generic overlay link remains DRAFT.
- Confirming a link changes only human-review provenance/ranking; it does not validate a realization.
- Re-proposing an existing DRAFT creates no orphan evidence.
- Structural bridge evidence passed through `propose_fact` remains
  `structural_connector/proposed`; it is never mislabeled `profiler/supported`.
- Re-deriving an existing DRAFT after stronger type evidence creates a new current assessment and
  refreshes compatibility-ledger type evidence even though the generic proposal is denied as a
  duplicate.
- An older assessment completion cannot overwrite a newer current pointer.
- Two concurrent assessments cannot both become current.
- An observation completing after one of its binding/grain dependencies changes cannot become
  current or deterministically validate a realization.
- A newer partial/sample does not supersede complete exact evidence for the same scope.

---

## Task 7 — Add Bounded Pairwise Data Evidence

**Bridge improvement:** #4.

**Files:**

- Modify: `src/featuregen/data_agent/observation.py`
- Modify: `src/featuregen/data_agent/relationship.py`
- Modify: `src/featuregen/data_agent/analysis.py`
- Modify: `src/featuregen/data_agent/executor.py`
- Modify: `src/featuregen/data_agent/store.py`
- Modify: `src/featuregen/data_agent/sql_hive.py`
- Modify: `src/featuregen/data_agent/sql_postgres.py`
- Create: `src/featuregen/data_agent/relationship_observation.py`
- Add migration: use the exact relationship-observation filename reserved by Task 0
- Test: `tests/featuregen/data_agent/test_identifier_link_profile.py`
- Test: `tests/featuregen/data_agent/test_identifier_link_profile_hive.py`
- Test: `tests/featuregen/data_agent/test_relationship_observation_store.py`
- Modify/test: existing data-agent verified-join and pilot fixtures on the selected baseline

**Steps:**

- [ ] Replace the scalar-only `RelationshipProbeV1` execution path with a tuple-aware
  `RelationshipObservationPlanV2` that consumes `RelationshipObservationScopeV1`.
- [ ] Keep `relationship.py` as the public compatibility/admission seam or replace it atomically; do
  not leave two profilers with different meanings. Update `analysis.py` and pilot fixtures that
  consume `RelationshipEvidenceV1`.
- [ ] Execute through the existing DB-API cursor/dialect executor. Do not call
  `connection.execute()`, use PostgreSQL-only `FILTER` syntax for Hive, or bypass timeout/partition
  checks.
- [ ] Extend the existing `executor.Dialect` Protocol with
  `render_relationship_probe(plan)` and reuse `Dialect.effective_method()`. Do not add a second
  method-label resolver; the shipped executor already distinguishes the requested method from what
  the engine actually executes.
- [ ] Require selectors for each partitioned binding and retain actual partitions read. Refuse an
  unrestricted scan of a partitioned bank table.
- [ ] Collect, for each endpoint tuple:
  - row and non-null counts;
  - distinct tuple count;
  - duplicate tuple count;
  - maximum rows per tuple;
  - exact versus approximate distinct method;
  - full versus sampled/partial row coverage.
- [ ] Collect, for the pair and required predicates:
  - distinct containment in both directions;
  - row and distinct orphan ratios;
  - joined row count;
  - maximum target matches per source tuple in both directions;
  - observed row-amplification ratio;
  - tested normalization and predicate identifiers.
- [ ] Remove or rename the current `max_left_rows_per_right_key`: it measures source-key frequency
  (for example six transactions for one customer), not how many target rows one source row joins to.
- [ ] Define cardinality over non-null, joinable tuples and report nulls separately. Decide
  conservatively whether target NULL duplicates affect the verdict; test the chosen SQL-equality
  semantics explicitly.
- [ ] Use the dialect's actual exact/approximate implementation and report the effective method. Do
  not label an exact `COUNT(DISTINCT)` as approximate because the request asked for approximation.
- [ ] Execute probes only for the bounded shortlist.
- [ ] Return aggregate evidence only; no raw identifier values.
- [ ] Persist the result as `producer=profiler`, `strength=supported`, tied to exact source snapshots
  and the realization revision. Persist the two binding revisions, both scopes, principal, method,
  completeness, actual partitions and observation time.
- [ ] A partial-row or sampled-row observation, or an approximate distinct method, may detect a
  duplicate/fan-out conflict but may not prove global uniqueness.
- [ ] An exact partition-scoped observation may prove uniqueness only for that parameterized
  applicability scope; it never becomes an unrestricted table-level claim.

**Local acceptance fixture:**

- unique target produces `N:1`;
- duplicated target produces `N:N` risk;
- adding a business-date predicate changes duplicated history into `N:1`;
- six transactions for one unique customer produce source-key frequency `6` but source-row join
  multiplier `1`;
- zero-overlap values weaken namespace evidence but do not alone prove a different namespace when
  the expected populations are disjoint;
- empty, null-heavy, normalized-collision and partially-read fixtures are classified honestly;
- every expected number is hand-calculated.

**Execution decision for this slice:** use the existing bounded direct-SQL observation executor for
PostgreSQL/HiveServer2. A later Spark/Kedro executor may consume the same plan contract, but Task 7
does not claim Spark-local acceptance while implementing direct SQL. Live Hive execution remains
gated on data access; Hive SQL rendering and DB-API transport tests do not.

---

## Task 8 — Fuse Metadata, Data and LLM Evidence into Link Tiers

**Bridge improvements:** #1, #2, #3 and #4.

**Files:**

- Create or replace: `src/featuregen/overlay/upload/bridge_admission.py`
- Create or replace: `src/featuregen/overlay/upload/attest/bridge_critic.py`
- Reuse: `src/featuregen/overlay/upload/enrich_llm.py` for the audited structured-call seam
- Reuse: `llm_call` as immutable call audit
- Reuse or create once: the roadmap-owned content-addressed structured-result store/API; Task 0 must
  name its concrete module/table on the selected baseline
- Test: `tests/featuregen/overlay/upload/test_bridge_admission.py`
- Test: `tests/featuregen/overlay/upload/attest/test_bridge_critic.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_admission_policy.py`

**Steps:**

- [ ] If Task 0 finds no shared content-addressed structured-result store, add a minimal shared one
  before the critic using the exact migration filename reserved in Task 0. Do not mistake append-only
  `llm_call` audit rows or an enrichment-specific cache for that shared result store, and do not
  create a bridge-only replay table.
- [ ] Define versioned `BridgeAdmissionPolicyV1` with:
  - exactness/completeness and binding-currentness requirements;
  - minimum observed rows/partitions;
  - target uniqueness and empty-target rules;
  - containment/orphan/null/amplification thresholds by governed population relation;
  - allowed normalization/predicate vocabulary;
  - evidence age and conflict precedence.
- [ ] Run deterministic hard checks before any LLM call.
- [ ] Ask separate semantic questions:
  - are these the same identifier namespace?
  - what population relation should a human or deterministic probe investigate?
- [ ] Store the second answer only as `population_hypothesis`. Select
  `governed_population_relation` from source/governed/human evidence or leave it `unknown`.
- [ ] Keep observed containment separate from governed population semantics. Do not force subset
  populations into `different_namespace`.
- [ ] Bind every LLM conclusion to existing evidence IDs and expose the concept authority.
- [ ] Preserve LLM-only proposals:

  ```text
  proposed + llm_only + cardinality unknown
  ```

- [ ] Apply hard conflicts before ranking; do not use a weighted sum that lets many weak positives
  cancel observed fan-out or an explicit namespace contradiction.
- [ ] Derive display/review tiers:
  - `discovered`;
  - `strong_proposed`;
  - `rejected`/`stale`.
- [ ] Keep realization safety (`unassessed|deterministically_validated|unsafe`), optional review
  (`unreviewed|human_verified`) and lifecycle (`active|stale|rejected|superseded`) on independent
  axes. Review status never enters the execution predicate.
- [ ] Require deterministic safety evidence for a `deterministically_validated` operational
  realization:
  - compatible representation;
  - target uniqueness proved by a complete governed key, a governed source constraint or an
    exact/scoped profile;
  - acceptable containment for the governed population relation under
    `BridgeAdmissionPolicyV1`;
  - no observed fan-out and no conflicting governed fact.
- [ ] A source/governed namespace assertion plus a complete unique target key may validate the
  static cardinality without a human **only** when the key authority is catalog-authoritative or
  `authority_basis=source_declared` and `is_unique=true`. A merely DRAFT/advisory grain cannot. A
  current human-confirmed key may also be evidence, but is not the only path. An LLM-only namespace
  proposal requires the bounded exact data probe before production use. Both paths still execute
  the pre-computation fan-out gate; a later observed contradiction demotes the realization
  immediately.
- [ ] When population relation is unknown, apply the policy's conservative unknown relation; never
  let the LLM hypothesis lower the required containment.
- [ ] Human verification may record review of the business assertion and influence ranking, but it is
  neither required nor sufficient for execution. It may not convert unassessed or observed
  target-duplicate/fan-out evidence into a safe production traversal. This slice has no allocation
  policy, so current observed fan-out remains a production refusal.
- [ ] Let confidence order review work only; it never independently determines execution
  eligibility.

**Tests:**

- LLM-only match creates a candidate.
- UI/API evidence says `llm_only`.
- Confident LLM cannot override observed target duplicates.
- Exact data evidence can deterministically validate a realization without a human.
- Source/governed namespace evidence plus a source-declared/catalog-authoritative complete unique
  target key can validate static cardinality without a human.
- A DRAFT key or a complete grain with `is_unique=false` cannot validate static cardinality.
- An LLM-only link reaches production without a human after its exact bounded probe validates the
  realization.
- Exact evidence for one partition cannot validate an unrestricted realization.
- An LLM `left_subset` hypothesis cannot select the subset admission threshold.
- Human verification of namespace equivalence cannot override observed join fan-out.
- A DRAFT `entity_bridge` with a deterministically validated realization is production-executable.
- Human verification alone leaves an unassessed realization non-executable.
- Missing synonyms never blocks otherwise sufficient evidence.
- Branch code-to-description pairs remain suppressed despite the shared broad entity.

---

## Task 9 — Make Planners Consume Realizations, Not Guessed Bridge Cardinality

**Bridge improvement:** #3.

**Files:**

- Modify: `src/featuregen/overlay/upload/cross_catalog_links.py`
- Modify: `src/featuregen/overlay/upload/bridge_projection.py`
- Modify: `src/featuregen/overlay/upload/asset_detail.py`
- Modify: `src/featuregen/overlay/upload/planner/contracts.py`
- Modify: `src/featuregen/overlay/upload/planner/declarations.py`
- Modify: `src/featuregen/overlay/upload/planner/assembly.py`
- Modify: `src/featuregen/overlay/upload/planner/fingerprint.py`
- Modify: `src/featuregen/overlay/upload/planner/plan.py`
- Modify: `src/featuregen/overlay/upload/planner/plan_envelope.py`
- Modify: `src/featuregen/overlay/upload/planner/multisource_contracts.py`
- Modify: `src/featuregen/overlay/upload/planner/multisource_assembly.py`
- Modify: `src/featuregen/overlay/upload/planner/multisource_compile.py`
- Modify: `src/featuregen/overlay/upload/planner/multisource_plan.py`
- Modify: `src/featuregen/overlay/upload/planner/multisource_reuse.py`
- Modify: `src/featuregen/overlay/upload/planner/multisource_shadow_store.py` if Task 0 confirms
  persisted path-segment shape changes
- Modify: `src/featuregen/overlay/upload/contract/invalidation.py`
- Modify: `src/featuregen/analysis/grounding.py`
- Modify: `src/featuregen/materialize/joins.py`
- Modify: `src/featuregen/materialize/expression_ir.py`
- Modify: `src/featuregen/materialize/ir.py`
- Create or modify: `src/featuregen/materialize/render/nodes_join_gate.py`
- Modify: shared materialization renderer/project files selected by Task 0
- Test: `tests/featuregen/overlay/upload/planner/test_bridge_cardinality.py`
- Test: `tests/featuregen/materialize/test_bridge_joins.py`
- Test: `tests/featuregen/materialize/test_cross_catalog_ir.py`

**Steps:**

- [ ] Preserve Task 0B's fail-closed `_hop_evidence()` behavior and enable physical cardinality only
  by resolving a current directional realization revision. A regression must not restore bridge
  `MANY_TO_ONE` directly.
- [ ] Keep the existing single-catalog `materialize.plan_join()` fail-closed contract intact. Add a
  typed cross-catalog join adapter that consumes the resolved multisource path/realization; do not
  make the single-catalog function accept mixed catalogs by weakening its validation.
- [ ] Consume Task 1's `available_identifier_links()` for discovery, suggestions, data-agent
  planning and provisional realization construction. Add
  `executable_bridge_realizations()` for production compilation; do not recreate the available-link
  reader here.
- [ ] Produce and maintain a consumer matrix:

  | Consumer | Reader |
  | --- | --- |
  | search, asset, lineage, review UI | available links |
  | feature suggestion and realization construction | available links |
  | marked sandbox planner/data agent | available link + provisional realization + explicit warnings + runtime probe |
  | production analysis/materialization | executable realization only |
  | contract invalidation/reuse | realization revision + dependency snapshot |

- [ ] Add a grep/import acceptance gate proving no production consumer still reads
  `active_bridges()` or treats `CrossCatalogLink.usable` as production authority.
- [ ] Resolve cardinality from the directional realization revision.
- [ ] Extend `BindingPathSegmentV1` and multisource contracts with realization ID/revision,
  directional physical column mappings, predicates, applicability scope and dependency snapshot.
- [ ] Carry catalog source on every physical join endpoint. The current materialization IR infers one
  catalog for all join steps and therefore cannot authorize a cross-catalog read honestly.
- [ ] Extend the physical read set and Gate 2 authorization to include every endpoint table/column,
  extra mapping/predicate column and bridge path dependency from every catalog.
- [ ] Carry structured mappings/predicates into the execution IR and renderer. Render only the closed
  predicate vocabulary; never accept stored free SQL.
- [ ] For discovery and marked sandbox:
  - allow a proposed link;
  - carry `JOIN_IDENTITY_UNCONFIRMED` and/or `JOIN_CARDINALITY_UNKNOWN`;
  - execute a pre-computation relationship/fan-out probe;
  - keep the output visibly sandbox-only and impossible to publish through a production catalog
    dataset.
- [ ] For production publication:
  - allow the backing `entity_bridge` to remain generic overlay DRAFT;
  - require `safety_status=deterministically_validated`, lifecycle `active`, and current
    deterministic evidence/dependencies on the directional realization;
  - never require human confirmation or human realization review;
  - refuse unknown cardinality;
  - refuse `one_to_many`/`many_to_many` in the traversal direction in this slice;
  - validate duplicate output keys and observed amplification before publish.
- [ ] Put the join-level precondition in a dedicated pre-computation node, not
  `nodes_gate.py`, whose existing responsibility is output validation. Retain output duplicate-key
  and amplification validation as a second gate.
- [ ] Revalidate realization status and evidence currentness immediately before execution.
- [ ] Record the bridge fact key, realization revision and evidence revision on the generated
  artifact/feature contract. On later staleness, invalidate contract reuse and visibly mark existing
  artifacts stale; automatic row correction/restatement remains deferred.

**Tests:**

- A bridge with no realization no longer appears as `N:1`.
- The CIB/FTR scalar link returns `JOIN_CARDINALITY_UNKNOWN`.
- A profiled date-qualified realization compiles as `N:1`.
- Reversing it is `1:N` and production refuses fan-out.
- A link becoming stale during compilation fails the final revalidation.
- A cross-catalog physical read set contains and authorizes both catalogs and all predicate columns.
- A proposed link can run in a marked sandbox immediately.
- A proposed link can reach a production output dataset only through a current,
  deterministically-validated directional realization; human review is irrelevant to this predicate.
- A stale realization invalidates reuse of dependent feature/analysis contracts.

---

## Task 10 — Complete the Governance API and Evidence-First UI

**Bridge improvements:** all eight, user-visible surface.

**Files:**

- Modify: `src/featuregen/api/routes/governance.py`
- Modify: `src/featuregen/overlay/upload/lineage.py`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/screens/GovernanceReviewScreen.tsx`
- Modify: `frontend/src/screens/LineageView.tsx`
- Modify: `frontend/src/screens/IngestResultCallout.tsx`
- Test: corresponding backend and frontend test files

**Steps:**

- [ ] Implement separate list/confirm/reject routes and payloads for:
  - symmetric identifier links;
  - directional realization revisions.
  A combined screen action may call both only when it visibly names both decisions and their evidence.
- [ ] Treat link and realization confirmation as optional review actions. Neither route controls
  whether a proposed link appears in `available_identifier_links()` or whether a deterministically
  validated realization appears in `executable_bridge_realizations()`.
- [ ] Make the confirmation path synchronously update its corresponding projection and return the
  projection outcome.
- [ ] Return and display the authority actually enforced by `resolve_authority()` on the selected
  baseline. Do not claim endpoint-owner or dual-owner approval if the current bridge path is
  platform-admin/single-confirmer; the broader authorization redesign remains explicitly deferred.
- [ ] Return namespace verdict, governed population relation, advisory population hypothesis,
  concept authority, endpoint key roles,
  cardinality, basis, predicates, applicability scope, assessment/realization revision, safety
  status, optional review status, lifecycle, evidence freshness and missing requirements.
- [ ] Never render a blank cardinality. Use:
  - `Not evaluated`;
  - `Unknown — profile required`;
  - `N:1 — governed complete key`;
  - `N:1 when business_dt matches`;
  - `N:N risk`.
- [ ] Distinguish advisory lineage links from governed identifier links and executable
  realizations.
- [ ] Replace overloaded lineage `resolved` semantics with explicit fields such as:
  `endpoint_resolved`, `link_review_status`, `realization_safety_status`, and
  `execution_eligible`.
- [ ] Show why a link was proposed and the strongest contradiction.
- [ ] Show actual metrics rather than an unexplained confidence percentage.
- [ ] Split ingestion counts for Pass C joins versus cross-catalog bridges and assessments.
- [ ] Show visible truncation when candidate bounds were reached.
- [ ] Add a “run bounded profile” action or a clear external-run state for realizations that require
  data evidence; never make a link-confirm action silently run or validate a data probe. The normal
  automatic-validation workflow must not require the user to confirm the link first.

**Required Customer card:**

```text
Identifier link: CIB cust_num <-> FTR cif_id
Concept basis: declared/LLM/source as actually observed
Governed population relation: unknown
LLM population hypothesis: shown separately when present
Join cardinality: Unknown
Reason: CIB key is (business_dt, cust_num); cust_num alone is not unique
Next action: test business_dt equality or a snapshot predicate
```

---

## Task 11 — Reconcile the CIB/FTR Pilot on Kind and Then Hive

**Files:**

- Extend: `scripts/verify_bridge_cardinality.sql`
- Modify: `docs/superpowers/verified-interfaces/bridge-cardinality-kind.md`
- Create when data is available:
  `docs/superpowers/verified-interfaces/bridge-cardinality-hive-pilot.md`

**Kind acceptance:**

- [ ] Build and deploy the selected commit.
- [ ] Re-ingest CIB and FTR.
- [ ] Confirm Pass C truthfully reports zero intra-catalog join candidates because each source has
  one table.
- [ ] Confirm cross-catalog candidate counts are reported separately.
- [ ] Confirm `cust_num <-> cif_id` remains a proposed identifier link.
- [ ] Confirm branch-name/description decoys do not become strong links.
- [ ] Confirm Customer cardinality is `unknown/additional predicate required`, not guessed `N:1`.
- [ ] Confirm every DRAFT/PARTIALLY_CONFIRMED/VERIFIED link appears in
  `available_identifier_links()`, while REJECTED/REVERIFY/STALE and unreadable streams do not.
- [ ] Confirm a DRAFT link appears in the executable production reader only when its directional
  realization is deterministically validated with current evidence.
- [ ] Confirm a later grain confirmation creates a new current assessment and invalidates the old
  realization.
- [ ] Confirm the ingestion-stage truncation/suppression counts reconcile to the bounded derivation
  output and do not mix Pass C joins with cross-catalog candidates.

**Hive/data acceptance when source access exists:**

- [ ] Run the exact bounded pair probe for the Customer link.
- [ ] Test both:
  - `(business_dt, cif_id) -> (business_dt, cust_num)`;
  - `cif_id -> cust_num` under one governed CIB snapshot partition.
- [ ] Record both binding revisions, physical IDs, execution principal, selectors, actual partitions,
  predicate/normalization IDs, method and completion in the evidence.
- [ ] Hand-reconcile uniqueness, nulls, containment, orphan counts, maximum target matches per source
  row and joined-row amplification.
- [ ] Prove six source transactions for one unique customer do not report fan-out greater than one.
- [ ] Automatically validate only the direction/predicate combination supported by the evidence.
- [ ] Compile and run one sandbox feature using the proposed link and one production feature using
  the deterministically validated realization while leaving the generic overlay link unconfirmed.
- [ ] Prove the prior hardcoded-`N:1` path cannot reappear.
- [ ] Change/stale one grain or binding dependency and prove production compilation/reuse fails
  closed while history remains queryable.

---

## Task 12 — Adversarial and Mutation Gates

**Files:**

- Extend focused bridge, planner, materialization, API and UI tests
- Add a mutation harness following the repository's must-die/must-survive pattern

**Required adversarial cases:**

- same entity, different namespace;
- same namespace, subset population;
- same names, disjoint values;
- different names, strong exact overlap;
- LLM-only concept;
- branch code versus branch name/description;
- singleton unique key;
- composite unique key member used alone;
- temporal/history table requiring an as-of predicate;
- duplicate target causing fan-out;
- many source rows sharing one unique target without fan-out;
- exact uniqueness in one partition but not globally;
- empty and null-heavy endpoints;
- normalized values that collide;
- observation finishing after a dependency revision changes;
- two assessment/realization-current CAS writers racing;
- newer partial evidence following older complete exact evidence;
- parameterized and declared-only types;
- same schema/table names in different Hive databases;
- same table names in different schemas;
- stale/rejected bridge remaining in projection;
- expired bridge folded to REVERIFY while a VERIFIED projection row remains;
- unreadable bridge fact stream;
- flat logical bridge/dependency refs resolving to a non-public physical schema;
- complete governed grain with `is_unique=false`;
- human-verified link with stale/unassessed realization;
- DRAFT link with a current deterministically validated realization;
- same link endpoints with two physical binding revisions;
- Hive DB-API cursor and dialect compatibility;
- 1,000-column bounded candidate fixture.

**Required mutations:**

- restore hardcoded bridge `N:1`;
- treat any grain member as independently unique;
- drop one composite key member;
- change an outer/qualified predicate into an unqualified join;
- allow sampled/approximate uniqueness to deterministically validate;
- turn a partition-scoped proof into a global realization;
- compute source-key frequency as source-row fan-out;
- let a newer partial observation supersede complete exact evidence;
- ignore concept authority;
- let an LLM population hypothesis select the admission threshold;
- allow LLM conflict to override data fan-out;
- skip bridge demotion on stale;
- allow a link confirmation to validate its realization implicitly;
- require human confirmation before an otherwise-safe realization can execute;
- drop one catalog or predicate column from the physical read set;
- remove candidate bound;
- suppress every LLM-only candidate;
- force synonyms to be mandatory;
- treat REVERIFY as available;
- turn a fact-stream read failure into an available link;
- change only bridge fact refs from `public` to a physical schema and detach drift dependencies;
- relabel raw structural/proposed bridge evidence as profiler/supported;

The focused harness must include:

- a literal baseline count for its explicitly enumerated focused test set, not the whole repository;
- one must-die sentinel;
- one must-survive no-op;
- proof the suite actually ran before mutants are scored.

Also add replay/concurrency tests for:

- two assessments racing to advance the current pointer;
- deterministic safety validation racing with reject/stale;
- evidence attached to the wrong realization revision or scope;
- one flat logical bridge resolving across physical binding revisions without changing fact identity;
- dependency staling retaining the flat logical key while physical schema resolution changes;
- migration replay/idempotency;
- production consumers importing the available-link reader.

---

## Execution Order

```text
Task 0
  -> Task 0B (immediate cardinality and lifecycle fail-close)
  -> Task 1
  -> Tasks 2 and 3
  -> Task 6A (revision/currentness persistence foundation)
  -> Task 4
  -> Task 5
  -> Task 6B (lifecycle, dependency index, re-assessment and projections)
  -> Task 7
  -> Task 8
  -> Task 9
  -> Task 10
  -> Task 11
  -> Task 12
```

Task 0B removes the live hardcoded-cardinality and stale-lifecycle exposure before new persistence
can accidentally preserve it. Task 1 then freezes the corrected contract names, flat-logical versus
physical-binding ownership, availability reader and identity rules; downstream tasks must not implement
the superseded `operational_status`, scalar `evidence_level`, parallel type arrays or live
`fanout_observed` fields from an earlier draft. Task 6 installs durable revision/currentness before
Task 7 can attach data evidence or Task 8 can deterministically validate a realization.

Task 7's live Hive run may wait for data access, but its contracts, dialect rendering, DB-API
transport and local arithmetic tests do not. Task 9 must preserve Task 0B's refusal, resolve
cardinality only from a directional realization and complete the cross-catalog IR/read-authorization
seam before any new bridge is permitted to publish.

## Definition of Done

The programme is complete when:

1. LLM-only concepts still create clearly labelled identifier-link candidates.
2. Entity, namespace, population and key role are represented independently.
3. No bridge receives cardinality merely because one column has `is_grain=true`.
4. Complete composite keys and required predicates are preserved.
5. Pairwise data evidence is bound to two physical binding revisions and an explicit applicability
   scope, and can deterministically validate safe cardinality without human review. The separate
   metadata-only no-human path requires a catalog-authoritative or source-declared complete key with
   `is_unique=true`; DRAFT/advisory or `is_unique=false` grain metadata cannot use it.
6. REVERIFY/stale/rejected links, realizations and changed grain/binding evidence immediately leave
   the executable reader.
7. The Task-0B patch removes hardcoded bridge `N:1` before new contracts land, and no later planner
   path reintroduces it.
8. The UI never leaves bridge cardinality blank and explains the evidence or missing requirement.
9. The CIB/FTR Customer link is initially classified honestly and becomes `N:1` only if the
   date/snapshot-qualified data probe proves it.
10. Every published feature using a bridge can be traced to one link fact, one directional
    realization revision and one current evidence record.
11. Production compilation authorizes the complete cross-catalog physical read set and revalidates
    current realization safety/lifecycle/dependencies immediately before execution.
12. A stale realization blocks new publication and contract reuse and marks dependent existing
    artifacts stale, while automatic data correction/restatement remains deferred.
13. The old source-key-frequency metric cannot be reported as join fan-out.
14. Proposed links are available to discovery, feature generation, data-agent planning and sandbox
   work without human confirmation, and may reach production only through a current,
   deterministically validated directional realization.
15. Bridge fact keys and drift dependencies retain the platform's flat logical refs; physical
    database/schema/table identity is resolved and revisioned only through the binding/realization.

## Production Data-Correctness Activation Gate

This plan is functionally implementation-ready after Tasks 0–10 and ready for a production
data-correctness pilot only after Task 11. Broad production activation requires all of:

- one selected commit/image/migration baseline;
- backing link state currently folds to DRAFT, PARTIALLY_CONFIRMED or VERIFIED; REVERIFY, STALE,
  REJECTED, missing and unreadable state fail closed regardless of projection contents;
- current physical binding revisions for both endpoints;
- current `deterministically_validated` realization revision; human review status is not consulted;
- known non-fan-out cardinality in traversal direction;
- exact applicability scope carried into the execution IR;
- pre-computation relationship validation plus post-computation key/amplification validation;
- complete physical read-set authorization;
- artifact lineage to link fact, realization revision and evidence revision;
- immediate invalidation/visible staleness when a dependency changes.

This gate does not pull the deferred security and general NFR programme into this slice. It prevents
the functional plan from calling a bridge production-ready while its data correctness is unproved.

## Explicitly Deferred

The following do not block this functional plan:

- endpoint-owner dual sign-off and broader authorization redesign;
- tenant identity and quotas;
- mTLS, signed worker envelopes and secret-manager integration;
- durable scheduling, leases, retries and outbox delivery;
- full E3 composite identifier-namespace facts;
- probabilistic person/entity resolution from fuzzy customer attributes;
- unrestricted multi-hop ontology traversal;
- automatic correction, restatement or physical withdrawal of already-published rows; this plan
  still marks dependent artifacts stale and blocks their reuse;
- production-scale caches and retention automation;
- direct ODS adapters.

They may extend these contracts later, but none may reintroduce guessed cardinality or hide evidence
authority.
