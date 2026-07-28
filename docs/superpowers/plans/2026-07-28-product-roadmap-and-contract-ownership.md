# Product Roadmap and Contract Ownership — Ontology + Data Agent

Date: 2026-07-28 · Status: the master sequencing document. Supersedes the build orders inside the
individual programmes.

**This document exists because four plans described one product.** They defined overlapping candidate
stores, snapshot models, relationship contracts, LLM replay stores and link policies, and they
contradicted each other on the one question that decides whether a published number can be wrong.
Rather than reconcile them pairwise — which already drifted once within a day — this document owns
the parts they share, and each plan cites it instead of restating it.

## 1. The ruling

**The first governed business question is the product-driving vertical slice.** Build only the
ontology capabilities that this question, catalog discovery and feature generation consume. The
larger E5 ontology UI is an optional later product, not a prerequisite.

> *Customers whose transaction count decreased in the last completed month versus the previous
> month, by segment and sector.*

Do not implement the four plans independently as written.

**Scope is being re-sequenced, not cut.** Every user-facing capability stays in the programme:
metadata ingestion, Hive profiling, profiles improving the ontology, concepts/domains/synonyms,
LLM-powered proposals, entity and identifier detection, relationship discovery, governance evidence,
the NL agent, typed plans, real execution against Hive, tables and charts, grounded
summaries, and the ontology learning loop. What moves later is the standalone ontology *exploration
product* — the semantic-map service, the ER screen and the navigable graph — because none of them
answers a question or produces a feature.

**"Deferred" and "optional" are different words and this document means one of them.** E5 exploration
**remains in the product roadmap**; it is simply not committed to the first functional programme,
and its implementation order and funding are decided after data-agent and E0 usage evidence. It has
not been deleted.

What is **permanently removed** is duplicate machinery, not functionality: several competing
candidate *contracts* for one proposal, three LLM *reuse* stores, and a second execution
*interpreter* alongside generated Kedro code. Note what is NOT removed — physical identity and its
runtime binding are layered, and a metadata revision and a dependency snapshot are built one from
the other (§3).

## 2. Document disposition

| Document | Ruling |
| --- | --- |
| `2026-07-28-verified-interfaces-cross-catalog.md` | Useful, **not yet a reproducible baseline**. Regenerate against one integration branch in Release 0. |
| `2026-07-28-e0-semantic-map-v0.md` | **Keep.** Small, visible, useful. |
| `2026-07-28-e0b-identifier-link-admission.md` | **Rewrite the admission rule** (§4) and drop the demotion promise (§5). Otherwise keep. |
| `2026-07-27-e3-e5-cross-catalog-ontology-program.md` | **Architectural reference.** Split "minimum ontology core" from the optional E5 screens. |
| `2026-07-28-data-powered-ontology-analysis-agent-program.md` | **The master product roadmap**, after removing the duplicate contracts in §3 and correcting §5. |

## 3. Contract ownership — one owner per concern

Where two plans define the same thing, this table decides. The losing definition is deleted, not
kept "for reference".

| Concern | Competing definitions | Owner |
| --- | --- | --- |
| Physical identity | E3 `PhysicalObjectIdentityV1`; agent `PhysicalDatasetBindingV1` | **One identity, one binding — layered, not duplicated.** `PhysicalObjectIdentityV1` says *what exists*; `PhysicalDatasetBindingV1` is a runtime-access binding that **references** it. Once the binding is correctly named they are not two identity models. |
| Metadata state | E3 `OntologyMetadataRevisionV1`; agent `CatalogMetadataSnapshotV1` | **Both — a snapshot is built FROM revisions.** Keep the per-source metadata revision primitive; build one immutable dependency snapshot capturing a vector of those revisions plus fact heads, registry and binding revisions. They are not duplicates. |
| Candidate lifecycle | `entity_suggestion` (0967), `entity_bridge_candidate_evidence` (0989), `semantic_binding_candidate` (1014), E3 `CandidateIdentityV1`, agent `OntologyCandidateV1` | **One public contract and API now; one physical table later.** Freeze `CandidateIdentityV1` plus a typed family vocabulary, put new families through it, and adapt the existing tables behind it. Physically migrating WORM tables, currentness semantics, fact links and backfill is Release 6 work and would consume a release without improving the agent. The hard rule is **no fifth ad-hoc lifecycle**. |
| Relationship | `approved_join`, `entity_bridge`, E3 link types, agent `RelationshipDefinitionV1` | **Keep the three typed meanings distinct** — physical join, identifier equivalence, semantic relationship — stored through one governed fact framework. The governed fact is authoritative; semantic definitions reference its fact key and revision. |
| LLM record vs reuse | E0b replay store, E3 selection store, agent `llm_call` | **Complementary, not competing.** `llm_call` stays the attempt/audit record; ONE new content-addressed validated-result store handles reuse. Build the reuse store once, shared by every LLM task. |
| Execution IR | `FormulaExecutionIRV1`; agent `AnalysisPlanV1` | **Two top-level IRs sharing lower-level primitives** — see §3a. `AnalysisPlanV1 → AnalysisExecutionIRV1`, distinct from `TypedFormulaV1 → FormulaExecutionIRV1`. Still no second *interpreter*: both render through the same Kedro shell. |
| Permissions | catalog roles, E3 source entitlement, agent connection permissions | **Define one decision interface now**, defer the complete policy machinery per the security deferral. |

## 3a. Execution IR — two, sharing primitives

**`FormulaExecutionIRV1` cannot be the analysis IR.** Verified on `origin/main`
(`materialize/ir.py:106-128`): it carries `feature_name`, ONE `final_operation`, ONE
`grain_entity`/`grain_keys`, aggregate `expressions`, ONE `spine`, and a feature-group
`output_policy`. `FinalOperation` is `identity | ratio | difference` (`formula/schema.py:49-54`) —
so `current_count < previous_count` is not expressible as a final operation at all.

The pilot question needs current and comparison periods, per-entity current and previous counts, a
comparison predicate, PIT dimension joins, post-comparison filtering, multiple result measures, and
a **result table** rather than a feature-group table. Forcing that into the feature IR would either
deform the analysis semantics or turn the feature IR into a general query language.

```text
TypedFormulaV1   → FormulaExecutionIRV1     (features)
AnalysisPlanV1   → AnalysisExecutionIRV1    (analysis)
```

**Shared primitives** (reuse, do not fork): `PhysicalInputRequirement`, `PhysicalInputSnapshot`,
`JoinPlan`, `PitSpec`, `SpineSpec`, physical types and decimal rules, refusal codes where the
semantics match, canonical hashing, the generated Kedro project **shell**, and the
projection/scan-sharing patterns.

**Renderer**: reuse the project shell, not the whole feature renderer. The existing renderer's
layers, dataset names, required parameters and publication target are built around a feature group;
profiling and analysis need their own DAG and dataset renderers over the common shell.

## 3b. Execution mode — what makes the link tiers enforceable

§4's tiers are policy prose until something carries the tier. Widening `active_bridges` globally to
include `PROPOSED` would make all twelve consumers responsible for remembering which tier they are,
and one missed check puts a proposed relationship into production.

```text
ExecutionMode = discovery | sandbox | production

RelationshipUseDecision
  relationship_fact_key
  execution_mode
  relationship_status
  decision_basis
  fact_event_or_revision
  automatic_attestation_policy_version?
  evidence_snapshot?
```

Resolution takes the mode and returns the decision. Discovery may receive proposed relationships;
sandbox may, and must target a sandbox namespace; **production returns only verified or currently
policy-attested relationships**. Materialization consumes the decision, never raw `active_bridges`.

Prefer two explicit readers — `advisory_bridges(...)` and `operational_bridges(...)` — over
redefining what "active" means for everyone.

### The hash rule, corrected

E0b said relationship status stays out of **every** hash. That is right for the stable identity and
wrong for authorization:

```text
stable plan hash          : relationship fact key
execution/authorization   : fact key + exact fact event/revision + effective status
                            + execution mode + attestation policy/evidence revision
```

Without the second, a sandbox run on a proposed link caches a result under an execution hash, the
link is later verified or rejected, and the same execution identity is reused under a different
eligibility state. Confirmation must not rename the logical relationship; it must change whether a
particular execution is authorized.

## 3c. Execution — one plan, pluggable executors

**Kedro is packaging, not connectivity.** An earlier revision of this roadmap routed all data access
through a generated Kedro/Spark project, which made it read as mandatory. It is not. The three
concerns are separate:

```text
Hive / ODS   = where the data lives
Spark / SQL  = what performs the calculation
Kedro        = how calculation steps, config and inputs are organised
```

A Kedro node ultimately uses Spark SQL or a connector anyway. For a handful of `COUNT`, `MIN` and
`MAX` aggregates, generating a whole project is more machinery than the job needs.

**The rule: one typed plan, more than one executor, one result shape.**

```text
DataObservationPlan / AnalysisExecutionIRV1
      │
      ├── direct Hive SQL executor      ← fastest functional proof; Release 1
      ├── ODS native SQL executor       ← later, once the engine is known
      └── Spark/Kedro executor          ← scale, multi-stage, and production materialization
```

Every executor returns the identical typed result — table identity, input snapshot/partitions, row
count, column statistics, relationship statistics, **exact-vs-approximate method**, coverage,
failures. **The ontology must not be able to tell which executor produced its evidence.** That is
what keeps the executor a swappable detail instead of a fork in the architecture.

This applies to **analysis as well as observation**. The pilot question is expressible as SQL — two
period CTEs, a left join to the population spine, group by segment and sector — so the sandbox slice
runs it directly. `AnalysisExecutionIRV1` stays the artifact of record and *compiles to* either
backend. One plan with two backends, never SQL in one place and Kedro in another.

**Where the generated project genuinely earns its place:** hundreds of columns and shared scans;
multi-stage pipelines with intermediates; retries and partial-failure semantics; sealed
reproducibility; and production materialization, where feature generation already lives. Switch when
one of those is true, not by default.

### What the existing profiler already gives us

`ProfilerLimits` carries a **server-owned schema allowlist**, statement timeout, sampling threshold
and column cap; and `profiler_command.py` enforces the property that matters — *a caller's
self-attested `allowed_schemas` must not authorize a scan*, failing closed when no config is sealed.
That is the hard part of safe profiling and it is built.

**But the query layer is PostgreSQL-shaped** (`TABLESAMPLE BERNOULLI`, `sql.Identifier`,
`SET LOCAL statement_timeout`). So Release 1 reuses the **policy and limits** and adds a **dialect
layer**; it does not simply point the existing profiler at Hive.

### Two rules that hold regardless of executor

**Partition pruning is correctness and cost, not a Kedro feature.** `SELECT COUNT(*) FROM
banking.transactions` with no predicate is a full scan of a partitioned bank table. Pruning belongs
in the observation plan, or the first profiling run is the one the DBA remembers.

**Approximate by default, and record which.** Exact distinct counts are expensive on Hive and worse
on an operational ODS. The result carries the method because it decides what the evidence can later
support: **sampled uniqueness cannot prove uniqueness** — finding a duplicate disproves it, finding
none proves nothing.

## 4. Link policy — three tiers

This replaces the contradiction between the plans, where one admitted proposed links to
materialization and two acceptance criteria in another said a proposed link is never operational.

| Tier | Proposed (unconfirmed) links | Rationale |
| --- | --- | --- |
| **Discovery / feature suggestion** | **Allowed.** | Nothing is published; a wrong suggestion costs a reviewer's time. |
| **Sandbox analysis** | **Allowed, if visibly marked and isolated.** Never written to production feature or model-input tables. | Exploration is the point, and the marking is on the result, not a footnote. |
| **Production analysis / materialization** | **Requires a VERIFIED link, or policy-based automatic attestation.** | A `JOIN_IDENTITY_UNCONFIRMED` warning does not make an incorrect customer join safe. Users trust the headline number, not the provenance note. |

`materialize/joins.py` already implements the production tier correctly: three distinct governed
outcomes (`UNVERIFIED` / `DENIED` / `NO_PATH`), refusal on unknown cardinality, and per-hop fan-out
refusal. That is the policy; nothing should weaken it.

**Automatic attestation** is what stops this becoming "a human approves 100,000 columns" — but it
is currently a phrase, not a contract. It needs an owner, evidence-completeness rules, thresholds, a
version, an expiry and a drift response before it can gate anything. Staged:

- **Release 1 produces relationship EVIDENCE only, and promotes nothing**: left/right uniqueness,
  null rates, exact or bounded overlap, unmatched-key rate, join row multiplier, observed
  cardinality, format compatibility, plus the input snapshot and method.
- **Release 2 implements policy attestation in SHADOW.** A policy-attested relationship carries at
  minimum `relationship_fact_key`, `policy_id + policy_version`, `evidence_ids`,
  `input_snapshot_ids`, `decision ∈ {operational, insufficient, conflict}`, and
  `valid_until`/revalidation condition.

**Sampling does not prove uniqueness.** A sampled profile that *finds* a duplicate disproves
uniqueness; a sample that finds none proves nothing. Operational attestation requires exact
evidence, a physical constraint, or an explicit governed approximation policy.

For the first vertical slice, **human verification is acceptable** — attestation is measured in
shadow and does not block the agent.

## 5. Corrections to the plans as written

**The Kedro/PySpark renderer EXISTS — an earlier revision of this document said it did not.**
`origin/main` carries `materialize/render/project.py::render_project` with spine, projection and
calculation node renderers and render-time refusal tests. The agent plan's baseline claim was
correct; my contradiction of it was not.

The error is instructive enough to keep: I verified against `fix/join-neighbourhood-cap`, which is
**17 commits behind** `origin/main`, and every one of those 17 commits is renderer work. Checking
the wrong baseline produced a confident, wrong finding — which is exactly the failure mode §1 of the
release plan exists to remove, and the sharpest possible argument for doing Release 0 first.

**BIAN/FIBO are persisted — an earlier claim of mine was wrong.** They live in `field_evidence`
(migration 0983), 114 rows each on the deployment, written at `ingest.py:1044` and already consumed
by `attest/grounding.py`'s **`path_agreement`** check. Nothing needs to persist them.

**But `path_agreement` is a concept-name token heuristic, not BIAN/FIBO hierarchy alignment** — its
own docstring says `concepts.py` has no canonical path field. Reuse its evidence-LOADING pattern;
implement direct normalized path comparison for identifier-link corroboration.

**E0b must not promise artifact demotion.** Rejecting a link and deleting the projection row does
not correct published outputs. That needs artifact-to-fact lineage, a publication pointer, a
transactional invalidation, a rule for whether the old artifact becomes unavailable or merely
invalid, and cache/model-input invalidation. None of it exists. Until it does, the plan may not
claim rejection corrects published numbers — which is a further reason production materialization
requires a verified link (§4).

**Tenant is out of v1.** E3 correctly declares v1 single-tenant because tenant is absent from
current identities. The agent plan's M13 adds "quotas by tenant/source/user". Quotas are by
**source and user**; tenant does not appear as an operational label while identities remain
deployment-global.

**Two critics is not the default.** Risk-tier it: a panel for high-blast-radius or ambiguous pairs,
a single critic otherwise. A panel on every column pair is cost without signal — but removing the
panel entirely is wrong too, since a single confident model on an identity claim is where the damage
is largest.

## 5a. Superseded sections — DO NOT EXECUTE

A precedence sentence in a header is too easy for an implementer to skim past. These named sections
are **superseded**. An implementation agent must not build them as written.

| Document | Section / task | Ruling |
| --- | --- | --- |
| Data-agent programme | §5.3 `OntologyCandidateV1`, `OntologyCandidateRevisionV1` | **Superseded** by the single candidate contract (§3). Do not create a parallel candidate family. |
| Data-agent programme | §5.1 `CatalogMetadataSnapshotV1` | **Retained, but layered** — built from per-source metadata revisions, not instead of them. |
| Data-agent programme | M1-M13 build order | **Superseded** by §6. The milestone CONTENT stands; the order does not. |
| Data-agent programme | M3 durable scheduler/outbox, M5 pull worker | **Deferred to Release 6.** Not before the functional slice. |
| Data-agent programme | M4 "render a self-contained Kedro/PySpark project" for PROFILING | **Superseded** by §3c. Release 1 profiles over a direct Hive executor; the Kedro executor is Release 6. M4's profiling SEMANTICS (partition pruning first, bounded column batches sharing one filtered intermediate, shortlisted relationship probes, sensitivity inheritance, aggregate-only export) all stand — they are plan properties, not executor properties. |
| Data-agent programme | M9 "Freeze `AnalysisPlanV1`" | Stands, plus `AnalysisExecutionIRV1` (§3a). Its population spine and zero-observation handling are the reference behaviour. |
| E3/E5 programme | Non-negotiable boundary "…feeds feature generation **and materialization**, confirmed or not" | **Superseded** by the three tiers (§4). Corrected in place. |
| E3/E5 programme | Build order incl. source entitlement, signed cursors, full Foundation gates before E3.1 | **Superseded** by §6. Entitlement and cursors are deferred security. |
| E3/E5 programme | E5.5, E5.6, E5.7 (semantic map, ER, graph screens) | **Not committed** to the first programme. Roadmap items pending usage evidence. |
| E3/E5 programme | Title bindings, `StaleObjectStubV1`, general entity-link types | **Deferred** — no named consumer. |
| E0b | Task 6 "widen `active_bridges` globally to `VERIFIED \| PROPOSED`" | **Superseded** by §3b: separate `advisory_bridges` / `operational_bridges` readers plus `RelationshipUseDecision`. |
| E0b | Global constraint "`status` stays out of every hash" | **Superseded** by §3b's split: out of the stable plan hash, IN the execution/authorization hash. |
| E0b | Task 7 "reaches the materialized artifact" | **Corrected**: reaches a SANDBOX artifact and feature suggestions; production materialization refuses before artifact creation. |
| E0b | Task ordering 5 → 8 | **Superseded**: shadow-measure the critic and publish M8/M9 BEFORE admission gates on it. Task 5 must not suppress candidates until the gate is enabled. |
| E0 | — | No supersessions. Build as written. |

## 6. Release sequence

### Release 0 — one trustworthy baseline

**DONE.** `integration/ontology-data-agent` branched from `origin/main@c1582753`, with the three code
fixes and all plan documents merged in clean. 5,690 tests pass; the one failure is the planner
neutrality guard, which fires because the read-scope fix touches `planner/scope.py` (open decision).
The read-scope fix is **in this branch** — an already-complete divergence left "until convenient"
would defeat the point of having one baseline.

**Migrations, reingest and regeneration are DONE.** All 110 migrations (including 1032) apply from
scratch to an empty database; the real operator export re-ingests on this branch; and the reference
is regenerated against one commit by `tests/featuregen/overlay/upload/test_release0_baseline.py`,
which anyone can re-run with `FTR_CSV=... pytest ...`. Every code-derived number reproduces the
deployment exactly — 126 columns, 127 schema-attested, declared_type 113/7/6, field_evidence
127/127/127/114/114, 0 attested types, 1 source, 0 bridges.

The concept-dependent numbers (M1, M5, M6) are properties of an ENRICHED catalog, not of the code,
so they cannot be re-derived without an LLM provider and stay labelled as deployment observations.

Remaining in Release 0: freeze the §4 link policy and the §3b execution mode, and resolve the
planner neutrality guard (the one failing test).

> **Releases 1-5 are SANDBOX / NON-PRODUCTION.** Security is deferred by decision, so nothing in
> them is production-approved. Real sensitive data stays inside the bank-controlled Hadoop
> environment; results are marked sandbox and never written to production feature or model-input
> tables.

### Release 1 — look at real data, by the shortest honest path

**Goal:** the first time the system sees actual data. Bounded, read-only, aggregates only.

**0. Answer the question everything rests on.** Can the platform reach HiveServer2, with a
read-only account, under the bank's rules? Confirm the account genuinely cannot write. If the answer
is no, the pull-worker becomes mandatory and this release changes shape — so establish it *before*
writing an executor, not after.

**1. Give the two pilot tables an unambiguous address.** Minimal schema-preserving physical identity
— source, catalog/database, schema, table, column, object kind, normalized identity, and
unknown-schema refusal. This moved up from Release 2 because a profile attached to the wrong object
poisons everything built on it. The full CSV/OpenMetadata ingest migration stays in Release 2; the
*pilot bindings* cannot wait.

Map one customer table and one transaction table by hand. No connection-governance system yet.

**2. Add a Hive dialect layer over the existing profiler policy.** Reuse `ProfilerLimits` — the
server-owned schema allowlist, statement timeout, sampling threshold, column cap — and the
`profiler_command` rule that a caller cannot self-authorize a scan. Write the Hive query dialect;
the current one is PostgreSQL-shaped.

**3. Build the direct Hive executor** behind the `DataObservationPlan` contract, returning the
shared typed result. **Partition pruning is part of the plan, not an optimisation.** Approximate
distinct counts by default, with the method recorded.

**4. Validate against a small local fixture first.** Hand-computed null rates, distinct counts,
ranges and patterns on rows you wrote yourself. This is what makes the first cluster run a
confirmation rather than an experiment.

**5. Run bounded profiles on the two pilot tables.** Prove the partitions actually read, that the
account is read-only, and that only aggregates cross the boundary — never rows.

**6. Store the profiles as typed evidence** — with input snapshot, coverage and method.

**7. Produce relationship evidence and promote NOTHING.** Left/right uniqueness, null rates, overlap,
unmatched-key rate, join row multiplier, observed cardinality, format compatibility. Automatic
attestation is Release 2, in shadow.

**8. Load a second catalog source** before claiming any cross-catalog result. M7 is still 1.

**Not in this release:** the Kedro/Spark executor, the scheduler, the pull worker, the outbox, and
ODS. ODS waits until the engine and access method are known — and when it comes, the choice is
bounded native SQL, a read replica, or an approved Hive snapshot, decided on workload impact rather
than in advance.

**Buildable today without any cluster:** steps 1, 2, 3 and 4. Only steps 5-8 need access.

### Release 2 — minimum ontology core

One candidate API with adapters over the existing tables (§3). Ontology evidence production.
Concepts, synonyms, domains, entity/object roles. Identifier relationship candidates with
deterministic data evidence. Automatic attestation **in shadow**. E0's concept facet and a
governance queue.

### Release 3 — first governed analysis, end to end

Governed pilot semantic definitions. A **manually constructed** typed analysis request — no natural
language yet. `AnalysisExecutionIRV1` and the **deterministic compiler** (moved up from Release 4:
Release 3 cannot produce a result without compilation).

**Compile to direct Hive SQL for this slice** (§3c) — the pilot question is two period CTEs, a left
join to the population spine and a group-by, so the direct executor is the shortest honest proof. The
typed plan stays the artifact of record and can compile to the generated project later without the
ontology or the audit trail noticing.

A real run against Hive, and a validated tabular result.

Must demonstrate the population spine, zero-transaction customers, PIT dimensions, verified joins,
reversal/status filtering and hand-reconciled results.

**Start recording learning evidence here**, not in Release 5: unresolved semantic term, missing
relationship, ambiguous period, missing dimension, and every failed or refused plan reason.
Otherwise the first working question's evidence is discarded.

### Release 4 — natural language and presentation

Structured intent extraction, bounded semantic retrieval, clarification, typed plan preview, safe
charts, grounded narrative with cell citations, and the analysis workspace. The compiler is already
in place from Release 3; this release adds the LLM and the UI.

### Release 5 — the learning loop, automated

The recording from Release 3 becomes an automated question-to-ontology feedback loop, plus
additional metrics, dimensions and questions. This is what makes the ontology grow from real demand
rather than as an independent modelling project.

### Release 6 — operationalize, then expand

The **Spark/Kedro executor** (§3c) — for shared scans across hundreds of columns, multi-stage
profiling with intermediates, partial-failure semantics, and sealed reproducibility for production
materialization, which is where feature generation already lives. Plus the durable observation
workflow, pull worker, leases/retries/outbox, immutable manifests, artifact dependency invalidation,
caches, quotas, security hardening, and **physical** candidate-store consolidation. Then use query logs and unresolved questions to decide whether to fund the E5
exploration product, composite bridges or ODS adapters.

## 7. Functional correctness — keep now, do not defer

These are not NFRs. Without them the feature runs and returns a believable wrong answer:

- population spine, including zero-transaction customers;
- calendar month versus trailing 30 days;
- transaction reversal/status filtering;
- point-in-time dimension handling;
- join cardinality and fan-out detection;
- currency and unit handling;
- result reconciliation;
- the generated code being the actual execution path.

## 8. Deferred — security, resilience, scale

Per the standing steer, these come after the functional slice works end to end:

source-entitlement administration; mTLS and signed worker envelopes; secret-manager integration
beyond local configuration; tenant support; durable queues, leases and outbox delivery; disaster
recovery; automatic artifact demotion; production-scale caching; retention automation; full audit
hardening; comprehensive small-cell and privacy enforcement; E5 ER and graph visualizers; signed
ontology cursors; direct ODS adapters; multiple LLM critics on every candidate.

Do **not** add speculative placeholder fields. Use explicit schema versions and add a later contract
version when the need is real — a field with no current semantics is a guess that future code will
read as a promise.

**One already-built exception.** The governed read-scope fix (`9766c415`, migration 1032) is
committed and green. Deferring security means it **no longer gates Release 0** — not that it is
un-built. Merge it whenever convenient. It is worth keeping because it is the difference between an
LLM prompt containing a national ID number or not, and it costs nothing now that it exists.

## 9. Where LLMs add the most value

**Ontology workflow.** Business definitions, synonyms and domain candidates; BIAN/FIBO alignment
(the evidence is already stored); table/object role and measure/dimension suggestions; identifier-link
semantic criticism; conflict explanations; review-queue summaries; ontology-gap discovery from failed
questions; and prioritizing candidates by how many user questions they unblock.

**Data agent.** Structured intent extraction; semantic candidate ranking; ambiguity detection and
clarification wording; adversarial plan review; chart selection from a closed vocabulary; narrative
generation from validated cells; grounded follow-up suggestions.

**Retrieval.** Combine lexical search, embeddings and a bounded verified graph neighbourhood. Never
put the whole catalog in a prompt.

**Never LLM-controlled:** SQL; physical source selection; authorization; join approval; cardinality
or fan-out; PIT semantics; population; suppression; cost limits; numerical validation.

**Grounding is per-task, not one global rule.** An earlier revision called evidence-ID binding
"already proven in `attest/`". It is not: `GroundingV1` returns `(checks, coverage, conflict)` and
the blind reclassifier returns a bare concept value — neither returns or requires evidence ids.
Evidence-ID binding is a good **new** requirement. What each task must ground against differs:

| Task | Required grounding |
| --- | --- |
| Ontology proposal | evidence ids |
| Semantic selection | definition / candidate ids |
| Intent extraction | exact spans from the user's question |
| Plan critic | plan-node ids and typed findings |
| Chart proposal | result-column ids |
| Narrative | result-cell and semantic-definition ids |
| Follow-up suggestion | existing metric / dimension ids |

**Blindness is critic-specific.** `attest/reclassify.ColumnContext` is genuinely proven and must be
kept for critic and second-opinion tasks — never show the model the proposed answer. It is not a
requirement for intent parsing or narrative generation, where the input *is* the thing being read.

**Embeddings are not mandatory.** On a 126-column catalog, start with lexical search, synonyms,
concepts, domains, bounded direct relationships and deterministic ranking. Add embeddings only when
a retrieval gold set shows material recall improvement — otherwise they bring an index, revision
semantics and a model dependency before demonstrating value.

**Risk-tiered cascade.** A smaller model handles routine extraction; a stronger model is justified
for ambiguous grounding, high-impact link criticism and narrative quality. A second critic is used
selectively, not on every candidate.

## 10. The feedback loop this is all for

```text
user question
  → semantic resolution
  → missing/ambiguous ontology facts
  → targeted profile or governance candidate
  → accepted evidence/fact
  → future questions become more answerable
```

The ontology should be the governed semantic and evidence system that makes the agent — and feature
generation — reliable. Built from governed metadata, bounded observations of real data, and evidence
generated by actual user questions. That is worth more than a complete ontology explorer, and far
less likely to produce an elegant architecture nobody consumes.
