# Feature-Ready Metadata and Feature-Generation Integration - Implementation Plan

**Date:** 2026-07-18
**Finalized:** 2026-07-19 - reconciled against shipped `main`; architect/product-owner sequencing gates added
**Status:** FINALIZED. Architecture accepted. Execution is gated by funding decisions, not a single approval
(see "Funding Gates and Recommended Sequencing" and "Reconciliation with Shipped `main`" below).
**Production baseline (current):** `main` at `8636b4d` - Phase 2 Slice 3 (honest feature generation) and
Phase 3C.2a (live cross-catalog flip) are now MERGED. Migrations end at `1003`, not `1001`.
**Original in-flight baseline (now superseded):** the plan was authored against `main` at `8107bc` with
`phase1-llm-enrichment-hardening`/`phase2-slice1-column-view` in flight. Those, plus Phase 2 Slice 3, have
since merged. Every "Verified Baseline" claim about the feature assistant and migration ceiling is corrected
in the reconciliation section immediately below and takes precedence over the body where they disagree.

## Goal

Close the current gap between CSV ingestion and governed feature construction through an explicit,
persisted handoff. Ingestion produces feature-ready catalog metadata; a separately triggered feature-
generation workflow consumes it. The finished system must:

1. preserve source metadata and LLM enrichment with authority and evidence;
2. produce bounded, reviewable semantic relationship candidates;
3. project only field-policy-qualified source-attested or human-confirmed relationships into operational reads;
4. calculate honest column and recipe readiness;
5. expose one read-scoped asset-details API for the frontend;
6. feed confirmed metadata into feature assistance and, for eligible recipe-backed cross-catalog
   candidates only, the existing 3B.3c planner declaration compiler;
7. express checks that require physical data as external-validation requirements, never as false
   `DESIGN-CHECKED` claims.

It must not generate feature ideas, feature rows or feature contracts as a side effect of a catalog
upload.

## Reconciliation with Shipped `main` (2026-07-19 - binding)

This plan was authored on 2026-07-18. On 2026-07-19 the branch it depended on merged, so parts of it are
already delivered and parts of its baseline are stale. These corrections are binding and override the body.

**1. Migration ceiling moved `1001` -> `1003`.** `main` now carries `1000_graph_node_schema_declared`,
`1001_dispatch_flag_provenance`, `1002_live_activation` (3C.2a), `1003_contract_validation_status`
(Slice 3). The plan's allocated block `1002-1009` collides head-on and **reallocates to `1004-1011`** (see
the corrected Migration Allocation). Any statement that "migrations still end at `1001`" is void.

**2. Slice 3 already delivered the honest MVP of Deliveries C2/C3 and a column-form of C4/H.** What shipped
(commit `8636b4d`), and what therefore is NOT net-new work:

| Plan item | Shipped in Slice 3 | Remaining work this plan adds |
|---|---|---|
| C2 candidate context + field-aware egress + deterministic relevance + `CONTEXT_TOO_LARGE` | YES - `_candidate_columns` widened, nested `sanitize_feature_context` egress adapter (passed a 15-agent adversarial security review), byte-budgeted relevance | Re-source C2's reads from the **C0 immutable snapshot** instead of live flat columns |
| C3 tri-state validator (`_validate_idea` -> `design_checked`/`needs_external_validation`/`rejected`) + requirement codes | YES - live tri-state validator + closed requirement vocab + confirm-time MCV re-run | Add C3's **role-binding admission** (`needs_catalog_confirmation`) and the versioned `ValidationRequirementSchema` registry |
| C4 validation persistence | PARTIAL - as **columns** (`contract.validation_status` + `contract.requirements` jsonb, migration 1003) | C4 upgrades this to the **event-sourced** `feature_contract_validation_event -> state` lifecycle (decision below) |
| C1 authority adapter | PARTIAL - `column_authority.OperationalColumnFacts{value, authority, provenance}` | C1 **extends** this to the richer `OperationalValue` (producer/strength/conflict/policy versions); do not replace the shipped type, widen it |
| H confirm flow + anti-tamper | YES - considered-set -> draft -> confirm, and the CRITICAL **confirm-time fix** that reconciles server-authoritative `grain_table` + `derives_from` before the MCV re-run (mutation-verified) | H adds explicit **role-binding confirmation** (persisted bindings + binding hash, 409 on change) and **contract-version immutability** (`contract_input_column`, `feature_current_contract` pointer) |

So the program's true remaining size is smaller than the headline "117-183 days" implies for the C/H lane:
the validator, egress, relevance and a working confirm flow already exist. The remaining C/H work is the
**durability upgrade** (immutable snapshot, event-sourced validation, contract-version immutability,
role-binding model), not a from-scratch build.

**3. C4 column-vs-event decision (binding, no-rework path).** Slice 3's `contract.validation_status` /
`contract.requirements` columns are **retained as the INITIAL validation stamp**, exactly as this plan
already treats `verification` (see "existing `feature.verification`/`contract.verification` columns remain
legacy initial stamps; new APIs return the effective stamp from the version-scoped validation projection").
C4's `feature_contract_validation_event -> feature_contract_validation_state` becomes the **authoritative
version-scoped lifecycle**, and every API returns the EFFECTIVE state from that projection. Do **not**
migrate away or rewrite the Slice 3 columns; build C4 on top and read effective state from the projection.
This preserves the merged work and still reaches the correct event-sourced model.

**4. C5 may overlap `1001_dispatch_flag_provenance`.** Before adding `1004_ingestion_llm_dispatch`, audit
what `1001_dispatch_flag_provenance` already records; fold C5's per-dispatch audit into the existing table
if it subsumes it, rather than shipping a second dispatch-provenance store.

**5. The `FEATUREGEN_FEATURE_CONTEXT` flag is already the live gate** shipped by Slice 3 (default off, read
only by the feature-generation workflow). Reuse the existing `feature_context_enabled()` helper; do not
introduce a second flag or a second reader.

## Funding Gates and Recommended Sequencing (binding decision framework)

This is a program, not a single approval. Accepting the architecture is not the same as funding all nine
deliveries. Fund in three tranches; each later tranche is **gated on evidence**, not on code existing.

**Tranche 1 - the honest spine (fund now).** The plan's own first-release subset: converge the baseline
(A), finish per-field validation and source-authority evidence (B), the immutable snapshot + authority
adapter + effective-state validation (C0, C1-as-an-extension-of-the-shipped-adapter, C4-effective-state),
the core read-only asset API and capability readiness (F0), and single-catalog feature authoring on the
already-shipped Slice 3 validator (H single-catalog). This proves BOTH the trust story (no false
`DESIGN-CHECKED`) and the feature-quality story end-to-end on one real FTR dataset. Estimated 51-77
person-days on top of Slice 3, and it is a coherent, demoable product on its own.

**Tranche 2 - catalog intelligence (gate on catalog engagement).** Deliveries D and E (LLM-proposed
semantic candidates -> human-confirmed governed facts) and F1/G-edit (relationship sections + edit UI). This
is the genuinely "smarter" tranche - richer semantic grounding for better feature suggestions. **Gate:** do
not fund until Tranche 1 shows users actually opening asset details and asking for richer relationships.
Building governed semantic-fact machinery (~24-38 person-days, ~20 new durable tables' worth of lifecycle)
before that demand is confirmed is speculative infrastructure.

**Tranche 3 - external attestation (gate on a committed counterparty).** Delivery I is protocol-correct
(Ed25519, RFC 8785, nonce replay, key rotation) but **inert without an external data platform that runs the
checks and returns signed results**. **Gate:** do not build Delivery I (~16-26 person-days) until there is a
named integration partner or signed intent with a team committed to implementing the pull/ack + signing side.
"DATA-CHECKED" cannot be earned by a protocol we build alone.

**Flag discipline (already in the body, elevated here):** do not enable a flag merely because its code
exists. Each flag carries its own corpus/gate; the live cross-catalog grounding flag stays forbidden until
the 3C.1 signed gate passes; the external-accept flag stays forbidden until Tranche 3's counterparty exists.

## Verified Baseline

This plan was checked against the current code, not only the design documents.

- `ingest_upload` already owns validation, Pass A enrichment, graph persistence, governed joins,
  Pass C, Pass B, glossary evidence, projection drain, drift and run provenance
  (`overlay/upload/ingest.py`).
- Phase 2 Slice 1 is implemented only on `phase2-slice1-column-view`, not on `main`. It adds the
  attachable `ColumnMetadataView`, field-aware egress, versioned LLM calls and Pass B v2 dual-type
  input. Do not reimplement it.
- Phase 2 Slice 2 is a reviewed plan but is not implemented. It adds per-field Pass B validation,
  stale display clearing and durable dispositions. It is a prerequisite.
- `field_evidence` and `field_decision_event` are the existing authority path for scalar fields.
  The resolver is string-valued and suitable for concepts, definitions, roles and classifications;
  it is not the correct lifecycle for structural graph relationships.
- `overlay_fact` is the existing authority path for load-bearing structural facts. Grain,
  availability, approved joins and entity bridges already use it.
- Pass C already provides the exact candidate-ledger -> governed proposal -> verified projection
  pattern required for new semantic candidates.
- The feature assistant ~~currently reads a lossy graph projection and `_menu` discards definitions,
  types and authority. Its validator returns binary accept/reject and stamps accepted ideas
  `DESIGN-CHECKED`~~ **[STALE - corrected by Slice 3]**. As of `8636b4d`, `_candidate_columns` is
  widened, the menu carries authority-wrapped facts behind `FEATUREGEN_FEATURE_CONTEXT`, a nested
  field-aware egress adapter sanitizes free-text before dispatch, and the validator is tri-state
  (`design_checked`/`needs_external_validation`/`rejected`). C2/C3 build on this, they do not replace it.
- The 3B.3c planner already compiles temporal, connectivity, aggregation, safety and freshness
  declarations for a predefined `Template`/`recipe_id` and emits a `BindingPlanV1` declaration identity.
  It does **not** create the user-governed `contract` row produced by `ContractDraft`, and it cannot compile
  an arbitrary LLM `FeatureIdea`. Preserve that distinction; do not build a second physical planner and do
  not describe a planner declaration id as a feature-contract id.
- Ingestion-run/object provenance exists, but the API reads by run id only. LLM enrichment calls
  use the shared `overlay-enrichment` run bucket, so there is no exact ingestion-run/column linkage.
- The prototype asset screen is hardcoded and local-only. Search, lineage, readiness, evidence and
  ingestion history are not assembled by any backend endpoint.
- Upload, feature-assist and contract APIs are already separately triggered routes:
  `POST /uploads`, `POST /features/recommend`, and the `/contract/considered-set|draft|confirm` flow.
  Preserve that separation even though `feature_assist.py` and contract modules currently live under
  the legacy `overlay/upload/` package path.

## Runtime Ownership Boundary

There are four runtime workflows, with separate transaction and failure boundaries:

1. **Ingestion:** parse, validate, enrich, persist evidence/candidates and rebuild catalog projections.
2. **Catalog governance:** review proposals and confirm/reject governed metadata facts.
3. **Feature generation:** respond to an explicit user/API/job intent, read committed catalog facts,
   generate ideas, validate them and compile a feature contract.
4. **External validation:** execute/check the contract outside this platform and return signed results.

`ingest_upload` may invoke ingestion enrichment stages and reapply already-VERIFIED catalog facts after
`build_graph`. It must never invoke feature assistance, feature/contract persistence, contract compile,
feature registration or external-validation submission. Governance confirmation also runs outside the
upload transaction.

The existing `overlay/upload/feature_assist.py` location is a packaging legacy, not runtime ownership.
Keep it in place for compatibility during this delivery, enforce the boundary with imports/tests, and
leave a package move to a separate mechanical refactor.

The handoff is committed catalog state. The feature workflow opens a `REPEATABLE READ` transaction,
reads that state through the authority-aware adapter, and persists the exact values, logical/physical
refs, field-decision ids, governed fact/event ids, read-scope fingerprint and rule/registry versions it
consumed as an immutable `catalog_metadata_snapshot`. It stores that snapshot id and fingerprint on the
resulting considered set and contract version. The existing ingestion `source_snapshot_id` tokens and
`gn-v1` ingestion fingerprints are correlation mechanisms and must not be reused as this replay contract.
An upload rollback cannot roll back a feature workflow, and a feature failure cannot fail an upload.

## Scope Decisions

### Identity

Do not re-key the graph in this delivery. The current operational graph and overlay facts use
`public.table[.column]`; FTR physical schema is stored separately on `graph_node.schema_name`, while
field evidence keeps a schema-preserving logical ref.

Every new cross-workflow record that identifies an asset (semantic candidate, asset response or
feature contract) must therefore carry the relevant identities explicitly:

```text
graph_ref:       public.comp_financial_tran_repos_dly.tran_amt
logical_ref:     source::dpl_eib_compliance.comp_financial_tran_repos_dly.tran_amt
physical_ref:    dpl_eib_compliance.comp_financial_tran_repos_dly.tran_amt
```

Candidate matching remains same-source and same-table for the first release. Multi-schema graph
re-identity remains a separate migration.

Because the graph key flattens schema to `public`, ingestion must HOLD a source containing two physical
columns that collapse to the same `(table, column)` graph identity. It may not choose one silently. Every
external-platform submission additionally resolves `physical_ref` through an explicit, governed external
dataset binding; a physical FQN alone is not globally unique across platforms/environments.

### Authority

- Scalar meaning fields use `field_evidence` + `field_decision_event`.
- Grain and event/as-of time reuse the existing `grain` and `availability_time` facts.
- Intra/inter-table connectivity reuses `approved_join` and governed `entity_bridge` facts.
- Entity assignment becomes a governed fact projected onto `graph_node.entity`; the current mutable
  `entity_suggestion -> graph_node` shortcut is retained only for compatibility until migrated.
- A measure-to-currency-column association is a new governed `currency_binding` fact.
- LLM output may create a candidate or DRAFT proposal. It can never create VERIFIED state.
- Operational eligibility is field-specific and comes from the existing field-policy/fact lifecycle.
  Do not introduce a parallel coarse authority vocabulary that collapses producer, assertion strength,
  conflict, retirement and influence ceiling into one label.

### Aggregation

Do not persist `TRAN_AMT -> SUM` as a column property. Additivity is a column property; aggregation
function is recipe/feature-contract context. For recipe-backed candidates, the existing 3B.3c compiler
remains the planner-declaration authority. Its aggregation registry is populated only by versioned recipe
declarations, not by ingestion guesses. A free-form feature idea may carry a requested aggregation for
single-catalog validation, but it cannot claim a 3B.3c-resolved declaration unless it has a preserved,
validated `recipe_id` and compiled physical plan.

### External Data

During feature generation, unknown operational type, uniqueness, population, lag and value-level
currency consistency produce machine-readable external-validation requirements on that feature
contract version. Catalog readiness may preview the same missing checks diagnostically. Ingestion does
not create or submit contract requirements. Metadata hints may reject or tighten, but may not clear a
requirement that needs physical evidence.

An external pass is contract-version evidence only. It never creates a reusable VERIFIED catalog fact,
approved join, entity assignment or currency binding. Promoting external observations into catalog
governance would require a separate proposal/review policy and is outside this delivery.

### Verification Vocabulary

Do not add a second spelling of existing verification stamps. Keep the durable stamp vocabulary:

```text
UNVERIFIED | DESIGN-CHECKED | DATA-CHECKED | USEFULNESS-CHECKED
```

Keep candidate workflow status separate from contract validation:

```text
candidate_status:
proposed | needs_catalog_confirmation | rejected

contract_validation_status (only after selected role bindings are confirmed):
design_checked | needs_external_validation | rejected
```

A generated candidate is never stamped `DESIGN-CHECKED` merely because advisory concept/domain metadata
matched. Gate 1 must show and persist exact role-to-column bindings plus their evidence authority. The
human's explicit selection confirms those bindings for this contract only; it does not promote the global
catalog concept. A contract with unresolved external requirements remains `UNVERIFIED`. Satisfying signed
external requirements can promote the projection to `DATA-CHECKED`.

### Contract and Version Ownership

There are two distinct durable concepts and they must keep distinct names and identifiers:

1. **Feature contract version** - the human-confirmed `contract` row authored from a selected
   `FeatureIdea`. This owns business definition, input columns, metadata snapshot, validation
   requirements and external attestations.
2. **Planner declaration** - the 3B.3c declaration attached to a recipe-backed `BindingPlanV1`, identified
   by its existing `physical_plan_id` and `cc_*` declaration id. This proves physical connectivity,
   temporal, aggregation, safety and freshness declarations for one predefined recipe.

Preserve `recipe_id`, generation source and planner ids when a `GroundedFeature` becomes a `FeatureIdea`.
Recipe-backed ideas invoke 3B.3c only for a `source_to_target_resolved` cross-catalog plan and attach its
declaration to the feature contract only after 3C.2 is enabled. Recipe-backed single-catalog ideas record
`not_applicable_single_catalog`. LLM-free-form and user-defined ideas have `recipe_id=NULL`, are limited to
single-catalog authoring, and record `not_applicable_nonrecipe`; they do not fabricate need roles or planner
declarations.

The mutable `feature` row remains a compatibility/current read projection. The immutable unit for this
program is the feature-contract version. Stop replacing global `feature_derives_from` as the historical
source of truth: persist contract-version input columns separately and project the current feature from an
atomic `feature_current_contract` pointer. The existing immutable `feature_versions` aggregate remains the
activation/deployment artifact; when one is minted, it references the qualifying feature-contract version
instead of becoming a third authoring-version model.

Later validation promotion never updates an immutable `feature_versions` row. The activation workflow may
mint a new feature-version artifact from the current contract validation state, preserving the previous
DESIGN-/DATA-CHECKED artifact as history.

Validation lifecycle is append-only and contract-version scoped:

```text
feature_contract_validation_event -> feature_contract_validation_state
                                      (one current projection per contract_id)
```

Do not put mutable validation status or requirements on the global `feature` row. Do not let an
attestation for an older contract version update the current feature projection. Existing
`feature.verification`/`contract.verification` columns remain legacy initial stamps; new APIs return the
effective stamp from the version-scoped validation projection.

### Metadata Snapshot Contract

Add an immutable feature-generation snapshot, not an alias for an ingestion run:

```text
catalog_metadata_snapshot
  snapshot_id, generation_run_id, read_scope_hash, isolation_level,
  projection watermarks, policy/registry/config versions, content_hash, created_at

catalog_metadata_snapshot_item
  snapshot_id, catalog_source, graph_ref, logical_ref, physical_ref,
  item_kind, field_or_fact_type, value_json, authority_json,
  decision_event_id/fact_key/fact_event_id, item_hash
```

The builder runs after the feature transaction has entered `REPEATABLE READ`, stores exactly the bounded
context consumed by generation/validation, and hashes canonical JSON over every item plus all governing
versions and the caller's effective read scope. Both tables are physically write-once. Confirmation
revalidates every referenced current decision/fact and either retries from a new snapshot, downgrades to
external validation where allowed, or rejects. A role change or hidden input is drift, not a replay hit.

## Target End-to-End Flow

```text
WORKFLOW 1 - INGESTION TRANSACTION

CSV/Excel/OpenMetadata
        |
        v
existing readers -> CanonicalRow + source sidecars
        |
        v
validation -> Pass A -> build_graph -> governed join import
        |                         |
        |                         +--> graph/search display projection
        v
ColumnMetadataView -> Pass C + Pass B + glossary field evidence
        |                         |
        |                         +--> scalar decisions + table facts
        v
bounded semantic candidate pass (proposal authority only)
        |
        v
commit catalog state, ingestion-run provenance, evidence, proposals and graph projection

======================= COMMITTED-STATE BOUNDARY =======================

WORKFLOW 2 - CATALOG GOVERNANCE (SEPARATE COMMAND/TRANSACTION)

human review -> overlay_fact VERIFIED
        |
        +--> entity/currency projections
        +--> column readiness
        +--> asset-details API -> search detail/edit UI

WORKFLOW 3 - FEATURE GENERATION (EXPLICIT USER/API/JOB TRIGGER)

feature intent -> REPEATABLE READ authority-aware metadata snapshot
        |
        v
existing feature assistant -> candidate idea + validation disposition
        |
        v
recipe-backed cross-catalog?
       | yes + 3C.2 live --> 3B.3c planner declaration --+
       | yes + gated off --> reject (no permissive fallback)
       | no -------------> single-catalog validator -----+
                                                          |
                                                          v
human-confirmed feature-contract version + external requirements

WORKFLOW 4 - EXTERNAL VALIDATION (AFTER CONTRACT CREATION)

contract submitted to external data platform
        |
        v
execution/checks -> signed validation results
        |
        v
DATA-CHECKED projection (only when every blocking requirement passes)
```

Ingestion enrichment and proposal stages are fail-soft within Workflow 1. Governance confirmation,
feature/contract creation and validation promotion fail closed inside their own workflows. One upload
may support zero, one or many later feature contracts; it creates none by itself.

## Delivery Dependency Graph

```text
A. Baseline convergence
    |
    +--> B. Pass B validation and lifecycle (existing Slice 2)
            |
            +--> C0. Feature-generation metadata snapshot
            |
            +--> C1. Shared authority-aware facts adapter
            |
            +--> C5. Ingestion LLM/run/subject provenance --> D. Semantic candidates --> E. Governance

 C0 + C1 ---------------------------> F0. Core asset/readiness API
 C0 + C1 + F0 ----------------------> C2-C4. Feature context + validation --> H. Feature contracts
 E + F0 ----------------------------> F1. Semantic relationship sections --> G. Asset edit/review UI
 H ---------------------------------> I. External validation protocol

J. Tests, gates and rollout span all workflows but preserve each runtime boundary.
```

### Migration Allocation

**REALLOCATED (2026-07-19):** `main` already deployed `1002_live_activation` and
`1003_contract_validation_status`, so the block below shifts `+2` off the stale `1002` start. Deployed
numbers `1000-1003` are immutable and may not be renumbered.

```text
1004  ingestion LLM dispatch/run/subject attribution   (C5; first audit whether 1001_dispatch_flag_provenance already covers it)
1005  immutable catalog metadata snapshots             (C0)
1006  feature-contract requirements + validation events/state  (C4; event stream ON TOP of the shipped 1003 columns, which stay as the initial stamp)
1007  semantic candidate sets/current projection       (D1)
1008  governed semantic fact read projection           (E3)
1009  immutable contract inputs + current-contract pointer  (H2)
1010  recipe aggregation declarations                  (H3)
1011  external dataset/key/submission/attestation protocol  (I)
```

Each migration is idempotent, adds its write-once/check/FK/index constraints in the same delivery, and has
a PostgreSQL migration test. A parallel branch must reallocate before merge rather than renumber an
already-deployed migration. Every `1002_*`/`1003_*`/`1004_*` filename reference elsewhere in this document
refers to the ORIGINAL numbering; read it through this reallocation (+2 from `1002`).

## Delivery A - Converge the Baseline

**Purpose:** prevent parallel implementations against incompatible branches.

1. Merge or rebase Phase 1 LLM hardening and Phase 2 Slice 1 onto the chosen integration branch.
2. Preserve the current uncommitted prototype frontend work; do not use it as a backend contract.
3. Run the Phase 1/Slice 1 unit, DB, provider-schema and synthetic FTR acceptance suites.
4. Record the exact baseline commit in this plan before Delivery B starts.
5. Migrations now end at `1003` (not `1001`); the finalized allocation is `1004` through `1011` (see the
   reallocated Migration Allocation) and may not be reused by parallel work.

**Gate:** the Slice 1 acceptance test passes and `ColumnMetadataView` is the sole Pass B input
assembler.

## Delivery B - Finish Phase 2 Slice 2 and Source-Authority Prerequisites

**Source plan:** `2026-07-18-phase2-slice2-passb-validation-plan.md` rev. 3 corrections are binding.
Items 8-11 below are additive prerequisites discovered by this integration review and require their own
PR after the existing Slice 2 tasks; they do not silently expand the referenced Slice 2 implementation.

**Files:**

- create `overlay/upload/table_vocab.py`;
- modify `overlay/upload/table_synth.py`;
- modify `overlay/upload/field_resolution.py`;
- modify `overlay/upload/ingest.py`;
- add Pass B validation, stale lifecycle, disposition and acceptance tests.

**Required outcomes:**

1. Grain, availability, role, entity and event/snapshot are validated independently.
2. One invalid field cannot discard valid sibling fields.
3. Previous LLM evidence is producer-scoped staled when omitted or replaced.
4. Staled advisory display values are cleared while retaining a durable staled decision link.
5. Every table records five field dispositions, including `not_evaluated` records.
6. Prompt v3 uses schema v2; do not add a strict canonical role enum that rejects the whole object.
7. The real-provider canary validates the exact Pass B contract.
8. Complete source-field evidence wiring for both glossary and technical profiles. Add versioned policies
   for `business_term`/`term_type`/`declared_type` (RECOMMENDATION), `data_type` (OPERATIONAL only for a structural-
   attesting technical profile), `unit`/scalar `currency` (OPERATIONAL only for source-ATTESTED or
   human-CONFIRMED evidence), and source `entity` (display/recommendation; VERIFIED `entity_assignment`
   remains the operational path). Route existing definition/domain/sensitivity/additivity fields through
   the same profile-aware writer instead of leaving technical values only on `graph_node`.
9. Add `SOURCE_CAPABILITY_PROFILE_VERSION`; persist source type/profile version in the ingestion manifest
   and evidence provenance. For every accepted row, write non-empty source fields at the profile's exact
   strength, attaching glossary fields only through the validated source/schema/table/column binding. FTR
   `declared_type`, unit/currency/entity hints remain PROPOSED and cannot clear checks merely because
   `graph_node` is populated.
10. Reconcile present->changed and present->absent evidence producer-by-producer for technical and glossary
    uploads, using `ingestion_run_id` as the durable cycle/provenance reference. Update the touched-field,
    stale-display, revalidation and decision registries for every new field. This is ingestion write work,
    not a responsibility of the read-only C1 adapter.
11. Bump field-policy/resolver/source-profile versions, replay affected active evidence into fresh decisions,
    and invalidate caches/snapshots by version. A migration/default may not mark old flat values attested.

**Gate:** two-upload integration test proves stale values disappear from graph display and active
evidence while source/human evidence remains untouched.

## Delivery C - Shared Metadata Adapter and Feature Validation Foundation

**Purpose:** implement the safe part of the existing Phase 2 Slice 3 while preserving runtime
ownership. C0/C1 are feature-generation snapshot/read services, C2-C4 run only in feature generation,
and C5 runs only in ingestion audit persistence. `ingest_upload` must not call C0 or C2-C4.

For Slice 3 only, this finalized plan supersedes the earlier Phase-2 design statements that coarse flat-
node presence is sufficient authority and that the binary validator remains unchanged. Slice 1's column
view/egress contract and Slice 2's per-field validation/lifecycle remain binding. Record this precedence in
the Slice 3 implementation plan so two agents do not implement incompatible authority models.

Merge C5 migration `1004`, C0 migration `1005`, then C4 migration `1006` (reallocated from the original
`1002/1003/1004`) so migrations remain append-only in deployment order.

### C0. Immutable metadata snapshot builder

**Runtime owner:** feature generation only. It reads committed catalog state; ingestion never creates a
feature-generation snapshot.

Add migration `1003_catalog_metadata_snapshot.sql` for the write-once header/item tables defined in the
Metadata Snapshot Contract. Add `feature_metadata_snapshot.py` with a single bounded batch loader that:

1. enters `REPEATABLE READ` before its first catalog query and fails if the transaction has already read;
2. reads all graph, evidence, decision, fact, bridge and registry inputs needed for one request;
3. applies read scope before materializing items;
4. stores exact values plus authority/provenance ids and canonical item hashes;
5. persists one content hash over sorted items, policy/registry/config versions and read scope;
6. returns an immutable context object consumed by C2-C4 and H without re-querying mutable flat columns.

Pin overlay, field-decision, semantic and validation projection checkpoints/head sequences. If a required
load-bearing projection is lagged, poisoned or degraded, abort feature generation with
`CATALOG_PROJECTION_UNAVAILABLE`; do not reinterpret missing projected truth as an external data check.

Add a dedicated feature-generation connection dependency that executes `SET TRANSACTION ISOLATION LEVEL
REPEATABLE READ` before the first SQL statement. Refactor considered-set/draft/confirm route ordering so
authentication may run first but no database read precedes this boundary; a late isolation change is a
server error, not a silent fallback to `READ COMMITTED`.

Snapshot creation and considered-set persistence commit atomically. A failed generation may retain a
snapshot for audit, but it may not produce a contract. Add retention/classification metadata without
pretending the existing database supplies a separate restricted store.

Create the durable generation-run manifest first in the same feature transaction and enforce
`catalog_metadata_snapshot.generation_run_id` as an FK. The considered set references exactly one snapshot;
draft and confirm reject client-supplied snapshot ids and reload the server-persisted choice/run lineage.

### C1. Operational facts adapter

**Runtime owner:** shared committed-state read service used by catalog APIs and feature generation.
The adapter has no write path and does not trigger either workflow.

Create `overlay/upload/operational_facts.py`. Do not add a new gating enum. Return the existing authority
axes and the selected immutable ids:

```python
@dataclass(frozen=True)
class OperationalValue:
    value: object | None
    influence: InfluenceTier
    producer: EvidenceProducer | None
    strength: AssertionStrength | None
    status: str
    conflict_status: str | None
    selected_evidence_ids: tuple[str, ...]
    decision_event_id: str | None
    fact_key: str | None
    fact_event_id: str | None
    policy_version: str
    resolver_version: str | None
```

Implement reads for:

- `additivity` from the latest non-retired, conflict-free load-bearing field decision;
- `is_grain` and `is_as_of` as governed only with non-null fact event ids;
- `data_type` as operational only when an explicitly versioned technical-source capability profile
  attests that field; glossary/FTR `declared_type` never qualifies;
- `declared_type` as hint only;
- `entity`, `unit` and scalar `currency` as operational only when their field policy returns a current
  load-bearing value or a VERIFIED specialized fact; otherwise retain them as labelled display context;
- concept/domain/definition as display context with their evidence authority attached.

Do not infer authority from a populated flat graph column. Keep BIAN/FIBO/process/synonym data as
evidence/search context unless a later field-specific policy is defined; do not force structured lists
through the current string resolver. The adapter is read-only; policy/evidence registration remains B8.

For every scalar read, verify one unambiguous latest decision head, selected-evidence existence and
evidence-set/value hashes under the pinned policy/resolver versions. A fork, missing evidence, conflict,
retired decision or degraded/lagged projection returns no operational value with a reason code; timestamp
ordering alone may not manufacture authority.

### C2. Candidate context and egress

**Runtime owner:** feature generation.

Modify `overlay/upload/feature_assist.py`:

1. widen `_candidate_columns` with schema, declared type, semantic terms and authority-qualified facts;
2. load parent-table context only for authorized candidate tables;
3. sanitize every free-text field through the Slice 1 field-aware egress projector;
4. retain structural fields through allowlist/bounds rather than definition sanitization;
5. make relevance selection deterministic and byte-budgeted;
6. return `CONTEXT_TOO_LARGE` when mandatory context cannot fit;
7. thread caller roles through generation, critique, refinement and every revalidation call.

The relevance contract is binding, not left to implementation taste: normalize objective tokens; include
all confirmed grain/as-of columns and objective-matched entity columns first; rank remaining columns by
entity match, then concept, then domain, with `(catalog_source, physical_ref)` as the total tie-break; fit
full records to the configured byte/item budget; summarize the remainder by table/domain/term type. Persist
selected/summarized/dropped counts, selection version and final bytes on the generation run. Mandatory
records are never summarized away. The egress audit records each emitted field and its projector decision,
not only one success bit for the whole prompt.

### C3. Candidate admission and contract tri-state validation

**Runtime owner:** feature generation.

Add result types in `feature_assist.py` or a focused `feature_validation.py` module:

```text
CandidateValidation:
  proposed(catalog_confirmations[], external_requirement_previews[])
  needs_catalog_confirmation(role_bindings[])
  rejected(reason_codes[])

ContractValidation (after selected role bindings are human-confirmed):
  design_checked
  needs_external_validation(requirements[])
  rejected(reason_codes[])
```

Initial requirement codes:

```text
TYPE_IS_NUMERIC
GRAIN_IS_UNIQUE
TEMPORAL_IS_POPULATED
TEMPORAL_LAG_BOUNDED
JOIN_CONNECTIVITY
CURRENCY_CONSISTENT
```

Define each code in a versioned `ValidationRequirementSchema` registry with typed subject refs,
parameters, result schema, unit and default blocking behavior. Candidate requirements are immutable value
objects; unknown code/version/parameters are programmer errors, not open JSON accepted from the LLM.
The LLM may identify uncertainty, but deterministic code selects the requirement code and builds its
validated parameters from server-known refs.

Update `_validate_idea`, `_vet`, refine, set generation, Gate 1 persistence, contract MCV and confirm-time
revalidation. Advisory concept/domain/business-term metadata may nominate a role binding, but it may never
produce `design_checked`. Gate 1 explicitly confirms the selected contract's role-to-column bindings;
global catalog authority remains unchanged. A physical-data hint may preview an external requirement, but
only post-selection deterministic validation creates the durable requirement.

Under the new feature-context flow, change transient `FeatureIdea.verification` from the current optimistic
`DESIGN-CHECKED` default to `UNVERIFIED`; only the confirmed contract validation event can earn a higher
effective stamp. Preserve legacy flag-off response bytes until rollout, then remove the compatibility path
after clients consume the explicit candidate/contract statuses.

Deterministic policy/safety violations remain `rejected`; lack of physical evidence becomes a bounded,
deduplicated external requirement. Missing join proof may become `JOIN_CONNECTIVITY` only when all proposed
keys are visible, authorized and structurally safe; a hidden/forbidden/unsafe key is rejection, not an
external escape hatch. Persist per-check disposition and reason so one unknown check cannot erase sibling
results.

### C4. Contract-version validation foundation

**Runtime owner:** feature generation and feature/contract APIs. This migration does not add any
feature or contract writes to ingestion.

Add migration `1004_feature_contract_validation.sql`:

- `feature_contract_validation_event`, append-only, keyed to a concrete `contract_id`;
- `feature_contract_validation_state`, a rebuildable current projection keyed by `contract_id`;
- `feature_validation_requirement`, immutable and keyed to `contract_id`, requirement schema version and
  metadata input fingerprint; external dataset binding is deliberately not embedded in this authoring row;
- closed status/reason vocabularies and database cross-field checks;
- initial event types `ASSESSED`, `EXTERNAL_PASSED`, `EXTERNAL_FAILED`, `INVALIDATED`, `SUPERSEDED`;
- no validation columns on mutable `feature` and no mutable requirements JSON on `contract`.

The effective state enforces: unresolved blocking requirements imply `needs_external_validation` and
`UNVERIFIED`; all deterministic checks passed with no blocking requirements imply `design_checked` and
`DESIGN-CHECKED`; a blocking negative check implies `rejected`; only current signed attestations can
produce `DATA-CHECKED`. Direct `POST /features` remains a legacy `UNVERIFIED/no_contract` registration and
cannot claim a validation disposition. Historical rows remain `legacy_unassessed` until replayed through
the stricter validator; never infer status from the old stamp.

Register the validation state as a sequence-guarded projection with checkpoint, reset/full replay,
degraded-state reporting and repair tests. API reads fail closed to `UNVERIFIED/unavailable` when that
projection is lagged or degraded; they never fall back to legacy stamp columns.

### C5. LLM/run/object provenance prerequisite

**Runtime owner:** ingestion audit persistence.

Add migration `1002_ingestion_llm_dispatch.sql`:

```text
llm_dispatch(dispatch_ref, logical_call_ref, attempt_no, ingestion_run_id, stage, task, input_hash,
             redacted_input, redaction/provider/model/prompt/schema versions, created_at)
llm_dispatch_subject(dispatch_ref, catalog_source, object_ref, logical_ref, field_names jsonb)
llm_dispatch_outcome(dispatch_ref, outcome response_received|transport_failed, recorded_at)
ingestion_run_llm_call(ingestion_run_id, llm_call_ref, stage)
llm_call_dispatch(llm_call_ref, dispatch_ref)
```

Do not replace the immutable `llm_call.run_id` idempotency bucket. Before **each physical provider
request**, including repair/retry attempts, write one immutable dispatch header plus exact redacted input
and subjects on an independently committed audit connection. Enforce unique `(logical_call_ref,
attempt_no)` and a deterministic input hash. The already-durable ingestion run is a valid FK target even
when upload data later rolls back. If this pre-dispatch
commit fails, do not call the provider; return `AUDIT_UNAVAILABLE` and fail only that enrichment stage.

After egress, change the durable outcome writer to accept all dispatch refs, return `llm_call_ref`, and
write the immutable logical `llm_call`, dispatch associations and ingestion-run association atomically. If
post-call outcome persistence fails,
the pre-dispatch record still preserves the exact approved payload and subject attribution; record an
operational repair task rather than creating an unlinked call on the request transaction. The asset API
must never expose raw stored prompts/outputs.

More precisely, a pre-dispatch row proves that egress was authorized and may have occurred. Append a
per-attempt dispatch outcome after the transport returns. A dispatch with no outcome after crash recovery is
reported conservatively as `egress_outcome_unknown`, never as “not sent”; the logical `llm_call` remains the
provider-result record.

`llm_dispatch` inherits the SENSITIVE classification, read controls and retention treatment of `llm_call`;
it stores only the already-egress-approved redacted request, never raw upload text or sample values.
Across Pass A/B/D and other ingestion enrichment, a provider result is not eligible for cache/evidence/
candidate persistence until the corresponding logical outcome audit has committed. Audit-outcome failure
discards the enrichment result and records the stage as audit-degraded/failed while core ingestion continues.

**Flag:** `FEATUREGEN_FEATURE_CONTEXT=1`, default off. This flag is read only by the feature-generation
workflow; it does not change upload behavior. C5 audit linkage is controlled with the corresponding
ingestion enrichment stage/config rather than this feature flag.

**Gate:** zero restricted or unsanitized outbound fields; FTR numeric hints produce
`needs_external_validation(TYPE_IS_NUMERIC)`, not rejection or design approval; every dispatched
batch is attributable to the ingestion run and exact object subjects.

## Delivery D - Semantic Binding Candidate Pass

**Purpose:** create reviewable relationships without letting the LLM invent identity or operational
truth. This is reusable catalog enrichment in Workflow 1, not feature idea or feature contract
generation.

### D1. Candidate contract and store

Add migration `1005_semantic_binding_candidate.sql` with immutable candidate sets, immutable candidates,
a deterministic current-set projection and a separate proposal association:

```text
semantic_binding_candidate_set:
candidate_set_id, catalog_source, table_graph_ref, ingestion_run_id, attempt_no,
metadata_input_fingerprint, task/prompt/schema/config versions,
completion_status complete|partial|failed, content_hash, created_at

semantic_binding_candidate:
candidate_id
candidate_set_id
catalog_source
subject_graph_ref
subject_logical_ref
binding_kind               currency_binding | entity_assignment
target_graph_ref nullable
target_logical_ref nullable
proposed_value jsonb
disposition                strong | weak | rejected
reason_codes jsonb
evidence_json jsonb
input_hash
model/prompt/schema/config versions
llm_call_ref nullable FK llm_call
created_at

current_semantic_binding_candidate_set:
catalog_source, table_graph_ref, candidate_set_id nullable,
metadata_input_fingerprint, status current|unverifiable, projected_at

semantic_binding_candidate_proposal:
candidate_id PK/FK
fact_key
proposed_event_id
created_at
```

Candidate set/evidence rows have physical no-update/no-delete triggers. Mint `candidate_id`
deterministically from candidate-set id, kind, subject, target/value and input hash, with a matching UNIQUE
constraint so retry/replay is idempotent. Add kind-specific CHECK constraints: currency requires a target
column and no free value; entity assignment requires a registry value and no target ref.

Mint `candidate_set_id` from ingestion run, stage attempt, source/table, metadata fingerprint and task
versions, with a matching UNIQUE key. Replaying the same attempt is idempotent; an explicit retry receives
a new attempt and may supersede a partial/failed attempt without mutating it. A later ingestion run remains
distinct audit history even when its content hash is unchanged.

Binding kind, disposition and reason codes are closed, versioned registries validated in code and by
database CHECKs where practical. Confidence remains evidence about this inference event; it is never copied
to a confirmed governed fact or used as promotion authority.

Only a `complete` set whose metadata fingerprint still matches the table may become current, using a
compare-and-swap projection update. `partial`/`failed` sets remain audit history and make currentness
`unverifiable`; they never silently preserve an old set as current for changed metadata. A complete empty
set is an explicit tombstone that retires the previous set. This avoids forked supersession chains and
gives absent candidates a lifecycle. The proposal association is inserted only after `propose_fact`
succeeds.

The table metadata fingerprint is a versioned canonical hash over the exact bounded `TableMetadataView`,
validated Pass B dispositions, Pass C identifier metadata and shortlist/config versions consumed by this
stage. It is an ingestion-stage input hash, not the C0 feature-generation snapshot and not `gn-v1`.

Provide a deterministic reset/rebuild command for the current-set projection: for each table fingerprint,
select at most one complete set by total `(created_at, candidate_set_id)` order and fail/degrade on an
impossible content-hash conflict. Projection loss must not require another LLM call to recover.

When a candidate leaves the current set, stale any linked DRAFT proposal. A VERIFIED fact is not revoked
merely because an LLM shortlist changed; it is invalidated only by its governed dependencies, while a
candidate disagreement opens a durable divergence/re-review signal.

### D2. Deterministic candidate shortlist

Create `overlay/upload/semantic_bindings/`:

- `types.py` - frozen candidate/evidence/disposition contracts;
- `shortlist.py` - pure, deterministic candidate enumeration;
- `validate.py` - referent, role, ambiguity and bound checks;
- `store.py` - immutable candidate-set persistence and current-set CAS projection;
- `propose.py` - mapping to governed fact commands.

Input is `TableMetadataView` plus validated Pass B synthesis and Pass C identifier metadata.

Rules:

1. Targets must come from the explicit same-table roster supplied by the server.
2. No raw or LLM-generated FQN is accepted.
3. Currency candidates are shortlisted from structural names, curated business terms/concepts and
   declared semantic facets; ambiguous candidates remain weak.
4. Event-time is not a new semantic-binding kind; it exclusively reuses the Pass B availability fact
   path and lifecycle.
5. Entity candidates target identifier-eligible columns and a value from `known_entities()`.
6. `term_type=measure` may exclude a join/entity-key candidate, but open-vocabulary term types never
   become operational classifications by themselves.
7. No sample-value shape inference is used.

### D3. Separate LLM failure domain

Do not widen the existing Pass B response with a large relationship array. Add a separate audited
structured task, `overlay.semantic_bindings`, so a provider/schema failure loses only semantic
proposals, not grain/availability/table metadata.

The model receives only server-minted candidate ids and safe metadata. Its response selects candidate
ids and supplies rationale/confidence; code rejects unknown ids. Bound candidates per table, total
provider calls, input bytes and wall-clock deadline through `enrich_config`. Provider/schema/deadline
failure writes a `failed` or `partial` candidate set and truthful stage counts.

Project every free-text field through the Slice 1 field-aware egress policy, retain structural fields only
through explicit allowlists/bounds, and use C5 pre-dispatch attribution. An egress or audit refusal makes
the semantic stage failed without dispatching or failing core ingestion. Do not persist or propose from a
provider response until its logical `llm_call` outcome record has committed and supplied `llm_call_ref`.

### D4. Ingestion wiring

Insert the new stage after glossary evidence and Pass B results are available, before the final
projection drain in `ingest_upload`.

```text
glossary_evidence
semantic_binding_candidates
semantic_binding_proposals
projection_drain
```

Each stage is savepointed and fail-soft like Pass B/Pass C. Add truthful candidate/proposed/abstained/
failed counts to `IngestResult` and `ingestion_run_stage.detail`.

On every re-ingest, compute the table metadata fingerprint and invalidate a mismatched current candidate
set even when semantic enrichment is disabled or has no client. This check performs no LLM call and keeps
the old set as history while reporting currentness `unverifiable`; disabling the producer must not freeze
stale proposals as current.

Add both stages to `CANONICAL_STAGES`, early-exit filling, `IngestResult`, upload/connector response types
and `_effective_config_snapshot`. Record disabled/not-applicable states even when the stage does not run.
Set a tested maximum added latency and call count for a 126-column FTR table; exceeding either yields a
partial/failed semantic stage without changing upload acceptance.

**Flags:** `OVERLAY_SEMANTIC_BINDING_CANDIDATES=1` enables candidate-set persistence only;
`OVERLAY_SEMANTIC_BINDING_PROPOSALS=1` separately enables governed DRAFT proposals and is invalid unless
the candidate flag is on. Both default off. Neither flag permits VERIFIED promotion.

**Gate:** curated FTR and ambiguity corpus has zero false target refs; unknown/fabricated model refs are
dropped with durable reason codes. The ingestion acceptance test asserts that feature and contract row
counts are unchanged and that no feature-assist/compiler entrypoint was called.

## Delivery E - Governed Semantic Facts and Projection

**Runtime owner:** catalog governance commands in Workflow 2. Only reapplication of previously
VERIFIED facts after `build_graph` is allowed inside Workflow 1.

### E1. Fact types

Extend every closed fact/lifecycle registry explicitly: `overlay/facts.py`, `_types.py`, event schema
registration/decoding, `DATA_FACT_TYPES`, `FACT_VALUE_SCHEMAS`, `identity.py`, `dependencies.py`,
authority resolution, referent checks, expiry/reverify handling, task reads and projection replay:

```text
entity_assignment  value={entity_id}
currency_binding   value={currency_column: CatalogObjectRef}
```

Use the current public-flattened `CatalogObjectRef` for fact identity in this release. Enforce:

- entity is in `known_entities()`;
- currency target exists exactly once;
- currency subject and target are columns in the same source/table;
- proposed value matches the fact subject and candidate;
- no cross-schema/cross-source binding through this path;
- `use_case` is absent.

Fact dependencies include the subject and every target ref plus the exact field-decision/fact ids used to
justify the proposal. Re-proposing a changed target follows the existing terminal/reverify lifecycle; it
may not mutate a VERIFIED value in place.

Entity assignment and currency binding use one authorized human confirmer because both are constrained to
one owned table; four-eyes still requires the service/LLM proposer and human confirmer to differ. Replace
the current route-only raw `platform-admin` gate with a field/fact-specific authorization service that
accepts the registered source owner or platform admin and lets `resolve_authority` make the final decision.

### E2. Proposal and review surfaces

Mirror Pass C and table-fact governance:

- `GET /sources/{source}/governance/semantic-bindings`;
- `POST /governance/semantic-bindings/{fact_key}/confirm`;
- `POST /governance/semantic-bindings/{fact_key}/reject`.

Responses include candidate evidence, target metadata, candidate-set/ingestion-run provenance, prior
value, CAS target event, reason codes and reviewer note. Confirm/reject must dispatch through existing
overlay commands.

Add reverify/withdraw/correct actions for an already VERIFIED binding. The asset UI may not advertise an
edge as editable unless the server returns one of these commands. Every command carries an idempotency key,
target event id, actor, bounded note and tamper-evident security audit result.

Confirming an ingestion-created DRAFT uses the service candidate as proposer and one authorized human as
confirmer. A human-originated new/corrected binding is first proposed and requires a different authorized
human to confirm; “single-owner” does not permit one person to propose and approve the same value.

### E3. Verified projection

Add migration `1006_semantic_binding_projection.sql`:

- `semantic_binding_edge(fact_key PK, catalog_source, kind, from_ref, to_ref,
  confirmed_event_id, status, projected_at)`;
- `graph_node.declared_entity`, `entity_fact_key`, `entity_fact_event_id`, `entity_status`;
- indexes by endpoints and status.

Project synchronously after confirmation:

- VERIFIED `entity_assignment` -> `graph_node.entity` plus provenance links;
- VERIFIED `currency_binding` -> `semantic_binding_edge`;
- non-VERIFIED transition -> immediate demotion/removal from operational reads.

`declared_entity` preserves source display metadata. A current VERIFIED `entity_assignment` always wins
the effective `graph_node.entity` projection. A conflicting re-upload records a divergence/reverify signal;
it never silently overwrites the governed value. Demotion restores `declared_entity` as labelled display
context, clears governed provenance, and rebuilds `search_doc` in the same transaction.

After every `build_graph`, reproject every VERIFIED `entity_assignment` fact for the affected source
after the projection drain, following the existing table-fact and approved-join reapply pattern. A
re-ingest must not erase a governed entity binding.

Extend expiry, reject, drift and re-ingest hooks. A dropped/retyped target must stale the fact through
`overlay_fact_dependency`; operational readers also require `status='VERIFIED'` as a second gate.

Implement the semantic read model as a registered projection with checkpoint, deterministic `reset()`,
full event replay, sequence guards, poison/degraded reporting and repair registration. Synchronous
confirmation projection is an optimization; rebuilding from the event stream must reproduce the same
entity and currency state.

### E4. Existing entity suggestion migration

Keep reads of legacy `entity_suggestion.status='applied'`, but mark their authority as
`legacy_file_declared`, not governed. New apply actions create/confirm `entity_assignment` facts. Add a
one-time backfill tool that proposes legacy assignments for review; do not auto-verify them.

**Gate:** re-upload, target drop, reject and expiry all remove operational entity/currency projections
without deleting audit history.

## Delivery F - Column Readiness, Provenance and Asset API

**Runtime owner:** shared catalog read model. It does not generate feature contracts.

### F1. Column readiness

Create `overlay/upload/column_readiness.py`. Do not overload catalog/table readiness and do not use a
fixed denominator.

Readiness is a capability matrix, not one context-free score. With no requested use, return separate
capabilities such as `as_measure`, `as_entity_key`, `as_event_time`, `as_grain_key` and `as_join_key`.
Feature generation supplies its intended role/use and reads only that capability. Requirements may include:

```text
identity
semantic_role/concept
operational_type or TYPE_IS_NUMERIC external requirement
additivity authority
table grain
event/as-of time
entity key
currency binding for monetary measures
join connectivity when another table is required
safety classification
freshness
```

Return status, authority, evidence/fact ids, blocking flag and reason codes per requirement and capability. Column
readiness is diagnostic. It may advertise that a future feature would require an external check, but
it does not create a contract-specific requirement row or trigger that check. Recipe-level operational
truth remains the existing planner/compiler verdict.

### F2. Core asset read model (F0)

Create `overlay/upload/asset_detail.py` and `api/routes/assets.py`.

```http
GET /catalog/assets/{source}/{object_ref:path}?include=identity,effective_metadata,...
```

Define a versioned response schema, stable ordering, per-section cursor contracts, maximum page sizes,
projection/snapshot consistency token and `ETag`. Assemble all requested sections under one repeatable-read
transaction; do not issue per-node/per-edge queries. The response contains bounded sections:

- `identity`: graph, logical and physical refs, source/table/column, operational and declared types;
- `effective_metadata`: display values plus authority/provenance;
- `evidence`: active/stale/rejected proposals and latest decisions for permitted fields;
- `relationships`: containment and approved joins in F0; semantic candidates/verified edges arrive in F1;
  registered features and consumers are a separately authorized subsection;
- `readiness`: column requirements plus the parent table diagnostic;
- `history`: reverse `ingestion_run_object` lookup and stage outcomes; subject-linked LLM audit summaries
  are a separately authorized subsection;
- `actions`: server-calculated commands allowed for the caller.

Apply catalog read permission and sensitivity filtering before loading evidence. A hidden anchor returns
404. Related hidden nodes/edges are omitted. `feature:read` is required before loading registered features
or consumers. Add an `audit:read` capability for safe LLM-call summaries; raw/redacted inputs, raw outputs
and repair bodies remain restricted to the existing audit store and are never returned here. Governance
actions require their write/confirm capabilities and source ownership. Section omission due to permission
is explicit in `unavailable_sections` without revealing hidden row counts. Apply filtering in SQL before
counts, ordering and opaque cursor construction so pagination metadata cannot leak hidden assets.

Add `audit:read` to the permission registry but not to catalog-viewer, data-owner or feature-engineer
bundles; grant it only to platform admin and an explicitly provisioned audit role. Permission-denied tests
cover both complete section omission and direct audit-route access.

Add composite indexes for reverse run history `(catalog_source, object_ref, at DESC)`, current semantic
sets/edges, field decision/evidence lookup and feature reverse lineage. Set an SQL query-count and p95
latency budget and verify representative 126-column and larger-table plans with `EXPLAIN (ANALYZE, BUFFERS)`.

### F2b. Semantic relationship sections (F1)

After Delivery E is enabled and observed, add semantic candidate history, divergences, VERIFIED entity/
currency edges and server-calculated governance actions to the same versioned response. F0 returns the
section as `unavailable` rather than inventing empty success, allowing the read-only page to ship before
semantic governance without changing the response envelope.

### F3. Generic scalar correction command

Create a service/API for fields governed by `field_evidence`:

```http
POST /catalog/assets/{source}/{object_ref}/fields/{field}/decisions
```

Use an explicit command body with action `confirm_existing`, `propose_override`, `confirm_override` or
`reject`, selected evidence ids or bounded replacement value, reason/note, idempotency key, expected latest
decision id, expected evidence-set hash and expected policy version. The server rechecks source
ownership/field authority and four-eyes rules,
appends human evidence plus a decision event, re-runs resolution and projects display/search. Concurrent
evidence arrival causes 409 even if the latest decision has not yet changed. The command never overwrites
evidence, accepts an unregistered field, trusts a client authority label, or returns an action the caller
cannot execute.

`propose_override` appends non-load-bearing human evidence and opens a review task; it never projects the
new value in the same command. `confirm_override` requires a different authorized subject and the exact
proposal/evidence-set CAS target before human evidence becomes CONFIRMED. `confirm_existing` likewise
checks that the confirmer is not the evidence proposer. Rejection may be single-reviewer but cannot write
an operational replacement.

Policies opt in with `human_editable=true`; identity, physical type, sensitivity/floor and specialized
grain/time/join/entity/currency facts are excluded from this generic route and retain their dedicated
commands. Reject values outside field-specific bounds/vocabularies before writing evidence.

**Gate:** one API test proves the response can be built from a real synthetic FTR ingestion with no
hardcoded UI values; read-scope tests prove hidden evidence and related nodes do not leak.

## Delivery G - Frontend Asset Experience

**Runtime owner:** catalog/search product workflow. Feature authoring may navigate here, but opening or
editing an asset does not create a feature contract.

**Files:** `frontend/src/api.ts`, `SearchScreen.tsx`, `AssetDetailScreen.tsx`, routing/navigation, CSS and
tests. Replace the sample screen only after the backend contract exists.

**Validated design target (checked against the code, 2026-07-19):** the prototype
`frontend/src/screens/AssetDetailSampleScreen.tsx` (currently untracked in the main checkout, wired via
modified `App.tsx`/`nav.ts`) already IS the visual spec for F0/F1/G. It renders five tabs
(overview / metadata+evidence / relationships / readiness / history), authority badges
(`source declared` / `system derived` / `llm proposed` / `human staged`), the two-type-field honesty
(declared `double` vs operational `unknown`, both `eligible: no` with the explicit note that only a
technical source attests operational type), the 6-requirement readiness matrix ("1 / 6 ready, blocked"),
a read-only react-flow neighborhood graph that draws proposed edges distinctly from verified ones, and a
"Stage correction creates a new human evidence layer, it does not rewrite the source" review drawer. It
embodies this plan's entire authority/honesty thesis. **Consequence for sizing:** F/G is a
"make-the-mockup-real" job (wire it to the real asset-details API with read-scope + permission filtering,
OCC tokens, and 409 handling), NOT a UX-discovery job - the design risk is largely retired. Every hardcoded
value in that file (`ASSET_REF`, the `1 / 6` readiness, `GRAPH_NODES`, the `HistoryTab` timeline,
`run_01J3FTR7P2K8`) must exist ONLY as test/story fixtures in production per Delivery G's gate; the current
"No backend data was written" staging is local React state and must become the real
`POST /catalog/assets/.../fields/{field}/decisions` command (F3).

1. Add a `Details` action on each search result; route with encoded `source` and `object_ref`.
2. Fetch the asset endpoint; do not join search/lineage/readiness/history in the browser.
3. Render authority and lifecycle from the response, never infer them from non-empty values.
4. Render proposed relationships separately from VERIFIED operational relationships.
5. Use correction/confirm/reject commands with the response OCC token; handle 409 by reloading.
6. Keep graph nodes/edges read-scoped and show unavailable sections honestly.
7. Add desktop/mobile Playwright checks for overflow, nonblank graph canvas and navigation from search.

**Gate:** the current hardcoded `TRAN_AMT`, dates, readiness score, history and draft feature are absent
from production code and come from fixtures only in tests/story data.

## Delivery H - Feature Assist and Existing Planner Integration

**Runtime owner:** Workflow 3 only. Entry requires an explicit feature-generation request with intent,
caller and use-case context through the existing assist/contract routes. No ingestion stage imports or
invokes this orchestration entrypoint.

### H1. Feature assistant

At each feature-generation route entry, set `REPEATABLE READ` before any catalog query, build C0 once, and
under `FEATUREGEN_FEATURE_CONTEXT` feed that immutable C/F context into recommendation, refinement, set
generation and MCV validation. A check clears only when the field-specific adapter returns a current,
conflict-free load-bearing value or VERIFIED fact. Technical type may clear only through an attesting
technical-source capability profile. Include every other value as labelled context and convert physical-
data uncertainty into external requirements.

Update `FeatureIdea`, API models and considered-set persistence to carry:

```text
generation_source             recipe | llm_freeform | user_defined
recipe_id nullable
candidate_status
input_role_bindings[]         role, source/ref, evidence/fact ids, authority,
                              confirmation_required
external_requirement_previews
metadata_snapshot_id/fingerprint
binding fact keys used
planner_applicability         applicable_cross_catalog | gated_off |
                              not_applicable_single_catalog | not_applicable_nonrecipe
physical_plan_id/planner_declaration_id nullable
```

Update `_idea_from_grounded`, `_idea_json` and `_idea_from_json`; the server-persisted considered set may
not lose `recipe_id` during Gate 1 serialization/reconstruction. LLM free-form output cannot supply or
upgrade its own `recipe_id`; the server assigns `generation_source` for recipe, model and user-anchor paths.

Extend Gate 1 confirmation to persist the exact server-generated role bindings, the confirming actor/time
and a hash of those bindings. The UI/API must display role, column, source, authority and warnings before
confirmation. A changed binding hash returns 409. This confirmation is scoped to the selected feature
contract and does not write global catalog field/fact authority.

Persist candidate requirement-preview content/schema versions and hashes in the considered set. After
role-binding confirmation and deterministic revalidation,
mint durable requirement ids from the new `contract_id` plus requirement content hash; never accept
client-supplied requirement ids, codes, parameters or “passed” states.

Confirm-time revalidation must fail or downgrade if any referenced fact drifted, expired or became
unauthorized.

Until 3C.2 is independently enabled, reject a candidate whose selected inputs span catalogs with
`CROSS_CATALOG_GROUNDING_NOT_ENABLED`. Modify `contract/author.py` so the new flow never calls the
permissive `find_cross_catalog_path`; a multi-catalog contract requires a governed selected physical plan,
the live flag and a valid signed 3C gate artifact.

### H2. Contract persistence

Persist selected input columns, entity/time/currency fact keys, field decision ids, requirement ids and
snapshot id on each contract version. Do not derive them again from mutable flat graph columns after
confirmation.

For non-planner single-catalog contracts, deterministically expand the chosen derives, governed join path,
grain/as-of columns and any aggregation support columns into the same role-labelled contract-input shape.
Unknown or ungoverned support columns become requirements/rejection; they are never omitted from lineage.

Add migration `1007_contract_metadata_inputs.sql` for immutable-per-version input material:

```text
contract.metadata_snapshot_id FK
contract.metadata_input_fingerprint
contract.generation_source/recipe_id nullable
contract.physical_plan_id/planner_declaration_id nullable
contract.initial_validation_status/initial_verification
contract_input_column(contract_id, source, graph/logical/physical refs,
                      decision/fact ids, item_hash)
contract_metadata_dependency(contract_id, catalog_source, graph/logical ref,
                             decision/fact/event id, item_hash)
feature_current_contract(feature_id PK, contract_id, pointer_version, set_at)
feature_versions.contract_id nullable FK
```

Contract input rows and contract input material are physically write-once. Contract confirmation takes a
transaction-scoped advisory lock on normalized feature identity before the first lookup, inserts the next
contract version and input rows, appends its initial validation event,
and compare-and-swap updates `feature_current_contract` atomically. `feature` and
`feature_derives_from` become current compatibility projections from that pointer; they are not historical
truth. Attestation and drift always target `contract_id`, and update the feature projection only when that
contract is still current.

When the pointer advances, append `SUPERSEDED` to the prior contract validation stream and cancel any
undelivered external submissions for it. A later response for that superseded submission is rejected and
audited as stale; it never reopens or promotes the feature.

Populate a reverse dependency row for every snapshot item used to clear a check or bind an input. Catalog
drop/type/rename, field-decision retirement/conflict, fact stale/expiry/reject, policy-version change and
input sensitivity/policy changes append `INVALIDATED` to affected contract versions. Read-time status
also compares current ids/hashes as a second fail-closed gate so projection lag cannot serve a stale
DATA-CHECKED contract.

Add a no-update/no-delete trigger to `contract` after confirming no existing route mutates contract rows.
All later lifecycle changes are validation events or current-pointer changes, never contract mutation.

Before enabling writes, audit existing orphan rows; the migration fails with a remediation report rather
than deleting or reparenting them. Then add the missing `contract.feature_id -> feature` FK,
add a unique `(feature_id, contract_id)` key, and make `feature_current_contract` reference that composite
key so a contract cannot become current for another feature. Contract snapshot, input and requirement FKs
are mandatory; graph refs remain historical strings because graph rebuilds may remove their live nodes.
Provide a deterministic repair command that rebuilds `feature_current_contract` from the highest valid
confirmed contract version under the advisory lock and then refreshes compatibility feature/lineage rows.
Backfill each contracted legacy feature's pointer to its latest existing contract as
`legacy_unassessed`; do not fabricate snapshot/input/requirement rows. Directly registered features with no
contract keep no pointer and remain `UNVERIFIED/no_contract`.

Update feature/contract list and detail APIs to expose current contract id/version, initial and effective
validation/stamp, requirements, snapshot fingerprint, planner applicability/ids and invalidation reasons.
History is read from immutable contract versions/events, never reconstructed from the mutable feature row.

### H3. Planner/compiler

Invoke 3B.3c only when `generation_source=recipe`, a preserved registry `recipe_id` resolves to the exact
`Template`, and the selected physical plan binds that recipe with
`path_resolution_status=source_to_target_resolved`. Recipe-backed single-catalog and free-form ideas use
the single-catalog feature validator and record the appropriate planner non-applicability; user-defined
ideas follow the same nonrecipe rule. Do not synthesize templates or need roles from LLM text.

For an applicable recipe, require exact agreement between the chosen idea's ingredient refs and the
plan's ingredient bindings. Persist the compiler's full physical read set, including join keys, anchors,
weights and ordering columns, as role-labelled contract inputs; lineage based only on the original
`derives_pairs` is incomplete and may not drive drift checks or external submission.

Resolve planner ids only from the server-persisted generation run/considered-set result. At confirmation,
rebuild/revalidate the selected plan against the current repeatable-read snapshot and require stable
physical/declaration ids plus a current resolved freshness verdict; drift returns 409/retry. Never accept a
client-supplied physical plan or attach a shadow telemetry row without this revalidation.

Replace planner `_load_columns` consumption of unqualified flat `graph_node` fields with a compiler
adapter over the immutable C0 snapshot. Candidate discovery may use labelled advisory concept/business
metadata to nominate plans, but the planner declaration does not certify semantic role correctness; the
feature contract requires the Gate 1 role-binding confirmation above. Structural declaration checks use
only values whose field-specific policy permits the operation. Planner fingerprints include the metadata
ids/values and policy versions consumed by plan discovery/compilation; the separate feature-contract
metadata fingerprint includes the later confirmed role-binding hash. Attachment requires exact binding
agreement and does not alter the pre-existing planner declaration id. Do not change existing 3B.3c
physical plan ids. If new metadata changes declaration inputs:

1. bump the relevant contract/rule/registry versions;
2. include new fact keys/values in compiler input fingerprints and replay envelopes;
3. rerun deterministic replay, gold and 3B.4 enablement checks;
4. invalidate prior sign-off by version, not by manual convention.

Create a durable, versioned recipe aggregation-declaration registry only for functions that cannot be
soundly derived by the existing compiler. Load it in `build_compiler_context`; never infer a global
aggregation from the column.

Add migration `1008_recipe_aggregation_declaration.sql` with recipe id, need role, function, declaration
version, authority/provenance, effective interval and immutable content hash. Its read projection must
select exactly one active declaration per `(recipe_id, need_role)` or fail the compile as conflicting.

The live cross-catalog grounding flip remains 3C.2 and still requires a current signed PASS artifact from
the 3C.1 gate. Add a startup/runtime verifier for artifact signature, gate/schema/compiler versions and
expiry. The new flow fails closed when the flag is on but the artifact is absent/stale. The permissive
`find_cross_catalog_path` is never a fallback.

**Gate:** an FTR monetary feature is retained as `needs_external_validation`, carries explicit
type/grain/time/currency requirements after its exact measure/time/entity role bindings are human-confirmed,
and never receives a false DESIGN-CHECKED result. A single-catalog
recipe fixture preserves `recipe_id` through Gate 1 and records `not_applicable_single_catalog`; a
free-form/user-defined fixture records `not_applicable_nonrecipe`; a multi-catalog fixture is refused while 3C.2 is
disabled; and a gated test with a valid signed 3C.2 artifact attaches the expected physical/declaration ids.

## Delivery I - External Validation Feedback

**Runtime owner:** Workflow 4, after a feature contract version already exists. Ingestion cannot create,
submit or satisfy these requirements.

Add migration `1009_external_validation_protocol.sql`:

```text
external_dataset_binding
external_platform_key
external_validation_submission
external_validation_submission_item
external_validation_attestation
external_validation_result
```

Consume the C3 requirement-schema registry and C4 requirement rows. Every requirement has `requirement_id`,
`contract_id`, code/schema version, subject physical/logical refs, typed parameters, expected result
shape/unit, blocking flag, metadata input fingerprint, content hash and creation time. Submission items bind
each requirement to an immutable external dataset-binding version; authoring requirements remain unchanged.
Examples: uniqueness includes the exact key columns and null policy; lag includes event/availability refs,
maximum lag and time unit; currency consistency includes amount/currency refs and accepted null/mixed rules.
Unknown code/version/parameters fail before submission.

`external_dataset_binding` maps this platform's physical refs to one external platform, environment and
dataset namespace. It is versioned, owner-confirmed and immutable-per-version. `external_platform_key`
stores Ed25519 public keys with platform/environment scope, valid interval and revocation state.
Add audited admin/owner APIs for binding creation/retirement and public-key registration/rotation/revocation;
the platform never receives or stores the external platform's private signing key.

`external_validation_submission` is a transactional outbox with submission id, contract/version,
requirement ids, binding version, canonical payload, payload hash, state, attempt count, next-attempt time,
ack id and timestamps. Supply an authenticated pull/ack API as the baseline transport; adapter-specific
push workers may consume the same outbox. Authenticate the pull/ack caller through the existing verified
service-identity/JWKS path and require its platform/environment claims to match the submission. Retries are
idempotent by submission id and payload hash.

The versioned submission payload includes the contract id, metadata fingerprint, requirements, dataset
binding, platform/environment, issue/expiry times and nonce. The signed response covers canonical RFC 8785
JSON containing submission id, requirement id, contract id, input fingerprint, platform/environment,
dataset/snapshot id, observed typed result/unit, outcome `passed|failed|error`, checked/expiry times, nonce
and key id. Enforce key validity/revocation, payload hash, signature, audience/environment, nonce uniqueness,
submission acknowledgement and a bounded replay window before writing any result.

Add an adapter interface and endpoint that verifies the external platform identity/signature before
accepting results. A client-supplied `passed=true` without verification is rejected. Results must match
the exact contract version, requirement id and input fingerprint.

Attestations/results and validation events are append-only. Projection rules are deterministic:

- every blocking requirement has a current, unexpired pass -> `DATA-CHECKED`;
- any blocking failed result -> `rejected` with `EXTERNAL_CHECK_FAILED`;
- missing/error/expired/revoked result -> `needs_external_validation`/`UNVERIFIED`;
- catalog, contract, dataset-binding or metadata-fingerprint drift -> append `INVALIDATED` and require a
  new submission; historical attestations remain immutable.

An attestation affects only its contract version. It reaches the compatibility feature projection only if
`feature_current_contract` still points to that contract. Key compromise/result withdrawal is represented
by an append-only revocation event that immediately invalidates dependent validation state.

**Gate:** signature failure, unknown/revoked key, wrong environment/audience, stale contract version,
duplicate nonce/submission replay, malformed typed result, partial result set, expired attestation and
post-attestation catalog/dataset drift all fail closed. An end-to-end adapter test pulls a real submission,
returns a signed result and proves both promotion and later invalidation.

## Delivery J - Test, Evaluation and Rollout

### Test layers

1. Pure unit tests for shortlist, validators, identity mapping, authority adapter and capability readiness.
2. PostgreSQL tests for migrations, immutable candidate-set currentness, idempotency, OCC and projection
   reset/replay.
3. Two-cycle ingestion tests for unchanged, changed, absent, dropped and retyped metadata across glossary
   and technical source profiles, proving FTR hints never gain technical-source authority.
4. API tests for read scope, permissions, 404 opacity, pagination and response contracts.
5. Anthropic live canaries for every new prompt/schema version.
6. Synthetic FTR end-to-end: 126 columns, wide-table path, no operational type, all stage counts; a
   two-schema fixture that collapses to one public graph ref is HELD rather than silently merged.
7. Curated semantic-binding gold set with ambiguous and adversarial column names.
8. Feature-assist baseline-vs-context eval with zero unsafe accepts and bounded cost/latency.
9. Planner replay/fingerprint tests and a fresh 3B.4 signed gate artifact after version changes.
10. Frontend component tests and Playwright desktop/mobile navigation/edit/graph checks.
11. Runtime-boundary tests: upload creates no feature/contract rows, invokes no feature compiler and
    submits no external checks; feature generation reads only committed catalog state from a separate
    transaction and stores the exact metadata fingerprint it used.
12. Snapshot-concurrency test: ingestion commits between feature-context queries, yet one feature request
    observes either the old or new complete state and never a torn mix.
13. Contract-version test: v1 attestation cannot promote/demote v2 or the current feature pointer.
14. Candidate lifecycle tests for retry idempotency, complete-empty tombstone, failed changed snapshot,
    concurrent current-set CAS, stale DRAFT proposal and unchanged VERIFIED fact.
15. Recipe/free-form tests proving `recipe_id` survives Gate 1, arbitrary ideas never enter 3B.3c, and the
    permissive cross-catalog author path is unreachable.
16. External protocol tests for canonical bytes/signature, key rotation/revocation, nonce replay,
    environment mismatch, outbox retry/ack, attestation expiry and dataset/catalog drift invalidation.

### Flag matrix

```text
OVERLAY_TABLE_SYNTH=1                  prerequisite after Slice 2 gate
OVERLAY_PASS_C=1                       prerequisite for governed join candidates
OVERLAY_SEMANTIC_BINDING_CANDIDATES=1  new; immutable candidate sets only
OVERLAY_SEMANTIC_BINDING_PROPOSALS=1   new; DRAFT fact proposals, depends on candidates
FEATUREGEN_FEATURE_CONTEXT=1           new; enriched feature context
FEATUREGEN_INTENT_CONTRACT_COMPILE=1   existing shadow compiler
FEATUREGEN_INTENT_SHADOW_TELEMETRY=1   existing durable shadow telemetry
FEATUREGEN_EXTERNAL_VALIDATION_SUBMIT=1 new; outbox delivery only
FEATUREGEN_EXTERNAL_VALIDATION_ACCEPT=1 new; signed-result endpoint/projection
FEATUREGEN_GOVERNED_GROUNDING_LIVE=1   future 3C.2 only after signed PASS
```

Do not turn all flags on merely because code exists. Each flag has its own corpus/gate, and the live
grounding flag is forbidden before 3C.1 passes. Add ingestion flags to `_effective_config_snapshot` and
canonical stage reports. Add feature/external flags, snapshot/version ids and signed-gate identity to the
generation/submission manifests. Startup validation rejects invalid dependencies such as proposals without
candidates or live grounding without a valid gate artifact.

### Rollout order

Catalog/ingestion lane:

1. Merge A/B and deploy C5 provenance plus C0/C1 read foundations with consumers off.
2. Ship F0 core asset API and frontend read-only; semantic sections report unavailable.
3. Persist semantic candidate sets, no governance proposals.
4. Enable DRAFT proposals and human review, no feature consumption.
5. Enable confirmed projection, F1 relationship sections and edit actions after drift/expiry/replay gates.

Feature-generation/external lane, enabled independently after committed catalog inputs are available:

6. Run feature context, candidate admission and contract tri-state validation in shadow/baseline eval from
   explicit requests.
7. Deploy contract-version snapshot/input/validation lifecycle, then enable single-catalog authoring.
8. Enable external submission transport against a non-production adapter, then signed result acceptance.
9. Promote external feedback only after replay/revocation/drift tests and operational key runbooks pass.
10. Enable governed cross-catalog grounding only after the independent 3C.1 signed gate passes; never fall
    back to `find_cross_catalog_path`.

## Size and Staffing

This is a program, not one pull request.

| Delivery | Size | Sequential person-days |
|---|---:|---:|
| A. Baseline convergence | S | 2-4 |
| B. Slice 2 + source-authority prerequisites | L | 8-13 |
| C. Snapshot, adapter, provenance and validation foundation | XL | 20-30 |
| D. Semantic candidate pass | XL | 10-16 |
| E. Governance and projection | XL | 14-22 |
| F. Readiness, provenance and asset API | XL | 12-18 |
| G. Frontend asset experience | L | 7-11 |
| H. Feature-contract/planner integration | XL | 18-28 |
| I. External validation protocol | XL | 16-26 |
| J. Cross-cutting hardening/gates | XL | 10-15 |

**Full sequential estimate:** 117-183 engineering days as originally scoped. **Read this net of Slice 3**
(now merged): the C2/C3 validator, egress, relevance and a working confirm flow are delivered, so the C/H
lane's remaining work is the durability upgrade (immutable snapshot, event-sourced validation,
contract-version immutability, role-binding model), not a from-scratch build - shave the already-shipped
portion off the C and H rows before committing budget. Slice 2 remains unimplemented and is still counted.
The adversarial review exposed architecture work previously misclassified as wiring: contract-version
reconciliation, immutable snapshotting, external transport/trust and planner adaptation. The uncertainty is
concentrated there, not in CSV parsing. **Do not commit to the full number as one decision** - fund by the
three tranches in "Funding Gates and Recommended Sequencing."

With two backend engineers and one frontend engineer, the dependency graph supports approximately
16-25 calendar weeks, subject to external-platform availability. A first-release F0 read-only asset page
plus honest single-catalog Phase 2 feature context and contract-version foundation, without new semantic
governance or external attestation, is roughly 51-77 person-days.

Approximate effort by runtime ownership makes the boundary explicit:

| Workstream | Included work | Sequential person-days |
|---|---|---:|
| Ingestion/catalog supply | A, B, C1, C5, D, E, F, G and its J share | 68-110 |
| Feature generation/validation | C0, C2-C4, H, I and its J share | 49-73 |

These streams integrate through committed metadata identifiers and fingerprints, not by placing
feature generation inside the upload transaction.

## Pull Request Boundaries

Keep each pull request behaviorally bounded:

1. baseline merge only;
2. Slice 2 validation/lifecycle;
3. technical/glossary source-field evidence, policies, profile versioning and lifecycle;
4. ingestion LLM/run/subject provenance only;
5. immutable metadata snapshot tables/builder and concurrency tests;
6. shared read-only operational-facts adapter;
7. contract-version validation event/state foundation and historical compatibility;
8. authority-aware context/egress/relevance builder, not yet consumed;
9. semantic candidate-set store + pure shortlist/validator;
10. semantic LLM task + ingestion wiring, with no-feature-write and stage-manifest tests;
11. governed fact registries + proposal/review commands;
12. semantic projection reset/replay/demotion/drift and re-ingest reapplication;
13. capability readiness + F0 asset read model/performance indexes;
14. scalar correction commands and authorization reconciliation;
15. F1 semantic relationship/review API sections;
16. frontend read-only;
17. frontend edit/review;
18. explicit-request feature-assist consumption, role confirmation and preserved recipe provenance;
19. immutable contract inputs/current pointer/validation integration;
20. recipe-only planner adapter, fingerprints and gate requalification;
21. external dataset/key/requirement/outbox contract;
22. signed result ingestion, projection, revocation and invalidation;
23. final eval, runtime-boundary and flag rollout artifacts.

No pull request should combine migration, new LLM behavior, governance promotion, feature consumption
and frontend editing. Those need independently reviewable failure domains.

## Explicitly Out of Scope

- graph re-key from `public.*` to physical schema identity;
- value-shape identity inference;
- direct access to customer data;
- SQL generation/execution inside this platform;
- automatic feature registration during ingestion;
- automatic human confirmation based on LLM confidence;
- cross-table currency binding before same-table binding is measured;
- live cross-catalog planner output before the signed 3C.1 gate passes.
