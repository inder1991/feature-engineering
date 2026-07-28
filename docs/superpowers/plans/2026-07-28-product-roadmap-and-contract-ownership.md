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
the NL agent, typed plans, generated Kedro/PySpark, Hadoop execution, tables and charts, grounded
summaries, and the ontology learning loop. What moves later is the standalone ontology *exploration
product* — the semantic-map service, the ER screen and the navigable graph — because none of them
answers a question or produces a feature.

What is **permanently removed** is duplicate machinery, not functionality: several candidate stores
for one proposal, three LLM replay stores, two metadata-snapshot models, two physical-identity
models, and a second execution interpreter alongside generated Kedro code.

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
| Physical object | E3 `PhysicalObjectIdentityV1`; agent `PhysicalDatasetBindingV1` | **Both, layered.** The physical object identifies *what exists*; the binding authorizes *how a worker reads it* and references the object id. Neither is deleted; the reference is made explicit. |
| Metadata revision | E3 `OntologyMetadataRevisionV1`; agent `CatalogMetadataSnapshotV1` | **One dependency snapshot** carrying a per-source revision vector, physical binding revisions, fact heads and registry fingerprints. |
| Candidate lifecycle | `entity_suggestion` (0967), `entity_bridge_candidate_evidence` (0989), `semantic_binding_candidate` (1014), E3 `CandidateIdentityV1`, agent `OntologyCandidateV1` | **One** candidate identity/revision/currentness substrate with typed families. The agent's generic ontology candidate contracts are removed in favour of it. |
| Relationship | `approved_join`, `entity_bridge`, E3 link types, agent `RelationshipDefinitionV1` | The **governed relationship fact is authoritative**; semantic definitions reference its fact key and revision. |
| LLM replay | E0b replay store, E3 selection store, agent `llm_call` extension | **One content-addressed structured-result store**, shared by every LLM task. |
| Execution IR | `materialize/` IR; agent `AnalysisPlanV1` | The analysis plan **compiles into the shared execution IR** for reads, joins, PIT, spine, types and policy. No second interpreter. |
| Permissions | catalog roles, E3 source entitlement, agent connection permissions | **One effective authorization decision** combining operation, source, sensitivity, purpose and row policy. |

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

**Automatic attestation** is what stops this becoming "a human approves 100,000 columns". It
requires deterministic evidence: compatible physical types, target uniqueness, referential coverage,
observed duplicate multiplier and cardinality, identifier-format compatibility, no conflicting
governed facts, and bounded drift monitoring. Ambiguous or high-impact cases go to humans. Note this
tier needs **real data**, so it arrives with Release 1, not before.

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
by `attest/grounding.py`'s **`path_agreement`** check. Nothing needs to persist them; anything
needing taxonomy corroboration extends that check.

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

## 6. Release sequence

### Release 0 — one trustworthy baseline

Create the integration branch and merge the selected fixes, the materialization package and any
retained analysis contracts. Re-run migrations and tests, re-ingest the fixture, regenerate the
verified-interfaces reference against **one commit**. Commit the agent plan (still untracked). Freeze
the §4 link policy.

### Release 1 — prove real data access

Cluster inventory. Bind one customer and one transaction table with explicitly configured connection
and manually declared mappings — **not** the full connection-governance system. Write the renderer.
Run a bounded generated Kedro/Spark profile manually on Hadoop. Prove partitions, read-only access
and aggregate-only return. Load a second catalog source before claiming any cross-catalog success.

Do not build the scheduler/worker platform before this proof.

### Release 2 — minimum ontology core

Schema-preserving physical identity; corrected cardinality semantics; the single evidence/candidate
substrate; concepts, synonyms, domains, entity/object roles; identifier relationships with
deterministic data evidence; E0's concept facet and a governance queue.

### Release 3 — first governed analysis

The pilot semantic definitions and the exact question above. Must demonstrate the population spine,
zero-transaction customers, PIT dimensions, verified joins, reversal/status filtering and
hand-reconciled results.

### Release 4 — natural language and presentation

Structured intent extraction, bounded semantic retrieval, clarification, typed plan preview,
deterministic compilation, validated result table, safe charts, grounded narrative with cell
citations.

### Release 5 — the learning loop

Every unresolved or corrected question produces an ontology-improvement candidate: unknown term,
missing synonym, unclear measure, missing relationship, uncertain key, ambiguous period, missing
dimension. This is what makes the ontology grow from real demand rather than as an independent
modelling project.

### Release 6 — operationalize, then expand

Durable observation workflow, pull worker, leases/retries/outbox, immutable manifests, artifact
dependency invalidation, caches, quotas. Then use query logs and unresolved questions to decide
whether to fund the E5 semantic-map service, the ER view, the ontology graph, composite bridges or
ODS adapters.

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

Keep placeholders in contracts where a later field would otherwise force a breaking change. Do not
implement the machinery.

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

**Two global rules**, both already proven in `attest/`:

1. **Evidence-ID binding** — an LLM proposal that does not reference existing evidence ids is
   rejected. This is the single strongest governance idea in any of these plans; apply it to every
   LLM task, not just ontology candidates.
2. **Blind second opinion** — never show the model the proposed answer
   (`attest/reclassify.ColumnContext`). A critic shown the answer agrees with it.

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
