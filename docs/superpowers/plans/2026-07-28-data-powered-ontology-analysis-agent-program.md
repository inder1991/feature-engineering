# Data-Powered Ontology and Governed Analysis Agent — Implementation Program

**Date:** 2026-07-28  
**Status:** Proposed implementation plan, revision 1  
**Primary outcome:** A user can ask a governed business question in natural language, the
platform can run a bounded and reproducible analysis against approved Hive data, and the UI can
show validated tables, charts, and a grounded summary. The same data-plane observations improve
the ontology through governed evidence rather than unchecked inference.

> **Sequencing and shared-contract ownership live in
> [`2026-07-28-product-roadmap-and-contract-ownership.md`](2026-07-28-product-roadmap-and-contract-ownership.md).**
> This programme is the master product roadmap's detail; where the two disagree on release order,
> contract ownership or link policy, the roadmap wins. In particular §5.3's generic ontology
> candidate contracts are REMOVED in favour of the single shared candidate substrate.

## 1. Architectural ruling

Build this as four separately deployable capabilities:

1. **Metadata ingestion** imports what the catalog says exists.
2. **Data observation** runs after ingestion, inside the data environment, and returns bounded
   aggregate evidence.
3. **Ontology intelligence** converts metadata and data observations into versioned candidates
   which governance may accept, reject, supersede, or leave unresolved.
4. **The analysis agent** translates a user's question into a typed, governed analysis plan;
   deterministic code executes that plan and produces validated results.

The uploaded CSV is metadata input. Values such as `source_type`, `source`, `schema`, `table`, and
`column` may nominate a physical dataset, but they must not cause the control plane to open an
unregistered connection or trust a table identity. A separately governed connection and dataset
binding authorizes all data access.

The agent does not generate arbitrary SQL and send it to Hive. The LLM may interpret intent and
propose ontology candidates. A deterministic planner resolves governed entities, measures,
dimensions, relationships, time semantics, population, point-in-time policy, and physical inputs.
A compiler then emits a bounded Kedro/PySpark project or a supported source-native query from that
typed plan.

## 2. What is verified today

This plan must start from the repository as it exists, not from the desired architecture.

| Capability | Verified baseline | Not yet earned |
|---|---|---|
| Metadata ingestion | Durable ingestion runs, append-only status events, heartbeats, lease reconciliation, metadata graph ingestion | A post-ingestion data-observation trigger |
| Existing profiler | Bounded PostgreSQL/catalog profiling with allowlists, sampling and timeouts | Hive/Spark profiling; it cannot be reused as a Hive runtime |
| LLM use | Structured outputs, bounded repair, fake provider, append-only `llm_call` audit records | Analysis-agent schemas, prompts and deterministic analysis idempotency |
| Materialization | `origin/main` contains typed physical input requirements, the execution IR, join/cardinality/fan-out refusal, admission gates, canonical hashing **and a working renderer** (`materialize/render/project.py::render_project`, plus spine/projection/calculation nodes and render-time refusal tests) | Verified cluster inventory, general job submission/polling, and live Hadoop publication |
| Source integrations | OpenMetadata/catalog integration | Governed Hive/ODS connection registry and data-plane worker protocol |
| Ontology | A reviewed E3/E5 program exists as a plan | E3 link types and E5 ontology behavior are not implemented facts |
| UI | React application and governed catalog screens | A safe chart renderer and analysis workspace |
| Permissions | Catalog, feature, IAM and governance capabilities; sensitivity is a separate axis | Data-source administration, observation and analysis capabilities |

Before implementation starts, rebase or branch from the commit that actually contains the
materialization package. Do not copy its code out of `origin/main` into an older branch.

## 3. Non-negotiable functional invariants

These are correctness and governance requirements, not later operational polish.

1. Raw or row-level source data stays in the governed data plane. The control plane receives only
   an allowlisted aggregate result or profile.
2. A source is read only through a versioned `DataSourceConnectionV1` and
   `PhysicalDatasetBindingV1`. CSV content cannot create either automatically.
3. Every run pins the metadata revision, semantic revision, policy revision, physical input
   snapshot, compiler version, and generated artifact hash.
4. An LLM never selects an ungoverned table, invents a join, weakens sensitivity, bypasses
   point-in-time rules, or expands the read scope.
5. Unknown population, time semantics, physical layout, join cardinality, fan-out behavior,
   sensitivity, or input snapshot causes a typed refusal. It never defaults to a plausible value.
6. Every result states the exact population, period, comparison period, filters, dimensions,
   measures, exclusions, data cutoff, and whether an estimate or exact computation was used.
7. Small-cell suppression and restricted-data policy are applied before an aggregate crosses the
   data-plane boundary.
8. A partial profile cannot retract ontology evidence. A partial analysis cannot replace the last
   validated result.
9. Charts and narrative can fail independently after the data result is valid. A presentation
   failure must not turn correct result data into a failed data run.
10. A narrative can cite only cells and definitions in the validated result artifact. It cannot
    introduce a new number or silently run another query.

## 4. Target system shape

```text
                    CONTROL PLANE

 metadata upload ──► catalog revision ──► observation request/outbox
                                                │
                                                ▼
                                     governed dispatch protocol
                                                │
   ontology candidates ◄── aggregate profile ◄──┘
          │
          ▼
 governed semantic layer
          │
 NL question ─► typed request ─► grounded analysis plan ─► compiled artifact
                                                           │
                    DATA PLANE                             │
                                                           ▼
 approved Hive/ODS ◄── read-only cluster worker ◄── pull/verify request
          │
          ▼
 bounded Spark/Kedro or source-native execution
          │
          ▼
 validate ─► suppress/restrict ─► signed aggregate manifest
                                      │
                    CONTROL PLANE     ▼
                     result ingest ─► charts + grounded narrative + UI
```

Use a pull model for the first bank deployment: a worker inside the data environment pulls jobs
over mutually authenticated transport, verifies a signed request envelope, its declared scope and
artifact hashes, executes with a read-only principal, and returns a signed aggregate manifest.
Key IDs, algorithms and trust roots are explicit versioned fields; the bank PKI choice is verified
in Milestone 0 rather than invented here. This avoids making the control plane a network bridge
into every source.

## 5. Core contracts

All identity-bearing payloads use canonical JSON and an explicit schema version. Provenance such
as timestamps, live watermarks and job status stays outside identity.

### 5.1 Physical access

`DataSourceConnectionV1`

- stable connection ID and environment ID;
- kind: initially `hive`, later a separately implemented `ods` adapter;
- secret reference, never a password, keytab, token or JDBC URL containing credentials;
- allowed catalogs/schemas and execution principal;
- data residency, network zone and capability version;
- disabled/active state and owner approval.

`PhysicalDatasetBindingV1`

- governed logical table reference;
- connection ID plus source-native catalog/schema/table;
- physical schema fingerprint;
- partition columns and an explicit business-time-to-partition mapping;
- row/column policy references and sensitivity floor;
- allowed analysis purposes;
- owner approval, valid-from revision and supersession link.

`PhysicalInputSnapshotV1`

- binding version;
- exact ordered partition or snapshot identifiers read;
- metastore/schema fingerprint observed at run preparation;
- source cutoff/watermark when one is governed;
- observation time as provenance, not identity.

`CatalogMetadataSnapshotV1`

- catalog source and the ingestion run which triggered capture;
- exact ordered logical objects, relevant effective governed-fact heads and physical binding
  revisions;
- canonical content hash and schema version;
- capture time as provenance.

The existing ingestion `source_fingerprint` is correlation state, not a full catalog revision. It
must not be relabelled as this snapshot. Observation and analysis require the new complete,
content-addressed snapshot over their exact dependency set.

### 5.2 Observation and profile

`ProfilePolicyV1`

- permitted metrics by sensitivity class;
- sampling or exact mode;
- maximum tables, columns, partitions, bytes, rows and runtime;
- permitted approximate algorithms;
- minimum cohort size and suppression policy;
- permitted profile egress fields;
- profile retention and expiry.

`DataObservationRequestV1`

- request ID and idempotency key;
- catalog revision and physical dataset binding version;
- selected columns and relationship probes;
- profile policy hash;
- requested input snapshot selector;
- trigger provenance: ingestion run, manual actor, scheduled refresh or stale-evidence refresh.

`DataObservationManifestV1`

- request and run identities;
- final input snapshot IDs;
- generated artifact, execution and engine hashes;
- complete/partial status and exact coverage;
- aggregate metric schema;
- signed result digest and verification state;
- redacted failure or refusal code.

`DataObservationV1`

- column/table/relationship reference;
- statistic type and typed value;
- exact, approximate or sampled method;
- sample fraction and covered partitions;
- confidence/error information when supported;
- sensitivity and egress classification;
- manifest ID and expiry.

The initial profile vocabulary is closed: row count, null count/rate, approximate distinct count,
numeric min/max/quantiles, bounded categorical top values, string-length distribution,
date range, format/pattern distribution, and approved pairwise overlap probes. Raw example values
are excluded from the control-plane payload in the first release.

### 5.3 Ontology

The ontology stores a stable candidate separately from its revisions:

`OntologyCandidateV1`

- stable candidate ID;
- subject, predicate/link type, proposed object/value;
- candidate class: deterministic observation, deterministic rule, or LLM interpretation;
- immutable evidence dependency IDs;
- confidence and scoring model version;
- authority required for promotion;
- lifecycle: proposed, needs-review, accepted, rejected, superseded, stale.

`OntologyCandidateRevisionV1`

- candidate ID and revision number;
- evidence snapshot and proposal payload;
- producer version and prompt/schema versions when an LLM participated;
- supersession reason.

At minimum, the E3 link-type foundation must support typed identities for:

- physical binding of a logical object;
- governed joinability with direction and cardinality;
- semantic equivalence/synonymy;
- membership in an entity, measure, dimension, domain or concept;
- derivation/provenance;
- candidate contradiction and supersession.

Observed data can strengthen or weaken a candidate; it cannot on its own promote operational facts
such as a join path, availability-time column, entity key, SCD policy, or authorization rule.
Promotion remains governed. Low-risk search synonyms may use a separately declared auto-promotion
policy, with provenance and rollback.

### 5.4 Semantic analysis

The analysis agent requires governed definitions rather than only column labels:

- `EntityDefinitionV1`: entity, keys and eligible source/spine.
- `PopulationDefinitionV1`: who is included, effective-date rules and exclusions.
- `MeasureDefinitionV1`: aggregation, filters, null behavior, unit and physical reads.
- `DimensionDefinitionV1`: values, unknown handling and point-in-time lookup.
- `RelationshipDefinitionV1`: approved join, direction, cardinality and fan-out policy.
- `TimePolicyV1`: event time, availability time, timezone, calendar and closed-period rule.
- `MetricDefinitionV1`: measure plus population, grain, time, comparison and output type.

`AnalysisRequestV1`

- original user text as provenance;
- resolved or unresolved measure, population, dimensions, filters and comparison;
- requested output and presentation;
- actor, purpose and read-scope snapshot.

`AnalysisPlanV1`

- exact semantic definition revisions;
- exact physical read set and join tree;
- point-in-time predicates;
- population spine;
- current and comparison period boundaries;
- filters, aggregations, zero/null behavior and output schema;
- budget estimate and policy decisions.

`AnalysisResultV1`

- plan and execution identities;
- result schema and aggregate rows;
- validation and reconciliation report;
- suppression markers;
- input snapshot and freshness statement;
- source and policy provenance.

`ChartSpecV1`

- closed chart kind: `kpi`, `bar`, `line`, `stacked_bar` or `table`;
- title, axis labels and result-column references;
- sort, series, units and suppression presentation;
- no executable JavaScript, expressions, SQL, HTML or remote asset URLs.

`NarrativeEvidenceV1`

- structured claims;
- each numeric claim references exact result cell IDs;
- each semantic claim references exact definition IDs;
- deterministic verification status.

Two hashes keep plans stable while making executions reproducible:

```text
analysis_plan_hash =
  hash(semantic revisions + physical requirements + periods + joins
       + filters + aggregation + expected output)

analysis_execution_hash =
  hash(analysis_plan_hash + physical input snapshots + policy revision
       + effective authorization scope + execution principal
       + compiler/renderer versions + generated artifact hash)
```

A data timestamp and a source-delivery promise are different things. The first release may report
the exact snapshot/partitions observed and exclude rows whose governed availability time is after
the cutoff. Unless a separate governed delivery SLA exists, it must not claim the source was
complete or “ready by T+N”.

## 6. Durable lifecycle and refusal model

Observation and analysis use separate append-only run tables, but the same proven lifecycle
pattern as ingestion: immutable status events, heartbeats, leases, compare-and-set transitions,
terminal manifests and crash reconciliation.

Analysis lifecycle:

```text
received
  → grounding
  → needs_clarification | grounded
  → planned
  → policy_validated
  → submitted
  → running
  → result_received
  → result_validated
  → published
```

Terminal alternatives are `refused`, `failed`, `cancelled`, `stale_input` and `abandoned`.
Presentation continues as a child lifecycle after `result_validated`; its failure does not change
the data result's terminal truth.

Define a closed refusal vocabulary before implementing routes. It must distinguish at least:

- source connection or dataset binding not governed;
- metadata, semantic or physical snapshot moved;
- physical partition/snapshot unresolved;
- source engine unsupported;
- semantic term, metric, dimension or population unresolved;
- time period ambiguous;
- join path unverified, cardinality unknown or fan-out unsafe;
- point-in-time semantics missing;
- actor read scope insufficient;
- raw-data egress denied;
- query budget exceeded;
- small-cell policy unsatisfied;
- result attestation invalid;
- result schema mismatch;
- result totals unreconciled.

Malformed API declarations remain caller-validation errors, not catalog-governance refusals.

## 7. Delivery plan

Each milestone must be runnable and testable before the next one begins. Database migration
numbers are allocated at implementation time from the then-current branch; this document does not
reserve them.

### Milestone 0 — Verified interfaces and one-source pilot

**Goal:** Replace every live-data assumption with an observed or explicitly declared fact.

**Work**

1. Create a verified-interface record for the pilot environment.
2. Record Hive, Spark, metastore, Hadoop, Java, Python, Kedro and dataset-package versions.
3. Record authentication mechanism, job-submission mechanism, filesystem URI, metastore endpoint,
   network route, service principal and secret-reference mechanism without recording secrets.
4. Select one customer table and one transaction table. Capture `DESCRIBE FORMATTED`, column
   types, partition columns, sample partition names, row-policy behavior and update semantics.
5. Identify the authoritative customer population, transaction event time, availability time,
   reversal/status fields, customer key, segment and sector history semantics.
6. Verify a read-only cluster principal can run a bounded probe and cannot write to source tables.
7. Decide whether ODS data is read directly through a dedicated source adapter or first exposed as
   a governed Hive snapshot. The first vertical slice supports Hive only.

**Artifacts**

- `docs/superpowers/verified-interfaces/data-agent-hdfc-pilot.md`
- a typed environment inventory under `conf/environments/`, containing no secrets;
- redacted probe evidence and capability attestation.

**Gate**

Contract and UI work may proceed without this milestone. No task may claim live execution,
partition correctness, source readiness or performance until it passes.

### Milestone 1 — Contracts, canonical identity and permissions

**Goal:** Establish the vocabulary every later layer must obey.

**Work**

1. Add a new `src/featuregen/data_agent/` package for canonicalization, contracts and refusal
   codes. Do not place PySpark, Hive or Kedro runtime dependencies in the control-plane package.
2. Implement the common version/canonical/hash primitives plus the physical-access,
   metadata-snapshot and observation envelopes needed by Milestones 2–5. Each later milestone owns
   its own ontology, semantic, analysis and presentation contracts so the project stays runnable
   in slices.
3. Add canonical JSON hashes and prove live observations cannot enter stable plan identity.
4. Extend capabilities with separately gated permissions:
   `data_source:manage`, `data_observation:run`, `data_analysis:run`,
   `data_analysis:read`, and ontology/governance confirmation as appropriate.
5. Keep operation permissions independent from sensitivity roles and source row policies.
6. Write an architecture decision describing Hive-first execution and the ODS adapter boundary.

**Tests**

- canonicalization is order-independent where the contract says order is irrelevant;
- read-set and join order are stable and explicit;
- adding a physical input, filter or semantic revision changes the plan hash;
- watermark, timestamp and job status do not change the plan hash;
- snapshots or compiler versions change execution identity;
- every route capability has allow and deny tests;
- secrets cannot be serialized in any contract or API response.

### Milestone 2 — Governed source connections and physical bindings

**Goal:** Turn catalog names into authorized physical data access without trusting the CSV.

**Work**

1. Add immutable connection revisions, dataset binding revisions and owner decisions.
2. Store only a secret-manager reference. Resolve credentials exclusively inside the data-plane
   worker.
3. Build administration APIs and a minimal governance screen for connection state and binding
   approval.
4. Import CSV source fields as **binding candidates**. Exact equality with an existing active
   binding may reuse it; otherwise it remains unresolved.
5. Resolve physical schema from governed catalog evidence and declared mapping. Never parse a
   catalog-side `public` segment as the physical Hive database.
6. Reuse the materialization package's physical-layout and partition-mapping semantics when the
   interfaces are compatible; add adapters instead of a second contradictory model.
7. Revalidate schema and partition fingerprints at every run preparation.

**Tests**

- a CSV row cannot create or activate a connection;
- two same-named tables in different Hive databases cannot collide;
- a missing or changed partition mapping refuses before scan;
- disabled connections cannot dispatch;
- source credentials never reach logs, PostgreSQL JSON, LLM input or browser responses;
- a binding approved for one purpose cannot be reused for a broader purpose.

### Milestone 3 — Durable post-ingestion observation workflow

**Goal:** Schedule profiling after ingestion without making ingestion depend on profiling.

**Work**

1. Add `data_observation_run`, append-only event, request, manifest and terminal-result tables.
2. Add an idempotent reconciler which finds committed `ingestion_run.status = 'ingested'` records,
   captures a complete `CatalogMetadataSnapshotV1`, resolves active bindings, and creates
   observation requests keyed by ingestion run, metadata snapshot, binding revision and policy
   hash. It must not use the existing correlation-only source fingerprint as a revision.
3. Do not add cluster work to the ingestion transaction. Failed or unavailable profiling leaves
   ingestion successful and records an independently retryable observation state.
4. Add manual and scheduled triggers using the same request contract.
5. Add leases, heartbeats, cancellation, bounded retries, backoff and abandoned-run reconciliation.
6. Add a transactional dispatch outbox. State transitions and outbox insertion commit together;
   cluster delivery is at-least-once and worker execution is idempotent by execution hash.
7. When an ingested source has no governed binding or no applicable profile policy, create one
   terminal typed outcome. Do not retry it forever and do not pretend it was profiled.

**Tests**

- repeated reconciliation creates one logical request;
- an observation failure cannot change an ingestion run;
- crash after outbox commit is recoverable;
- duplicate delivery reuses or reports the same terminal manifest;
- lease expiry abandons only a still-running attempt;
- a newer catalog revision does not mutate an older request.

### Milestone 4 — Hive data-observation vertical slice

**Goal:** Produce a real, bounded profile for one approved Hive table.

**Work**

1. Implement a deterministic observation planner from binding, snapshot selector and
   `ProfilePolicyV1`.
2. Render a self-contained Kedro/PySpark project using the proven materialization renderer
   (`materialize/render/`) conventions: pinned runtime, complete catalog, generated lock, exact source declarations and no
   control-plane imports.
3. Apply partition pruning first. Use sampling or approved approximate statistics by default;
   exact full scans require an explicit policy and budget.
4. Split one large table into bounded column batches while sharing its filtered/sampled
   intermediate. Do not issue one full scan per column.
5. Run relationship probes only for ontology-shortlisted key pairs. Never evaluate all column
   pairs across the catalog.
6. Validate types, coverage, metric bounds and manifest completeness inside the data plane.
7. Apply sensitivity inheritance and small-cell suppression before export.
8. Emit a signed `DataObservationManifestV1` and aggregate observations only.

**Tests**

- Spark-local fixture yields hand-computed null, distinct, quantile, range and pattern results;
- partition pruning reads only declared partitions;
- approximate output is labelled and includes method/error metadata;
- a restricted top-value profile cannot leave the data plane;
- small groups are suppressed before serialization;
- unsupported data types return a typed per-column outcome, not a failed whole run;
- partial batch failure reports exact coverage and cannot masquerade as complete.

**Live acceptance after Milestone 5**

Run against the pilot Hive table, prove the actual partitions read, verify the manifest signature,
and show that no row/sample value entered control-plane storage.

### Milestone 5 — Data-plane runner and result-ingestion protocol

**Goal:** Make cluster execution reliable without embedding bank credentials in the API service.

**Work**

1. Define versioned dispatch, acknowledgement, heartbeat, cancellation and result envelopes.
2. Implement a bank-side worker which pulls allowed jobs, verifies request signature, artifact
   hash, environment, engine capability and read scope, and then runs the generated project.
3. Allowlist artifact kinds and compiler versions. Reject arbitrary archives, SQL, UDFs, imports,
   network calls and undeclared datasets.
4. Sign result manifests inside the data environment. Verify signature, execution hash, schema,
   size, sensitivity, suppression and replay state before accepting a result.
5. Store immutable artifacts and manifests; publish a current pointer using compare-and-set only
   after validation.
6. Separate dispatch retry from computation retry. A known terminal execution hash is reused, not
   recomputed.
7. Execute either by source-supported user/group impersonation or by an explicit signed effective
   row-policy scope. A broad service principal must never turn into a bypass of the requesting
   actor's governed source access. Include that effective scope in execution identity and result
   reuse.
8. Run Milestone 4's deferred live-profile acceptance through this protocol.

**Tests**

- forged, replayed, expired or wrong-environment envelopes are rejected;
- artifact and manifest hash mismatches are rejected;
- lost acknowledgements do not create a second computation;
- cancellation is monotonic and cannot cancel a different attempt;
- result ingestion accepts only aggregate schemas allowlisted by the request;
- previous validated data remains current when a new run fails.

### Milestone 6 — Profile store and ontology evidence producer

**Goal:** Use real data observations to improve discovery and semantic candidates safely.

**Work**

1. Store immutable observation versions and a separate current pointer.
2. Track expiry, input coverage, method and policy so old or partial evidence cannot appear fresh.
3. Implement deterministic evidence derivations first:
   likely identifier, enum-like category, continuous measure, date range, identifier overlap,
   format compatibility and candidate uniqueness.
4. Add contradiction rules. For example, a key candidate is weakened when a complete profile
   disproves uniqueness; a partial sample may flag risk but cannot prove non-authority globally
   unless a duplicate was actually observed.
5. Feed only metadata plus policy-approved aggregate observations into the existing structured LLM
   seam. Register task schema, prompt version and input hash in the existing `llm_call` audit path.
6. Make the LLM propose typed ontology candidate revisions with evidence IDs. It does not write
   accepted facts.
7. Land the minimum E3 typed-link registry required by section 5.3 if it is not already present on
   the implementation branch. Reuse the reviewed E3 design and compatibility tests; do not create
   untyped predicate strings in this package.
8. Project candidate revisions and their evidence through those link types. Full E5 search,
   traversal and ranking may arrive from its own program, but this milestone must deliver a real
   governed evidence-to-candidate lifecycle rather than a temporary unintegrated candidate table.
9. Add a governance queue showing evidence, observation completeness, contradiction and impact.

**Tests**

- the same observation snapshot and producer version create the same candidate revision;
- new evidence creates a revision, not a new logical candidate;
- partial or expired evidence cannot retract an accepted fact;
- LLM output without existing evidence IDs is rejected;
- restricted aggregate evidence is not sent to the LLM unless its egress policy allows it;
- synonym auto-promotion, if enabled, cannot promote joins, keys, PIT, sensitivity or access facts.

### Milestone 7 — Governed semantic layer for the first analysis

**Goal:** Define the business meaning required to answer one question correctly.

**Work**

1. Implement immutable revisions for entity, population, measure, dimension, relationship, time
   and metric definitions.
2. Add proposal and confirmation APIs with explicit owners and authority.
3. Create the pilot definitions:
   customer entity and key; eligible-customer population; posted, non-reversed transaction count;
   segment and sector dimensions; customer-to-transaction relationship; availability and
   event-time rules; Dubai or bank-approved business timezone; completed-calendar-month policy.
4. Make dimension history explicit: static or SCD/as-of. No current-value join is permitted when
   the requested report is historical and the dimension is temporal.
5. Add a semantic dependency index from each definition to exact columns, tables, joins and
   policies.
6. Use ontology evidence to suggest definitions, but require governance for this first metric.

**Tests**

- every accepted metric closes over a complete physical read set;
- a missing population, PIT rule or join cardinality refuses;
- a restricted operand raises the result classification;
- an override may restrict further but never loosen derived classification;
- changing a definition revision invalidates new plans without mutating old runs.

### Milestone 8 — Natural-language request and deterministic grounding

**Goal:** Turn a question into a reviewable request without executing data access.

**Work**

1. Add a structured LLM task which extracts candidate measure, population, periods, comparison,
   dimensions, filters and desired presentation.
2. Retrieve only a bounded semantic neighborhood relevant to the user's terms and permitted scope.
   Do not put the entire catalog or ontology into the prompt.
3. Resolve candidates deterministically against governed semantic revisions and synonyms.
4. Represent ambiguity explicitly. “Last month” defaults only when a governed user/business
   timezone and completed-period policy exist; “last 30 days” is a different request.
5. Return a clarification prompt when multiple governed meanings remain. Do not select by highest
   LLM confidence alone.
6. Persist original text, structured LLM output, prompt/schema versions, input hash, candidates,
   resolution decisions and actor scope.
7. Treat “customers whose transactions decreased last month” without a comparison baseline as
   ambiguous. Ask whether the user means the preceding month, a trailing-window baseline or
   another comparison.

**Tests**

- fake-LLM fixtures cover direct resolution, synonym resolution, unresolved terms and ambiguity;
- prompt injection in the user's question cannot change tools, read scope or system policy;
- hidden metrics and dimensions never appear as candidates;
- the same text plus the same semantic snapshot is idempotent;
- a changed semantic snapshot forces re-grounding.

### Milestone 9 — Analysis planner, authority gates and cost gate

**Goal:** Produce one typed plan which can be audited before execution.

**Work**

1. Build the entity-by-period population spine before transaction aggregation.
2. Resolve exact current and comparison period bounds using the governed calendar and timezone.
3. Resolve approved joins, direction, cardinality, PIT predicates and physical reads.
4. Represent zero-observation entities correctly: left join each period aggregate to the
   population spine and coalesce missing transaction counts to zero.
5. Add authority gates for operation permission, sensitivity/read scope, purpose, source policy,
   row policy, small-cell policy and LLM egress.
6. Add an explain/preflight cost gate using partitions, catalog statistics and configured budgets.
   Unknown cost may be refused or routed for explicit approval; it cannot silently become a full
   scan. Approval may cross a soft budget only. A policy-defined hard byte, partition, runtime,
   join and result-cell ceiling is not overrideable by a user.
7. Freeze `AnalysisPlanV1` and expected result schema before rendering.

**Tests**

- reversed or non-posted transactions are excluded;
- current-zero customers remain in the population;
- a customer is decreased only when `current_count < previous_count`;
- segment/sector are joined as of the declared report instant;
- a many-to-many join without allocation policy refuses;
- an added source not present in the authorization read set refuses;
- over-budget and unknown-partition plans refuse before submission.

### Milestone 10 — Deterministic analysis compiler and execution

**Goal:** Run the first governed analysis on Spark/Hive.

**Work**

1. Render the typed plan into a complete Kedro/PySpark project. The generated project is the only
   execution path; do not create a second interpreter whose semantics may drift.
2. Reuse common PIT-filtered projections and source scans independently from result
   materialization.
3. Emit per-stage manifests: input snapshot, population, current aggregate, comparison aggregate,
   joined result and final grouped result.
4. Run structural validation before submission and Spark-local fixture validation before cluster
   release.
5. Execute through the Milestone 5 worker protocol.
6. Per-customer counts and dimension assignments may exist only in access-controlled,
   retention-bounded data-plane staging because they are necessary to compute the answer. They
   never cross into the control plane. Export only the final suppressed aggregate; publish its
   immutable content-addressed result and move the current pointer atomically. Leave the last valid
   result visible on failure.

**Tests**

- generated project parses, imports and constructs its real Kedro pipeline;
- every declared source is read and no undeclared source can be read;
- generated and expected output schemas match exactly;
- data types and decimal policies are deterministic;
- a moved metadata or physical snapshot fails before publish;
- retrying the same execution hash reuses its terminal result.

### Milestone 11 — Result validation, charts and grounded narrative

**Goal:** Present useful EDA without allowing presentation code or prose to change the result.

**Work**

1. Validate schema, unique grouping keys, null policy, totals, suppression and reconciliation:
   sector/segment subtotals must reconcile to allowed totals.
2. Create stable cell IDs over the validated result.
3. Generate deterministic `ChartSpecV1` proposals from result shape. Let the LLM propose titles or
   chart choices only from the closed chart vocabulary.
4. Implement a safe first-party SVG renderer for KPI, bar, line, stacked bar and table. A later
   chart-library choice requires a separate compatibility/security decision; do not add executable
   chart specifications.
5. Ask the LLM for structured narrative claims using only semantic definitions and validated
   aggregate cells.
6. Reject ungrounded numeric claims and render the data result without narrative when verification
   fails.
7. Label suppressed, sampled, approximate, stale and incomplete values visibly.
8. Apply an explicit LLM-egress policy to the aggregate cells. If the configured model endpoint is
   not permitted to receive their classification, omit narrative generation or use an approved
   in-boundary provider; never weaken classification merely because rows were aggregated.

**Tests**

- every chart references real result columns;
- line charts require an ordered time field;
- suppressed values cannot leak through labels, totals, tooltips or narrative;
- each numeric sentence maps to an equal typed result cell;
- chart or LLM failure still exposes the validated table and run provenance;
- no chart field is interpreted as JavaScript, SQL or HTML.
- stable cell IDs derive from result identity plus canonical group key and column, never row
  position.

### Milestone 12 — Analysis workspace and APIs

**Goal:** Give users one traceable workflow from question to evidence.

**Work**

1. Add APIs to submit a question, inspect grounding, answer clarification, approve an expensive
   plan when authorized, inspect run status, cancel and retrieve validated results.
2. Add an analysis screen containing:
   question, interpreted meaning, population/period/comparison, sources and freshness, execution
   state, KPI cards, charts, result table, narrative, caveats and provenance.
3. Make ambiguity and refusal visible. Never replace “unknown” with an empty chart.
4. Add links from each measure/dimension/source to its catalog or ontology detail.
5. Enforce result access again on every read; a shareable run ID is not authorization.
6. Add audit views for operators without exposing row-level data or secrets.

**Tests**

- API and UI capability gates;
- direct URL access cannot bypass sensitivity;
- refresh/reconnect does not resubmit a running analysis;
- cancellation and terminal status display are monotonic;
- keyboard and screen-reader behavior for charts includes an equivalent table;
- desktop and mobile layouts do not hide caveats or suppression labels.

### Milestone 13 — End-to-end hardening and live acceptance

**Goal:** Prove correctness, resilience and scale on the real execution path.

**Work**

1. Run the full fixture through metadata ingestion, observation, candidate generation, governed
   semantics, question grounding, cluster execution, result validation and UI presentation.
2. Inject failures at dispatch, worker start, Spark stage, result upload, manifest ingest, chart
   generation and narrative generation.
3. Exercise duplicate messages, stale snapshots, schema drift, partition rewrite, expired leases,
   worker version drift and key rotation.
4. Add quotas by **source and user**, workload pools, queue backpressure and per-source concurrency.
   **Not by tenant:** v1 is single-tenant because tenant is absent from every current identity
   (catalog, fact, candidate, cursor and projection keys are deployment-global). Adding tenant as an
   operational label while identities ignore it creates a quota that cannot be enforced.
5. Cache profiles by binding revision, snapshot and policy hash. Cache analysis executions by
   execution hash. Never reuse across differing scope, snapshot or policy.
6. Measure partition count, bytes scanned, Spark stages, runtime, result size, queue delay and
   suppression counts without logging raw values.
7. Roll out in shadow mode, then to an allowlisted user group, then expand by source and semantic
   domain.

**Release gate**

The program is complete only when the worked example below is hand-reconciled against a live,
approved cluster result; all input snapshots and semantic revisions are visible; raw rows did not
cross the data-plane boundary; duplicate/retry tests prove one logical publication; and a failed
replacement run leaves the previous result intact.

## 8. First end-to-end business question

User question:

> How many customers had a decrease in transaction count in the last completed month compared
> with the previous completed month? Break it down by customer segment and sector.

The platform must display its interpretation before running:

- **Population:** the governed eligible-customer population at the report cutoff.
- **Current period:** last completed calendar month in the governed business timezone.
- **Comparison:** the immediately preceding completed calendar month.
- **Measure:** count of governed posted, non-reversed transactions.
- **Decrease:** `current_transaction_count < previous_transaction_count`.
- **Dimensions:** segment and sector as of the governed report instant.
- **Zero handling:** a customer with prior transactions and no current transactions has current
  count zero and is included.

Logical execution:

```text
eligible customers at cutoff
  LEFT JOIN prior-month transaction counts
  LEFT JOIN current-month transaction counts
  coalesce missing counts to zero
  filter current_count < prior_count
  point-in-time join segment and sector
  aggregate customer count and decline statistics by segment and sector
```

Minimum output:

- total customers with a decrease;
- eligible population and percentage with a decrease;
- customer count by segment;
- customer count by segment and sector;
- average and median absolute decrease;
- current and previous period boundaries;
- cutoff/freshness, suppression and approximation labels.

Minimum presentation:

- KPI cards for decreased customers and population percentage;
- bar chart by segment;
- stacked bar by segment and sector;
- comparison table with counts and decline magnitude;
- line chart only when the user additionally requests a multi-period trend;
- narrative whose numbers link to result cells.

The acceptance fixture must include:

- a customer with prior transactions and zero current transactions;
- a customer with an unchanged count;
- a customer with an increase;
- a reversed or unposted transaction;
- a late-arriving row tested against availability time;
- a segment or sector change requiring point-in-time treatment;
- a many-to-many join candidate which must be refused;
- a small cohort which must be suppressed.

Expected outputs are hand-calculated in the test, not generated from the implementation under
test.

## 9. Scale strategy

The first release is deliberately narrow, but its contracts support scale:

- profile by changed dataset revision rather than page view;
- profile one physical snapshot once and reuse it across ontology consumers;
- prune partitions and columns before Spark starts;
- share scans and sampled intermediates across profile metrics;
- use approximate sketches under explicit policy and label them;
- shortlist relationship probes using metadata/ontology before reading data;
- retrieve a bounded semantic neighborhood for the LLM;
- enforce maximum sources, joins, columns, partitions, bytes, runtime and result cells;
- isolate workloads by source and sensitivity;
- precompute governed daily/monthly aggregates only after query history proves reuse;
- keep computation grouping, result materialization and UI assembly as independent decisions.

Do not use unconstrained transitive “join neighbourhoods” for grounding. Start with verified direct
joins, apply a deterministic table/count budget, and report truncation visibly.

## 10. Observability and operating objectives

The following become release gates by Milestone 13:

- every run has a correlation ID and append-only event history;
- every dispatched job has a known owner, lease and terminal state;
- every result can be traced to exact code, policy, semantic and data snapshots;
- no secret, raw value or prohibited aggregate appears in logs or control-plane payloads;
- queue depth, age, retry, refusal, failure, scan-size and runtime metrics exist;
- alerting distinguishes source unavailable, policy refusal, budget refusal, worker failure,
  invalid result and presentation failure;
- retention deletion removes payloads through a governed lifecycle while retaining allowed audit
  metadata;
- disaster recovery can rebuild current pointers from immutable manifests.

Numeric service-level targets must be set after the pilot measures the real cluster. This plan does
not invent latency, throughput or freshness promises before that evidence exists.

## 11. Build order and dependency graph

```text
M0 verified cluster/source facts ───────────────┐
                                                ▼
M1 contracts/permissions → M2 bindings → M3 durable observation → M4 Hive profiling
                                                       │                 │
                                                       └──────► M5 runner/result protocol
                                                                          │
                                           M4 live acceptance ◄────────────┘
                                                                          │
                                               E3 link types (in M6) ──────┤
                                                                          ▼
                                                      M6 ontology evidence/candidates
                                                                          │
                                                                          ▼
                                                         M7 semantic definitions
                                                                          │
                                      M8 NL grounding → M9 typed plan ─────┘
                                                            │
                                                            ▼
                                                     M10 execution
                                                            │
                                                            ▼
                                             M11 presentation → M12 UI
                                                            │
                                                            ▼
                                                     M13 live release
```

M1, the non-live parts of M3, M4 renderer/Spark-local work, M8 contract work and M11 chart-contract
work may proceed while M0 is being completed. M4's live acceptance, M5 and the live half of M10
cannot.

## 12. Explicitly deferred

These do not block the first complete vertical slice:

- direct support for every ODS/database engine;
- autonomous promotion of operational ontology facts;
- arbitrary user-authored formulas or SQL;
- raw-row export, customer drill-down or row-level LLM analysis;
- unconstrained multi-hop joins;
- forecasting, causal claims or automated business recommendations;
- generalized dashboard authoring;
- continuous streaming analysis;
- full automatic source-delivery SLA derivation;
- choice of a third-party charting library;
- performance promises unsupported by the pilot.

They must be added through new versioned contracts and adapters, not by loosening the first
release's gates.

## 13. Definition of done

This program is done when:

1. Metadata ingestion reliably schedules an independent observation request.
2. A governed worker profiles approved Hive data and returns only verified aggregate evidence.
3. Data observations create traceable ontology candidate revisions and cannot silently become
   authoritative operational facts.
4. The pilot's population, measure, dimensions, joins and time rules are governed and versioned.
5. The natural-language question becomes a reviewable typed plan or an honest clarification or
   refusal.
6. The deterministic generated Kedro/Spark project runs on the approved Hadoop environment.
7. The result is hand-reconciled, point-in-time correct, fan-out safe, suppressed where required
   and traceable to exact input snapshots.
8. The UI renders a validated table, safe charts and a cell-grounded narrative.
9. Retry, crash, stale-input and presentation-failure tests leave no partial publication and no
   loss of the previous valid result.
10. Access, sensitivity and data-egress tests prove that natural language never broadens what the
    actor or platform is allowed to read.
