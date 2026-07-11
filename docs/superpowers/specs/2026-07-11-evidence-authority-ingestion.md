# Evidence-Authority Ingestion — Design Spec (v2)

Date: 2026-07-11. Status: DESIGNED, revised after architectural review (verdict: conditionally approve
after revision; v2 resolves all 12 load-bearing items). Supersedes the scope of
`2026-07-11-batched-enrichment.md` — that batching work (merged to main `6345296`) is **Pass A**.

## The one-sentence architecture (unchanged — the review affirmed it)

> Multiple evidence producers propose graph attributes; a **field-specific authority policy** decides
> what may become operational. The graph projection exposes the strongest currently-accepted value; the
> event/evidence log retains every proposal, conflict, promotion, and supersession.

Permanent invariants: **provenance ≠ authority; confidence ≠ permission.**

## What v2 changed (the review's 12 load-bearing corrections)

v1 modelled authority as a flat enum ranked by `min()`, treated `llm_call` as the whole evidence store,
put the semantic ingestion phase before the policy/resolver kernel it depends on, and overclaimed that
the ingestion core is untouched. v2 fixes each:

1. Authority is a **field-specific (producer × status) rule model**, not a flat enum ranking (§3).
2. **Source trust is per (source, field)**, not a global `SOURCE_DECLARED` (§3.3).
3. Field decisions reuse the event framework via **typed field-evidence + field-decision records +
   specialized fact keys** — not an untyped JSON blob (§5).
4. `graph_field_evidence` + item-level `llm_call` linkage are **first-class and load-bearing** (§5).
5. A **Phase 0 authority kernel** ships before any semantic ingestion (§9).
6. **Relationship proposals removed from Phase 1** (Pass C is Phase 3) (§9).
7. Readiness is **blockers/requirements first, percentages second** (§7).
8. **Field-specific conflict lattices** replace a generic `max()` (§6.2).
9. **Evidence staleness** via snapshot + input-hash + producer-version (§6.3).
10. **Logical-vs-provider object identity** defined up front (§2).
11. A **producer × mutation matrix** fixes what each producer may change (§4.1).
12. The **"core untouched" claim is corrected** to an accurate insertion invariant (§10).

---

## 1. Why (unchanged, condensed)

Real-world uploads are **business glossaries** (BIAN/FIBO term maps like the FTR
`FTR_Column_Mapping.csv`), not technical schema+facts catalogs. Uploaded as-is they fully quarantine (no
`table/column/type` headers) → empty graph. Even reshaped, a glossary carries meaning but not the
structural facts (joins, grain, as-of) features need. No single source has everything; the architecture
fuses partial sources and must never let a confident guess about a load-bearing field pose as an attested
fact.

---

## 2. Layer 0 — object identity (define before anything attaches to it) [item 10]

Structural fusion is a **provider-fusion problem from day one**, not a later ad-hoc merge. Evidence
attaches to a **logical** object and retains its **provider** origin:

```python
@dataclass(frozen=True)
class LogicalObjectRef:      # the stable identity evidence hangs on
    logical_catalog_id: str; schema: str; table: str; column: str | None

@dataclass(frozen=True)
class ProviderObjectRef:     # where a given assertion physically came from
    provider_id: str; provider_snapshot_id: str; native_ref: str   # e.g. FTR row, OM FQN, DDL line
```

A glossary and a later OpenMetadata/DDL snapshot are two **providers** describing the same
`LogicalObjectRef`. Keying evidence on the logical ref now prevents a Phase-3 identity redesign when the
structural source arrives. (v1 keyed on `source+table+column`, which would have forced that migration.)

---

## 3. The authority model — a (producer × status) lattice, field-evaluated [items 1, 2]

Authority is **not** one total order like `LLM < parser < source < human`. Whether a
`STRUCTURAL_SOURCE@attested` beats a `PROFILE_SUPPORTED`, or whether profile-uniqueness suffices, depends
on the **field**. So separate the two axes and let the field policy judge combinations.

### 3.1 Producer and status (the two axes)

```python
class EvidenceProducer(StrEnum):
    SOURCE = "source"; STRUCTURAL_CONNECTOR = "structural_connector"; PARSER = "parser"
    LLM = "llm"; PROFILER = "profiler"; TAXONOMY = "taxonomy"; HUMAN = "human"

class EvidenceStatus(StrEnum):
    PROPOSED = "proposed"; SUPPORTED = "supported"; ATTESTED = "attested"
    CONFIRMED = "confirmed"; REJECTED = "rejected"; STALE = "stale"
```

An assertion is a `(producer, status)` pair (e.g. `(profiler, supported)`, `(human, confirmed)`), plus
its value and evidence. There is no global ranking of these pairs.

### 3.2 Authority propagation (status-based, not `min(enum)`) [corrected]

> A derived value **cannot carry a stronger assertion status than any required input**, and its
> usability is re-evaluated by the **target field's** policy — not by a scalar minimum.

So `additivity = registry(concept)` where the input `concept` is `(llm, proposed)` yields
`additivity @ (taxonomy-derived, proposed)`; the additivity field's policy then decides whether a
proposed taxonomy derivation may gate a sum (it may not — §8). The governed mapping makes the *rule*
sound; it cannot lift the *input's* status. This closes the concept back-door (`monetary_flow` vs
`monetary_stock`) without pretending statuses form one ladder.

### 3.3 Source trust is per (source, field) [item 2]

`SOURCE_DECLARED` is not automatically governed. A glossary curates definitions/domain/BIAN but its
`sensitivity=public` or prose-embedded type hint must **not** override a taxonomy floor or structural
restriction. Each reader declares a capability profile:

```python
@dataclass(frozen=True)
class SourceCapabilityProfile:
    source_type: str                              # "ftr_glossary"
    governed_fields: frozenset[str]               # asserted at ATTESTED status  (business_term, definition, domain, bian_path, fibo_path)
    declared_unverified_fields: frozenset[str]    # asserted at PROPOSED status  (sample_profile, sensitivity, logical_type_hint)
    structural_fields: frozenset[str]             # none for a glossary
```

Authority is granted per (source, field): a governed field enters at `attested`; an unverified field
enters at `proposed`, subject to the field policy like any other proposal.

---

## 4. Field policy contract — a policy *language*, not an allowlist [item 1]

v1's `frozenset[...] min_authority` was mislabelled (a set is an allowlist, and it can't express "LLM
proposal **plus** profile support qualifies as review-ready"). v2 uses rules with `any_of` / `all_of`:

```python
@dataclass(frozen=True)
class Condition:
    producer: EvidenceProducer; status: EvidenceStatus

@dataclass(frozen=True)
class AuthorityRule:
    any_of: tuple[Condition, ...] = ()      # any single condition satisfies
    all_of: tuple[Condition, ...] = ()      # a combination is required

@dataclass(frozen=True)
class FieldPolicy:
    risk_class: FieldRiskClass                     # ADVISORY | STRUCTURAL | SAFETY_CRITICAL
    display_rules: tuple[AuthorityRule, ...]       # may appear in the (search/display) projection
    operational_rules: tuple[AuthorityRule, ...]   # may GATE feature construction
    conflict_strategy: ConflictStrategy            # per-field algebra (§6.2)
    reupload_strategy: ReuploadStrategy            # §6.3
    unresolved_behavior: UnresolvedBehavior        # what "load-bearing effective" is when unmet
```

Examples:

```python
# join: usable only when structurally attested or human-confirmed
operational_rules=(AuthorityRule(any_of=(Condition("structural_connector","attested"),
                                         Condition("human","confirmed"))),)
# grain: LLM+profile is a review-ready proposal; only structural/human/profile+human is load-bearing
display_rules=(AuthorityRule(all_of=(Condition("llm","proposed"),Condition("profiler","supported"))),)
```

### 4.1 Producer × mutation matrix [item 11]

Each producer's allowed effects are fixed so "proposal" can't be reinterpreted per phase:

| Producer | Write evidence | Emit proposed event | Display projection | Feature gating |
|---|---|---|---|---|
| Glossary reader | yes | governed fields → yes | yes | field-policy dependent |
| Parser (sample values) | yes | derived only | yes | limited (corroboration) |
| LLM | yes | yes | yes | advisory fields only |
| Profiler | yes | yes | yes | never alone for joins/entity |
| Taxonomy | derived evidence | no new human event | yes | authority-propagated (§3.2) |
| Human | yes | confirmation event | yes | yes |

---

## 5. Evidence & decision persistence [items 3, 4]

Reuse **one** event/authority framework, but do not force every field into a generic JSON `OVERLAY_FACT`.
Three record kinds + the existing specialized facts:

- **`events`** — the universal append-only lifecycle (unchanged).
- **`field_evidence`** — every producer claim, item-level (the load-bearing evidence index; `llm_call`
  stays the immutable *raw* audit):

  ```python
  @dataclass(frozen=True)
  class FieldEvidenceRecord:
      evidence_id: str; logical_ref: LogicalObjectRef; field_name: str
      proposed_value: object; proposed_value_hash: str
      producer: EvidenceProducer; producer_ref: str            # llm_call_ref / parser_run / profile_run / event_id
      producer_item_ref: str | None                            # the BATCH ITEM ref within an llm_call (Pass A)
      evidence_spans: tuple[str, ...]; confidence_band: str | None
      source_snapshot_id: str; object_identity: str; input_hash: str; producer_version: str
      created_at: datetime
  ```
- **`field_decision_events`** — the proposed/confirmed/rejected/staled lifecycle of a field's effective
  value:

  ```python
  @dataclass(frozen=True)
  class FieldDecisionRecord:
      decision_id: str; logical_ref: LogicalObjectRef; field_name: str
      selected_evidence_ids: tuple[str, ...]; effective_value: object; effective_value_hash: str
      authority_state: tuple[Condition, ...]; decision_rule_version: str
  ```
- **Specialized typed facts stay typed:** `grain`, `availability_time`, and `approved_join` keep their
  existing keys/validators (`approved_join`'s two-endpoint `ApprovedJoinRef` + dual-owner
  `join_confirmation` is exactly the relationship confirmation path — §... ). New load-bearing table
  facts (grain candidate → confirmed grain) flow through the existing PROPOSED→CONFIRMED events, not a
  JSON blob.

Rule of thumb: **advisory/soft fields** live as `field_evidence` + a `field_decision`; **load-bearing
facts with established typed schemas** (grain, availability, joins) reuse the specialized fact events.
The graph tables stay flat; a `graph_field_evidence` view links each effective node/edge property to its
decision + evidence for provenance queries.

---

## 6. The resolver — one function, two outputs, field-specific merge

### 6.1 Two projections from one resolver

A single resolver reads all `field_evidence` for a `(logical_ref, field)` and emits **both**:
`display_effective` (strongest accepted proposal, satisfies `display_rules`) and `load_bearing_effective`
(satisfies `operational_rules`, else `unresolved`). Never two code paths — they would drift.

### 6.2 Conflict lattices are field-specific [item 8]

No generic `max(values)`. Each safety/structural field declares its algebra:

- **Ordered severity (a floor):** `sensitivity` severity `public < internal < confidential < restricted
  < prohibited` — take the most restrictive; taxonomy supplies a **minimum floor** only (§8), never a
  ceiling.
- **Accumulated set:** `sensitivity_classes` (`personal_data`, `financial_data`, `health_data`,
  `protected_attribute`) — **union** of all non-rejected evidence; these are incomparable, not collapsed.
- **Boolean safety flags** (`leakage_anchor`): `true` dominates `false`.
- **Grain / join conflict:** disagreement → **unresolved, no winner** (surface a review item).

### 6.3 Staleness & re-upload merge [item 9, item 12]

Every evidence record carries `source_snapshot_id + object_identity + input_hash + producer_version`.
On re-upload the resolver does NOT blindly re-apply "current LLM proposals by object ref":

```text
same logical object + same relevant input_hash   -> evidence reusable
same object + changed input                       -> old evidence STALE (must be re-produced)
renamed object with a CONFIRMED identity mapping  -> evidence migrated explicitly
removed object                                    -> evidence inactive
```

Human-confirmed overrides survive (generalize `entity_suggestion status='applied'`) but **revalidate**
if the underlying object materially changes — a confirmed value on a column whose type/definition moved
becomes a review item, not a silent carry-over. `build_graph` order: build source → taxonomy derivations
(status-propagated) → reusable+fresh proposals → confirmed overrides (revalidated) → per-field conflict
merge.

---

## 7. Feature readiness — blockers first, percentages second [item 7]

A percentage misleads ("82% safety complete" can hide one unclassified PII column). Compute from explicit
requirements; gate features on **blockers**, not scores:

```python
@dataclass(frozen=True)
class ReadinessRequirement:
    requirement_id: str                      # "table.grain", "column.sensitivity"
    status: Literal["confirmed","proposed","missing","conflicting"]
    blocking: bool; authority_required: tuple[Condition, ...]

@dataclass(frozen=True)
class FeatureReadiness:
    operational_status: Literal["ready","blocked"]
    blocking_requirements: tuple[ReadinessRequirement, ...]
    review_requirements: tuple[ReadinessRequirement, ...]
    advisory_gaps: tuple[str, ...]
    summary_scores: dict[str, float]          # DISPLAY only, derived from the requirements
```

Feature generation reads `blocking_requirements`; the percentages are a dashboard convenience derived
from the same requirement list.

---

## 8. Safety defaults — taxonomy is a floor, not the final word [item 10]

Concept-registry sensitivity (`customer_identifier → sensitive`) is a **baseline minimum restriction
floor**. Source/human evidence may make it **more** restrictive; nothing (including a confident LLM or a
`sensitivity=public` glossary cell) may make it **less** restrictive without an explicit governed
override. `taxonomy=INTERNAL` + `source=RESTRICTED` → `RESTRICTED`; `taxonomy=RESTRICTED` + `llm=PUBLIC`
→ `RESTRICTED`. Fail-closed. (Real sensitivity also depends on jurisdiction, masking, direct-vs-derived,
column combinations, purpose — so the taxonomy value is deliberately only the floor.)

---

## 9. Relationship promotion (Pass C) — evidence matrix, into the existing lifecycle

Semantic similarity is never authority. Two `ACCOUNT_ID`s may be different namespaces (customer vs
counterparty). Promotion uses the join evidence matrix (LLM-only → proposal; +profile overlap/cardinality
→ review candidate; declared FK / approved catalog join → usable; human-confirmed → usable). **Default
banking posture: source or human authority required to *use* a join.** Proposals flow through the
existing `approved_join` two-endpoint fact + dual-owner `join_confirmation` lifecycle — no new edge
authority system. Profiling **supports** grain/join/cardinality candidates but **cannot** establish
semantic entity identity alone [item 11] — entity promotion needs source / governed identifier namespace
/ taxonomy-backed / human evidence, expressed as `all_of` rules.

---

## 10. Type model + the corrected "core" invariant [item 12]

Three type layers: `physical_type` (DB source), `logical_type` (representation, e.g. `numeric_string`),
`semantic_type` (role, e.g. `identifier`). Never label a prose-derived type as a database type.

**Corrected invariant** (v1 overclaimed "the core is untouched"):

> The existing ingestion lifecycle remains the orchestration backbone. New evidence, authority
> resolution, and merge stages are **inserted** without weakening validation, the append-only fact
> history, drift handling, quarantine, or rebuildability. Reader output, evidence persistence, the graph
> projection, re-upload merge, field resolution, review diagnostics, and possibly some fact types **do**
> change.

---

## 11. Calibration — policy-gated, never authority-granting [item 16]

Auto-promotion requires **both**: the field policy explicitly permits calibrated auto-promotion **and**
the calibrated reliability threshold passes. Calibration never creates authority for a safety/structural
field (definition/domain may auto-promote; concept is advisory-validated; grain/join/sensitivity are
never sole-authority auto-promoted). Store `model_reported_confidence` and `calibrated_reliability`
separately; calibration governs the **advisory** tier where the decision is "auto-promote vs review."
Reuse the batched-enrichment gold-set harness; per-field gates (sensitivity: zero false-negative on the
critical set; join: zero false-confirmed; grain: zero incorrect auto-promotions). Gate controls
promotion, not proposal generation.

---

## 12. Hierarchical LLM batching (unchanged shape; Pass A shipped)

- **Pass A — column semantics** (SHIPPED = the merged batching engine): concept, logical-type proposal,
  role/sensitivity *proposals*, normalized definition/domain — 20–40 cols/call, focused retry.
- **Pass B — table synthesis:** entity, grain candidate + key candidates, event-vs-snapshot, time cols.
- **Pass C — relationships:** deterministic candidate **blocking** first, then §9 evidence; hard caps
  (O(tables²)); `relationship_search_truncated=true` when a cap hits — never claim full coverage silently.
- **Pass D — reconciliation:** contradictions / islands / unresolved entities / review priorities →
  **worklist only, no graph-writing authority.**

---

## 13. Field policy matrix (corrected)

| Field | LLM may propose | Aids search now | May gate feature-gen | Load-bearing rule (operational) |
|---|---|---|---|---|
| definition | yes | yes | not directly | LLM proposed allowed |
| domain | yes | yes | advisory routing only | LLM proposed allowed |
| concept | yes | yes | yes (registry-validated) | validated controlled concept |
| feature_role | yes | yes | ranking only | LLM proposed allowed |
| logical_type | yes | yes | limited | parser corroboration or source attested |
| additivity | fallback only | yes | yes | taxonomy-derived **from a confirmed concept**, or confirmed |
| temporal_role | fallback only | yes | yes | taxonomy(confirmed concept) / source / human |
| sensitivity | yes, conservative | yes | yes | source / taxonomy-floor / human; most-restrictive |
| leakage_anchor | proposal only | yes | yes | governed taxonomy / human |
| entity | yes | yes | soft until confirmed | source / human / (profile **+** taxonomy/namespace) |
| grain | yes | display only | no | structural or human (profile supports, not alone) |
| as_of / availability | yes | display only | no | structural or human confirmation |
| join | yes | display only | no | approved structural or human confirmation |
| cardinality | yes | display only | no | structural / (profile-supported **+** confirmation) |

("profile-supported alone" is removed as a sole authority for `entity`; additivity/temporal_role now
require a **confirmed** concept, per §3.2 propagation.)

---

## 14. Revised sequencing [items 5, 6] — kernel first

### Phase 0 — Authority kernel (ships before any new enrichment)
Logical/provider identity (§2); `field_evidence` + item-level `llm_call` linkage; `FieldPolicy` registry;
the `AuthorityRule` evaluator; status propagation; per-field conflict lattices; the two-output resolver;
evidence staleness rules; `field_decision_events`. No new LLM enrichment yet. This is the minimum safe
foundation everything else stands on.

### Phase 1 — FTR semantic front door (no joins, no grain promotion) [item 6]
Glossary reader (+ rich BIAN/FIBO sidecar as provenance); deterministic sample-value parser; Pass A
proposals; concept normalization; taxonomy derivation (status-propagated); flat graph projection with
provenance via the resolver; source-capability + blocker-based readiness diagnostics. Proves
`glossary row → column evidence → field authority resolution → rich searchable node`.

### Phase 2 — Table facts
Pass B; grain / availability candidates; event/snapshot classification; a review queue for load-bearing
table facts; human confirmation persistence (revalidated on re-upload).

### Phase 3 — Structural provider fusion + relationships
The logical-catalog/provider model in anger; OpenMetadata/DDL pairing by `LogicalObjectRef`; profiling
evidence; deterministic relationship candidate blocking; Pass C; `approved_join` proposals + evidence-based
confirmation.

### Phase 4 — Reconciliation & operating model
Pass D; calibration; prioritized HITL (risk × feature-unlock × evidence strength × reuse) with
feature-unlock analysis; conflict-resolution workflows; operational dashboards.

---

## 15. What stays invariant
The corrected §10 statement. Enrichment/evidence stays advisory + fail-soft; governed egress + audit on
every LLM call; the graph is a rebuildable projection; `events` / `field_evidence` / `field_decision_events`
/ `llm_call` / the typed facts are the truth.

## 16. Open dependencies
- Availability of a structural source (governs Phase 3 effort).
- Confirming the `approved_join` envelope carries every relationship dimension (direction, cardinality,
  namespace, evidence, effective-time, supersession) — extend if not.
- Reconciling the ungoverned declared-`joins_to` shortcut with the governed `approved_join` path (treat a
  declared join as `(source, attested)`).
- Migration of the existing flat `graph_node`/`graph_edge` to carry decision/evidence links without
  regressing search/lineage query performance.

## 17. Readiness
Architecture: approved. Spec: ready for task-level planning once Phase 0's kernel contracts (identity,
field_evidence, FieldPolicy, AuthorityRule evaluator, resolver, staleness) are the acceptance criteria —
they now are. The honest guarantee: aggressively LLM-driven proposals, with deterministic validation and
human confirmation as the only paths by which a value acquires operational authority over features,
safety, point-in-time correctness, or joins.
