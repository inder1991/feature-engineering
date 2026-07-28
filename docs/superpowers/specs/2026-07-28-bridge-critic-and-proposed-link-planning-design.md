# Bridge Critic & Proposed-Link Planning — Design

Date: 2026-07-28 · Status: design, not yet planned · Verified against `origin/main@ef213a83`
plus the on-branch fix `e6e5f31d`.

**Every interface below was opened and read before being written down.** The recurring failure in
this project's specs has been describing an API from memory; three separate defects this week —
`declared_type` never reaching the bridge matcher, BIAN/FIBO never being persisted, and confirm-time
CAS not existing — were all "the capability is there" claims that dissolved on contact with the
code. Anything here without a file citation is a decision, not a fact.

---

## 1. Outcome

Two changes, taken as directives:

1. **Cross-catalog links are created by the system, shown as `proposed`, and used by the feature
   planner and feature generation regardless of whether a human has confirmed them.** Human
   confirmation changes status; it is not an admission gate.
2. **A link only becomes `proposed` if it survives strong evidence — deterministic corroboration
   plus an independent LLM critic that can say no.**

The net effect: the AI proposes identity links freely and features flow from them immediately, but
nothing reaches the planner that a critic rejected, and every feature standing on an unconfirmed
link says so.

---

## 2. Verified baseline — what exists today

### 2.1 The link substrate

| Fact | Citation |
| --- | --- |
| A bridge links ONE column to ONE column | `overlay/identity.py` `EntityBridgeRef` |
| Cross-catalog ONLY — same-source refused in code and by DB `CHECK` | `identity.py:131-133`, `bridge_candidates.py:98`, `0989_entity_bridge_governance.sql:21` |
| Candidate evidence table already exists | `0989_entity_bridge_governance.sql:6-22` |
| Proposal writes a PROPOSED fact + candidate evidence row | `bridge_propose.py:28-71` |
| **Projection drops anything not VERIFIED** | `bridge_projection.py:36` |
| **`active_bridges` filters `WHERE status = 'VERIFIED'`** | `bridge_projection.py:62` |
| The planner already consumes bridges | `planner/plan.py:19,76`; 12 call sites across `assembly`, `multisource_*`, `declarations`, `fingerprint` |
| Bridge `fact_key`s are hashed into **plan identity** | `plan.py:76`, `declarations.py:826`, `fingerprint.py:75` |
| No HTTP route exists to propose/confirm/reject a bridge | `api/routes/governance.py:155-442` covers joins, table-facts, semantic-bindings only |
| Confirmation is single platform-admin; `owner_of` returns `None` everywhere | `overlay/authority.py:132-145`, `upload_catalog.py:65-66,101-102` |

### 2.2 Candidate derivation

Matching is: concept group `identifier` + identical `entity_link` + compatible type family +
distinct source (`bridge_candidates.py:85-102`). Enumeration is an **unbounded** nested loop
(`bridge_candidates.py:129-130`).

`e6e5f31d` (this branch) added the `declared_type` fallback and `BridgeCandidateV1.type_basis`
(`attested|declared|mixed`). Measured on the deployed FTR catalog: eligible identifier columns went
**0 → 28**.

### 2.3 Evidence actually available to a critic

Verified by querying the deployed catalog (126 columns):

| Field | Persisted? | Coverage |
| --- | --- | --- |
| `definition` (business definition) | yes, `graph_node` | 126/126 |
| `semantic_terms` (synonyms) | yes | 126/126 |
| `domain` | yes | 126/126 |
| `declared_type` | yes | 126/126 |
| `concept` → `entity_link` | yes (registry-derived) | 120/126 |
| `schema_name`, `table_name`, `column_name` | yes | 126/126 |
| **`bian_path`, `fibo_path`** | **NO** | file has 114/127 |
| **`term_type`, `process_path`, `related_terms`** | **NO** | file has 127/127 |
| `entity` (free-text tag) | yes but empty | 0/126 |

**The BIAN/FIBO finding is material.** The FTR file carries banking-standard taxonomy alignment —
`CIF_ID` is BIAN `Customer Management / Customer Profile`, FIBO `Business Entities` — and it is the
single strongest semantic signal for deciding whether two identifier columns denote the same
namespace. It is read (`glossary_reader.py:73-74`), used once to help classify the concept
(`enrich.py:503`), and then **discarded**. No table stores it; `information_schema` has no column
matching `%bian%` or `%fibo%`, and no evidence row contains it.

**Value-shape evidence is unavailable for FTR.** The profiler computes `distinct_count` and
`uniqueness_ratio` (`overlay/profiler_metrics.py`) but samples real data. FTR is a glossary upload
with no dataset behind it. So the critic will judge **meaning**, not values — it can catch a
semantic mismatch, it cannot catch two different customer numbering schemes that both mean
"customer". State this plainly wherever a confidence is displayed.

### 2.4 The pattern to reuse — `attest/`

`overlay/upload/attest/` already implements exactly this shape for concepts and **must not be
reinvented**:

- `grounding.py::ground_concept` → `GroundingV1(checks, coverage, conflict)` — deterministic
  corroboration.
- `reclassify.py::ColumnContext` — the **blind** input: name/definition/sample-shape only, and
  *never the proposer's prior answer*. Blindness is the whole point; a critic shown the proposed
  answer agrees with it.
- `reclassify.py::ReclassifyV1` — one independent opinion; `value=None` on failure or
  off-vocabulary output.
- `fusion.py::fuse(...)` → `FusionV1(confidence, agreement)` — pure, no DB/LLM, and a hard
  deterministic conflict **caps** confidence regardless of agreement (`_CONFLICT_CAP`).
- `shadow_store.py`, `runner.py`, `report.py` — shadow observation and measurement.

### 2.5 The gauntlet

`REQUIREMENT_CODES` is a **closed** vocabulary (`feature_assist.py:110-114`): `TYPE_IS_NUMERIC`,
`GRAIN_IS_UNIQUE`, `TEMPORAL_IS_POPULATED`, `TEMPORAL_LAG_BOUNDED`, `JOIN_CONNECTIVITY`,
`UNIT_CONSISTENT`, `CURRENCY_CONSISTENT`, `ADDITIVITY_SUPPORTS_OPERATION`. `Requirement` is an
immutable value object validated against the versioned schema registry
(`validation_requirements.py`). `VALIDATION_STATES` is the tri-state
`DESIGN_CHECKED | NEEDS_EXTERNAL_VALIDATION | REJECTED`.

### 2.6 The LLM seam

`intake/llm.py::drive_structured_call(client, request, validate_output, *, repair_budget,
retry_budget) -> StructuredCallOutcome` — provider-agnostic, fail-closed, bounded repair/retry.
`DEFAULT_LLM_MODEL = "claude-sonnet-5"` (`llm.py:88`).

**No durable result reuse exists.** `llm_dispatch` persists `redacted_input` and no response
(`1005_llm_dispatch_provenance.sql:19-34`); its key is per-attempt by design; `find_llm_call`
(`intake/llm.py:409-453`) has zero production callers and is `run_id`-scoped. This is a
prerequisite, not an assumption — see §7.

---

## 3. Decisions taken

**D1 — Proposed links reach the planner.** `active_bridges` widens from VERIFIED-only to
`VERIFIED | PROPOSED`, each row carrying its status.

**D2 — The critic gates admission, not features.** A critic `no` prevents the link from becoming
`proposed` at all, so it never reaches the planner. It remains visible with its reason and is
overridable by a human. Rationale: this keeps the AI strictly *narrowing* what features may join
while leaving D1 intact — the filter sits before the planner, not as a gate after it.

**D3 — Features on unconfirmed links are created and flagged, never blocked.** A new requirement
code `JOIN_IDENTITY_UNCONFIRMED` puts the feature in `NEEDS_EXTERNAL_VALIDATION`, naming the link it
rests on. Human confirmation clears it → `DESIGN_CHECKED`. This is the E4a unit loop applied to
identity.

**D4 — Materialization runs on unconfirmed links too. No gate anywhere.** *(User decision,
2026-07-28, after the alternative was raised and declined.)* Proposal, grounding, display, gauntlet
evaluation **and materialization** all run on proposed links. There is no point in the pipeline at
which an unconfirmed identity link stops the work.

What replaces the gate is **provenance that cannot be separated from the output**. The codebase
already holds this exact pattern: `materialize/classify.py:40` — "the requirement travels with the
artifact" — for access requirements. `JOIN_IDENTITY_UNCONFIRMED` gets the same treatment:

- it is serialized onto the governed contract through the existing
  `contract/_serial.py::requirements_to_json`, so it survives snapshotting and reload;
- it names the specific link `fact_key`, so "which link is this standing on" is always answerable;
- it travels onto the materialized artifact alongside `access_requirements`, so a consumer of the
  numbers can see the basis without going back to the catalog;
- it clears on confirmation and demotes on rejection, updating existing artifacts' status rather
  than silently leaving stale claims behind.

The residual risk is stated plainly here so it is never a surprise: a wrong link produces numbers
that look entirely reasonable — two customers' payments aggregated into one row — and the evidence
available for glossary catalogs is semantic, not value-level (§2.3). The critic panel (D6) and the
deterministic grounding are what keep the false-match rate down; the flag is what makes a mistake
findable afterwards.

**D5 — The critic's verdict is evidence, not authority.** Recorded with its reasons, replayable,
and overridable by a human in both directions, with the override audited.

**D6 — A panel of two critics with different lenses; unanimity to admit.** *(User decision.)*

Two identical critics cost double and add nothing — redundancy does not catch what a single lens
misses, so the two are deliberately **different**:

| Lens | Question it is asked |
| --- | --- |
| **Meaning** | Do these two columns denote the same *kind* of identifier? Definitions, concept, domain, taxonomy. |
| **Population** | Do they identify the same *set of real-world things*? A bank customer and a cardholder are both "customers" and are not the same population; a group-level CIF and a branch-scoped customer number are not interchangeable. |

Decision rule, with only two voices and therefore no majority available:

- **both `same_namespace` → admit** as `proposed`;
- **any `different_namespace` or `insufficient_evidence` → suppress**, retained with reasons;
- **the two disagree → suppress AND surface as a distinct `critics_disagree` state.**

Disagreement is the most informative outcome the panel produces and must not be flattened into an
ordinary rejection: it marks the pairs where human judgement has the highest value, and it is the
natural top of the review queue.

---

## 4. Architecture

```
identifier columns (read-scoped)
        │
        ▼
[1] deterministic pre-checks ──reject──▶ never leaves; no LLM call spent
        │  survive
        ▼
[2] ground_bridge()  → BridgeGroundingV1(checks, coverage, conflict)
        │
        ▼
[3] blind LLM critic → BridgeCriticV1(verdict, reasons, driving_evidence)
        │
        ▼
[4] fuse_bridge()    → FusionV1(confidence, agreement)     [pure]
        │
        ├─ conflict OR critic=no  ──▶ suppressed, retained as audit evidence
        └─ otherwise              ──▶ propose_bridge() → PROPOSED fact
                                            │
                                            ▼
                                     active_bridges (VERIFIED | PROPOSED)
                                            │
                                            ▼
                                   planner → feature + JOIN_IDENTITY_UNCONFIRMED
```

### [1] Deterministic pre-checks — no model call

Reject before spending anything: incompatible type family, different `entity_link`, same
`catalog_source`, missing concept, endpoint not readable under the caller's scope, endpoint does not
exist. These already exist in `derive_bridge_candidates`; this step formalises them as a named,
testable stage that records *why* a pair was dropped.

### [2] `ground_bridge` — deterministic corroboration

Modelled directly on `grounding.ground_concept`, returning the same `(checks, coverage, conflict)`
shape so `fuse` can be reused. Checks, each `agree | disagree | absent`:

| Check | Basis | Notes |
| --- | --- | --- |
| `type_family` | `declared_type`/`data_type` | already computed; `type_basis` records attested vs declared |
| `entity_link` | concept registry | must be identical (a pre-check, restated as evidence) |
| `domain_compatible` | `graph_node.domain` | `Compliance` vs `Compliance` corroborates; disjoint domains disagree |
| `name_tokens` | column/table names | reuse `grounding._name_tokens` |
| `definition_overlap` | `graph_node.definition` | token overlap of business definitions |
| `synonym_overlap` | `semantic_terms` | 126/126 populated |
| `grain_role` | `is_grain` + grain fact | one side being a table's key is corroboration; **neither** side being a key is not a conflict (a FK↔FK link is legitimate) |
| `uniqueness` | profiler `uniqueness_ratio` | `absent` when unprofiled — the FTR case |
| `taxonomy_alignment` | BIAN/FIBO | **`absent` until §6 lands** |

`conflict = true` on a hard contradiction — e.g. disjoint declared type families, or contradictory
BIAN level-1 paths once available. Per `fuse`'s existing contract, a conflict caps confidence no
matter what the critic says.

### [3] The LLM critic — blind and adversarial

The critic is the judgement half of the evidence and is **not optional**: deterministic checks alone
cannot tell that "Customer Information File Identifier" and "Cardholder Reference Number" describe
different populations.

**Blindness.** Following `ColumnContext`'s rule, the critic is *not* told that the system proposed
this pair, nor any confidence. It is given two column descriptions and asked whether they denote the
same identifier namespace.

**Adversarial framing.** It is asked for *the strongest reason these are NOT the same namespace*,
with `insufficient_evidence` an explicit, expected, unpenalised answer. A critic asked "is this
correct?" agrees.

**Envelope** (bounded, sanitized, metadata only — no sample values, no raw logical refs):

```
for each side:
  column_name, table_name, schema_name
  term/business definition   (bounded, sample-value clauses stripped)
  concept, entity_link, domain
  declared_type, type_basis
  synonyms
  [bian_path, fibo_path, term_type  — once §6 lands]
```

**Response** — closed vocabulary, schema-validated via `drive_structured_call`:

```
verdict: "same_namespace" | "different_namespace" | "insufficient_evidence"
reasons: [bounded strings]
driving_evidence: [field names it actually used]
```

`different_namespace` and `insufficient_evidence` both suppress. Anything off-vocabulary is a
failure, and a failed critic call suppresses (fail-closed) rather than defaulting to admit.

**Both panel members (D6) receive the same envelope and the same response schema**; only the
question differs. Each is dispatched independently — neither sees the other's verdict — so the two
opinions stay genuinely uncorrelated. If one call fails while the other returns, that is not
unanimity: the pair suppresses. `BridgeCriticPanelV1` carries both verdicts, both reason sets, and
the derived `panel_outcome` (`admit | suppress | critics_disagree`).

### [4] `fuse_bridge`

Reuse `fusion.fuse`'s structure and constants. `agreement` records
`critic_verdict`, `grounding_coverage`, `grounding_conflict`, plus `type_basis` so a reviewer can
see the match rested on a declared rather than attested type.

Confidence is **display and review-routing only**. It may order a queue. It may never feed
`operational_eligibility` — the influence ceiling (`field_policies.py:58-65`,
`field_authority.py:296-297`) is the hard guarantee and confidence must not become a way around it.

---

## 5. Threading proposed links through the planner

1. `bridge_projection` admits `PROPOSED` alongside `VERIFIED`; `entity_bridge_edge.status` already
   exists. Demotion still deletes.
2. `ActiveBridgeV1` gains `status`. `active_bridges` returns both, ordered as now.
3. All 12 consumers reviewed; most simply carry `status` through.
4. **Plan identity.** Bridge `fact_key`s are hashed into the plan fingerprint. The `fact_key` goes
   in as today; **`status` stays out of every hash**, so a link flipping `PROPOSED → VERIFIED` does
   not invalidate existing plans. This is the trap operand roles hit, where the fix was keeping the
   new field off anything hashed — same treatment.
5. `JOIN_IDENTITY_UNCONFIRMED` added to `REQUIREMENT_CODES` and the versioned
   `validation_requirements` registry, with the operand naming the link's `fact_key`.
6. Confirming the link clears the requirement on every feature resting on it; rejecting demotes
   them.

---

## 6. Persist the taxonomy evidence — optional, never required

Add `bian_path`, `fibo_path`, `term_type`, `process_path`, `related_terms` to durable storage so the
critic and grounding can read them after ingest. Currently they are read, used once at ingest, and
discarded (§2.3).

Preferred shape: a schema-preserving glossary-record sidecar table keyed by `logical_ref`, rather
than more columns on the public-flattened `graph_node`. The value reaches beyond this spec — it is
also the evidence the E3/E5 ontology programme would need.

**Optional by design** *(user decision)*. Most catalogs will not carry BIAN/FIBO — your customer
catalog may well not — and a file without them must be a first-class citizen, not a degraded one.
Concretely:

- absence is reported by `ground_bridge` as **`absent`**, never `disagree`. An `absent` check cannot
  contribute to `conflict` and therefore cannot suppress a link;
- absence lowers `coverage`, which is exactly what `fusion.fuse` already does with it — the LLM
  agreement signal is weighted *less* when there is less deterministic corroboration, rather than
  the pair being penalised. This is existing behaviour (`_AGREE_COVERAGE_WEIGHT`), not new logic;
- the critic envelope simply omits the fields when they are missing. It is never told "taxonomy
  unavailable" in a way that invites it to treat absence as suspicious;
- a mixed pair — one side with taxonomy, one without — is normal and scores as `absent`, not as a
  mismatch;
- **an acceptance test asserts that a pair with no taxonomy on either side reaches the same
  admission outcome as the same pair with matching taxonomy**, differing only in confidence. That
  test is what stops the optional field quietly becoming mandatory.

Where the taxonomy *is* present on both sides, a contradiction at BIAN level 1 (e.g.
`Customer Management` vs `Payment Execution`) is a genuine `disagree` and may set `conflict`.

---

## 7. Prerequisite: deterministic replay

The same pair with the same metadata must yield the same verdict rather than re-rolling the model.
That store does not exist (§2.6). Required before the critic gates anything:

- a content-addressed selection key over `(candidate_id, both endpoints' metadata fingerprint,
  prompt/schema/model versions)` — not a minted id, not `run_id`-scoped;
- the validated response body and its hash, persisted;
- a reuse probe on the dispatch path.

Without it, re-deriving candidates produces different admissions on identical inputs, and no
shadow measurement means anything.

---

## 8. Measure before trusting

The critic runs in **shadow** first, via the existing `attest/` runner and `shadow_store`, recording
verdicts without suppressing anything. Two numbers decide whether it ships:

- **agreement with human decisions** on the confirm/reject record;
- **rejection rate.** A critic that never says no is theatre — it adds cost and false assurance. If
  it approves everything in shadow, it does not gate.

Publish both before enabling D2. ~40 provisional gold labels exist in the demo database from the P0
work; they are a starting point, not a sufficient sample.

---

## 9. Bounded cost

One critic call per pair that survives the pre-checks, so enumeration must be bounded first
(`bridge_candidates.py:129-130` is an unbounded nested loop). Bound the identifier roster per
entity/source, order deterministically, page, and carry a `pairs_examined` budget — recording
honestly when it stopped early. Unbounded pairing plus a model call each is the expensive failure
mode.

---

## 10. Scope boundaries

- Metadata only. No row reads, no value joins, no customer matching. The critic never sees sample
  values; `_value_shape`'s shape-hint pattern is the only permitted derivation and only where the
  profiler has run.
- The critic cannot invent an endpoint, propose a different pair, or alter a concept.
- It cannot change any authority tier; the influence ceiling is untouched.
- Nothing here creates a bridge governance route — that gap (§2.1) remains, and D3's
  "confirmation clears the flag" is unreachable through the product until it is built. **This spec
  depends on that route.**

---

## 11. Decisions — resolved 2026-07-28

All three open questions are settled by the user:

1. **Materialization runs on unconfirmed links.** No gate at any stage; provenance travels with the
   artifact instead (D4).
2. **Panel of two, different lenses, unanimity to admit**, with `critics_disagree` surfaced as its
   own state (D6).
3. **Persist BIAN/FIBO, but strictly optional** — `absent`, never `disagree`; pinned by an
   acceptance test that a taxonomy-free pair admits identically (§6).

### Remaining dependency, not a decision

D3's "confirmation clears the flag" is **unreachable through the product** until an
identifier-bridge governance route exists (§2.1 — `api/routes/governance.py` covers joins, table
facts and semantic bindings only, and every bridge that exists anywhere came from a fixture or a
script). Under D4 this matters more, not less: links now flow all the way to materialized numbers,
so the ability to confirm — and, more importantly, to **reject** a wrong one and have that demote
the artifacts resting on it — is the only correction mechanism in the loop.

**The bridge governance route is therefore a hard prerequisite of this spec, not a follow-up.**

---

## Deferred NFRs

Caching and incremental re-derivation; bulk review UI; confidence calibration and model-quality
dashboards; critic latency SLOs and cost controls; multi-hop component analysis over proposed links.
