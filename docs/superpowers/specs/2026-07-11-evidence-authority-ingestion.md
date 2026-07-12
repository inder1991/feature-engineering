# Evidence-Authority Ingestion — Design Spec (v5)

Date: 2026-07-11. Status: DESIGNED — architecture APPROVED. v3 tightened the contracts; v4 added the §0
reuse map (Phase 0 is an extension, not greenfield); **v5 adds §U — this IS the unified file-ingestion
pattern**: every file upload (technical CSV, glossary CSV, Excel) flows through this one evidence→authority
flow as a *source profile*; the only other pattern is the OpenMetadata connector. The existing technical
upload pipeline is **reused as the shared spine**, not frozen as a parallel path. Supersedes the scope of
`2026-07-11-batched-enrichment.md` (merged to main `6345296`) — that batching work is **Pass A transport**.

## The one-sentence architecture (unchanged, affirmed twice)

> Multiple evidence producers propose graph attributes; a **field-specific authority policy** decides
> what may become operational. The graph projection exposes the strongest currently-accepted value; the
> event/evidence log retains every proposal, conflict, promotion, and supersession.

Permanent invariants: **provenance ≠ authority; confidence ≠ permission.**

## What v3 tightened (the review's 16 contract items)

1. `AuthorityRule(any_of, all_of)` → a closed **AuthorityPredicate tree** (§4.1).
2. Split conflated `EvidenceStatus` into **AssertionStrength** + **EvidenceLifecycle** (§3.1).
3. `FieldDecisionRecord` → append-only **FieldDecisionEventV1** with replay/supersession fields (§5.2).
4. **Object identity resolution** contract — `ObjectIdentityStatus` + `ProviderObjectBinding`; no attach
   when ambiguous (§2).
5. Field policy gains **disqualifiers** (stale/conflict/ambiguous/pending-revalidation) (§4.3).
6. Safety-floor downgrades require a distinct **GovernanceAuthority + SafetyOverride**, not generic human
   confirmation (§7).
7. Concept split into **advisory vs operational** use; operational authority scales with derived-field
   blast radius (§8).
8. Pass A: **transport shipped, evidence schema to extend** + a legacy-cache migration plan (§15.1).
9. Readiness is **scoped**; the load-bearing decision is **recipe/run-scoped** (§9).
10. Precise **typed-fact vs field-decision boundary** via `resolution_mode` (§4.4, §5.3).
11. The ungoverned `joins_to` shortcut is a **Phase 0/1 hardening** item with a retirement plan (§12.1).
12. Replay needs a **producer_configuration_hash**, not just a version (§5.1).
13. **Conflict-review lifecycle** with a stable fingerprint (§10).
14. Human confirmations carry **scope + expiry + revalidation** (§11).
15. Pass B outputs have an explicit **destination mapping** (§15.2).
16. The field matrix uses three influence tiers — **DISPLAY / RECOMMENDATION / OPERATIONAL** (§4.2, §16).

---

## U. Unified ingestion — this IS the file-ingestion pattern [v5]

The platform has exactly **two ingestion patterns**: (1) **file ingestion** — CSV / Excel / glossary,
all through this one evidence→authority→resolution→graph flow; (2) the **OpenMetadata connector** — a
structural source that is a *producer into* the same flow. There is no third, legacy path: the existing
technical-CSV pipeline is **reused as the shared spine**, and the technical CSV is just one **source
profile** within the unified flow — not a frozen parallel branch.

### U.1 One flow, many source profiles
Every upload reduces to `(rows, SourceCapabilityProfile)` (§3.3). The profile — not the code path — is
what differs:
- **Technical CSV** — attests structure + facts: `type/grain/joins_to/sensitivity/additivity` enter as
  `source/attested` (high strength, fast to load-bearing). *Structure-vouched.*
- **Glossary CSV** — attests semantics, proposes structure: `definition/BIAN/FIBO` attested;
  `domain/type-hint/sensitivity` proposed. *Semantics-vouched, structure-incomplete.*
- **OpenMetadata** — a `structural_connector` profile: `type/joins` attested from the real catalog.

The FTR glossary and a technical CSV are therefore the **same kind of thing** — sources with different
capability profiles — resolved by the same field-authority policies into the same `graph_node`.

### U.2 Reuse map for the existing upload pipeline (the shared spine)
| Existing component | Role in the unified pattern |
|---|---|
| `read_csv_rows`/`read_excel_rows`/`_headers.py` | the technical/Excel readers — each tagged with a profile |
| `CanonicalRow` | the common funnel shape (already true for CSV/Excel/OM) |
| `validate_rows` | reused, made **profile-aware**: a field is required only when the profile attests it (technical → `type` required; glossary → `type="unknown"` is a readiness gap, not a quarantine) |
| the fact/event substrate (`append_overlay_event`, `OVERLAY_FACT_*`, `resolve_fact`, `propose_fact`/`confirm_fact`, `approved_join`, expiry) | reused — **declared** facts and **inferred** facts both become evidence/proposals here (§U.3) |
| `build_graph` (`graph_node`/`graph_edge`) | reused as the projection of **resolved** values from any source |
| quarantine / Review Queue | reused — conflicts + revalidation become review items |
| Pass A batching, Phase 0 kernel, OM connector | reused as-is |

Net-new is only what it always was: `field_evidence`, the producers (parser, taxonomy, glossary reader),
the field-policy registry + resolve-and-project, the source profiles, readiness.

### U.3 The declared-fact reconciliation (reuse, don't fork)
Today the technical path has a shortcut: a **declared** fact is written straight as a CONFIRMED overlay
fact. Under the unified model a declared fact is `source/attested` evidence that **resolves per policy**.
Reconcile by reuse: route declared facts through the **same** `propose_fact`/resolution plumbing (not a
parallel write), and make "auto-confirm on declare" a **profile policy**, not a hardcoded shortcut. A
trusted technical upload can still fast-path (its profile grants attested strength + an auto-confirm
policy for declared structure) — preserving today's behaviour — while flowing through the *same*
evidence/authority machinery, so it is uniform and auditable next to glossary/LLM evidence. Interpretation:
**the current technical-CSV path already IS an evidence-authority pattern with an implicit
all-source-attested-auto-confirmed profile**; making that profile explicit is what unifies the two.

### U.4 Sequencing note
This is a **framing + reuse** change, not new kernel scope. Phase 1 stands up the unified entry (profiles
+ profile-aware validation) and proves the glossary vertical through it; the technical CSV runs the same
entry with its profile (its existing fact-assertion + `build_graph` reused **unchanged**). Fully routing
declared facts as source-attested evidence (§U.3) is an explicit, staged convergence — not a big-bang —
so the working technical path is never destabilized.

---

## 0. Reuse map — Phase 0 is an EXTENSION of the overlay fact substrate [v4]

A read of `src/featuregen/overlay/` shows a mature event-sourced *propose → confirm → fold → resolve*
substrate. The spec's new contract NAMES must reconcile to it rather than spawn a parallel governance
system (which the review explicitly warned against). Mapping each Phase-0 contract to what exists:

| Contract | Status | Existing home / gap |
|---|---|---|
| Field-decision events (append-only, replayable) | **REUSE** | The 6 `OVERLAY_FACT_*` events (`facts.py`) + `fold_overlay_state` (`state.py`) + `OverlayProjection` + `resolve_fact` (`resolve.py`) ARE this log — supersession (`confirms_event_id`, `target_event_id`), `evidence_ref` linkage present. `FieldDecisionEventV1` = a payload/status extension of this, not a new store. |
| Typed-fact vs generic resolution | **REUSE** | `DATA_FACT_TYPES`/`POLICY_FACT_TYPES` + per-type `FACT_VALUE_SCHEMAS` + `resolve_fact`. Caveat: there is **no untyped field channel** — every field must be a registered `fact_type`, so new advisory fields (concept/logical_type/…) need registering as fact types. |
| Human confirm w/ scope, expiry, revalidation | **REUSE** | `confirm_fact`/`enter_fact` + `resolve_ttl` + `schedule_expiry` + `fire_due_overlay_expiries` + `detect_catalog_changes` stale-on-source-change. Scope/expiry already modeled via `expires_at` + use_case. |
| Object identity + provider binding | **EXTEND** | `CatalogObjectRef`/`ApprovedJoinRef`/`fact_key` + `CatalogAdapter` native-oid rename detection exist; **add** the `exact/aliased/ambiguous/unresolved` status + the no-attach-when-ambiguous rule. |
| Authority policy | **EXTEND** | `resolve_authority` gives WHO-confirms (four-eyes, dual-owner, governance queue); **add** the WHAT-authority-a-value-has layer over (producer, strength) for advisory fields. |
| Disqualifiers | **EXTEND** | `resolve_fact` read-time guards (drift/stale/expiry/referent-gap) exist as reason strings; **add** "active conflict" + "ambiguous identity" and reify as a policy-driven set. |
| Assertion-strength axis | **NEW (small)** | Lifecycle exists (DRAFT/VERIFIED/STALE/REVERIFY/REJECTED); **add** producer + proposed/supported/attested/confirmed strength onto the existing `overlay_evidence` record. |
| Safety-override authority | **NEW** | Only a compliance-gated `policy_tag` exists; **add** a sensitivity floor + governed below-floor downgrade (governance authority + rationale + scope). |
| Conflict-review lifecycle | **NEW** | Quarantine + STALE/REVERIFY are not it; **add** a conflict record + fingerprint + OPEN→…→REOPENED. |
| Readiness scope model | **NEW — but DEFERRED out of Phase 0** | Nothing exists. It depends on resolved facts/fields and is a feature-gen-time concern → moved to **Phase 2/feature-gen**, not the kernel. |
| Governed `joins_to` | **NEW wiring** | Bypass confirmed: declared `joins_to` writes `graph_edge 'joins'` directly (no `kind` CHECK, no event). The governed `approved_join` dual-owner path (`propose_fact` → `join_confirmation`) exists; **wire** declared joins into it and gate the raw edge. |

**Net Phase-0 new surface (reuse-first):** (a) strength axis on `overlay_evidence`; (b) identity-status
enum + no-attach-when-ambiguous; (c) field-authority policy over (producer, strength) + reified
disqualifiers; (d) safety-override authority + sensitivity floor; (e) conflict-review lifecycle;
(f) governed-`joins_to` wiring. Everything else is extension of named existing modules. Readiness scope
is deferred. This is the scope the Phase 0 plan builds to.

---

## 1. Why (unchanged, condensed)

Real-world uploads are **business glossaries** (BIAN/FIBO term maps like the FTR
`FTR_Column_Mapping.csv`), not technical schema+facts catalogs — uploaded as-is they fully quarantine
(empty graph). Even reshaped, a glossary carries meaning but not structural facts (joins, grain, as-of).
No single source has everything; fuse partial sources, and never let a confident guess about a
load-bearing field pose as an attested fact.

---

## 2. Object identity — resolution before attachment [item 4]

Evidence attaches to a **logical** object and retains **provider** origin:

```python
@dataclass(frozen=True)
class LogicalObjectRef: logical_catalog_id: str; schema: str; table: str; column: str | None
@dataclass(frozen=True)
class ProviderObjectRef: provider_id: str; provider_snapshot_id: str; native_ref: str
```

But logical identity is **resolved, not assumed** — ingestion meets renames, case/alias variants,
one-term-to-many-columns, many-rows-to-one-column, schema/table moves. A binding step runs first:

```python
class ObjectIdentityStatus(StrEnum):
    EXACT = "exact"; ALIASED = "aliased"; AMBIGUOUS = "ambiguous"; UNRESOLVED = "unresolved"

@dataclass(frozen=True)
class ProviderObjectBinding:
    provider_ref: ProviderObjectRef; logical_ref: LogicalObjectRef | None
    status: ObjectIdentityStatus; evidence_refs: tuple[str, ...]; confirmed_by: str | None
```

**Evidence MUST NOT attach to a logical object while identity is `AMBIGUOUS`/`UNRESOLVED`** — an
incorrect FQN match would merge evidence from unrelated physical objects. `ALIASED` bindings require a
recorded (eventually human-confirmed) mapping; ambiguous ones become review items, not silent merges.

---

## 3. The authority model — producer × (strength, lifecycle), field-evaluated

Authority is not one total order. Separate the axes and let field policy judge combinations.

### 3.1 Three axes: producer, assertion strength, evidence lifecycle [item 2]

```python
class EvidenceProducer(StrEnum):
    SOURCE = "source"; STRUCTURAL_CONNECTOR = "structural_connector"; PARSER = "parser"
    LLM = "llm"; PROFILER = "profiler"; TAXONOMY = "taxonomy"; HUMAN = "human"

class AssertionStrength(StrEnum):     # how strongly THIS producer asserts the value
    PROPOSED = "proposed"; SUPPORTED = "supported"; ATTESTED = "attested"; CONFIRMED = "confirmed"

class EvidenceLifecycle(StrEnum):     # the record's own lifecycle (orthogonal to strength)
    ACTIVE = "active"; REJECTED = "rejected"; STALE = "stale"; SUPERSEDED = "superseded"
```

An assertion is `(producer, strength, lifecycle)` + value + evidence. Typical strengths: profiler →
`supported`, structural connector → `attested`, human → `confirmed`, LLM → `proposed`. **Human acceptance
of an LLM value creates a NEW human `confirmed` evidence row and a decision event — it never rewrites the
LLM row's producer/strength** (provenance stays honest). `rejected`/`stale`/`superseded` are lifecycle
transitions, never producer strengths.

### 3.2 Status propagation (not `min(enum)`)

A derived value cannot carry a stronger **assertion strength** than any required input; its **usability**
is re-judged by the target field's policy. `additivity = registry(concept)` with `concept @ (llm,
proposed)` yields `additivity @ (taxonomy, proposed)`; the additivity policy then decides whether a
*proposed* taxonomy derivation may gate a sum (it may not — §8). Governed mapping makes the *rule* sound;
it cannot lift the *input's* strength.

### 3.3 Source trust per (source, field)

A reader declares which fields it can authoritatively assert; unverified fields enter as *proposals*:

```python
@dataclass(frozen=True)
class SourceCapabilityProfile:
    source_type: str
    attested_fields: frozenset[str]              # governed → enters at strength=attested
    proposed_fields: frozenset[str]              # declared-but-unverified → enters at strength=proposed
    structural_fields: frozenset[str]            # none for a glossary
```

A glossary's `sensitivity=public` or prose type-hint enters as a *proposal*, never overriding a taxonomy
floor or structural restriction.

---

## 4. Policy — an expression language, not an allowlist

### 4.1 Authority predicate tree [item 1]

`any_of`/`all_of` as two fields is ambiguous (does a rule with both mean AND-of-OR or OR?). Use a closed,
composable predicate:

```python
class AuthorityPredicate: ...
@dataclass(frozen=True)
class HasEvidence(AuthorityPredicate): producer: EvidenceProducer; strength: AssertionStrength
@dataclass(frozen=True)
class AnyOf(AuthorityPredicate): conditions: tuple[AuthorityPredicate, ...]
@dataclass(frozen=True)
class AllOf(AuthorityPredicate): conditions: tuple[AuthorityPredicate, ...]
```

```python
operational = AnyOf((HasEvidence("structural_connector","attested"),
                     HasEvidence("human","confirmed")))
grain_review = AllOf((HasEvidence("llm","proposed"), HasEvidence("profiler","supported")))
```

Predicates evaluate only over `lifecycle == ACTIVE` evidence.

### 4.2 Three influence tiers [item 16]

"May gate feature-gen" is too binary — domain/feature_role *influence* generation without determining
buildability or safety. Every field declares its highest tier:

```python
class InfluenceTier(StrEnum):
    DISPLAY = "display"              # search / UI only
    RECOMMENDATION = "recommendation"  # ranking / routing / applicability
    OPERATIONAL = "operational"     # binding / joins / PIT / safety / computation
```

Policy carries a predicate per tier a field participates in.

### 4.3 Disqualifiers [item 5]

Positive rules aren't enough — an old human confirmation could still satisfy a rule after the source
changed. Evaluation is `satisfies(positive) AND NOT any(disqualifier)`:

```python
class Disqualifier(StrEnum):
    STALE_SELECTED_EVIDENCE = "stale_selected_evidence"
    ACTIVE_HIGH_SEVERITY_CONFLICT = "active_high_severity_conflict"
    AMBIGUOUS_OBJECT_IDENTITY = "ambiguous_object_identity"
    MISSING_REQUIRED_SNAPSHOT = "missing_required_snapshot"
    CONFIRMATION_PENDING_REVALIDATION = "confirmation_pending_revalidation"
```

### 4.4 Resolution mode [item 10]

Advisory fields resolve through the generic resolver; load-bearing typed facts resolve through their
specialized fact projection (§5.3). The policy states which:

```python
class ResolutionMode(StrEnum):
    GENERIC_FIELD = "generic_field"; SPECIALIZED_FACT = "specialized_fact"

@dataclass(frozen=True)
class FieldPolicy:
    influence_max: InfluenceTier
    display_rule: AuthorityPredicate
    recommendation_rule: AuthorityPredicate | None
    operational_rule: AuthorityPredicate | None
    disqualifiers: tuple[Disqualifier, ...]
    conflict_strategy: ConflictStrategy          # §6.2
    reupload_strategy: ReuploadStrategy          # §6.3
    resolution_mode: ResolutionMode              # §5.3
    unresolved_behavior: UnresolvedBehavior
```

### 4.5 Producer × mutation matrix (unchanged)

| Producer | Write evidence | Emit proposed event | Display projection | Feature gating |
|---|---|---|---|---|
| Glossary reader | yes | attested fields → yes | yes | field-policy dependent |
| Parser | yes | derived only | yes | limited (corroboration) |
| LLM | yes | yes | yes | advisory tiers only |
| Profiler | yes | yes | yes | never alone for joins/entity |
| Taxonomy | derived evidence | no human event | yes | strength-propagated |
| Human | yes | confirmation event | yes | yes |

---

## 5. Persistence — evidence, decisions, typed facts

### 5.1 field_evidence (item-level; llm_call stays raw audit) [items 4, 12]

```python
@dataclass(frozen=True)
class FieldEvidenceRecord:
    evidence_id: str; logical_ref: LogicalObjectRef; field_name: str
    proposed_value: object; proposed_value_hash: str
    producer: EvidenceProducer; strength: AssertionStrength; lifecycle: EvidenceLifecycle
    producer_ref: str                     # llm_call_ref / parser_run / profile_run / event_id
    producer_item_ref: str | None         # BATCH ITEM ref within an llm_call (Pass A)
    producer_configuration_hash: str      # replay: model+prompt+schema+gen-settings | parser rules+registry+locale
    evidence_spans: tuple[str, ...]; confidence_band: str | None
    source_snapshot_id: str; object_identity: str; input_hash: str; producer_version: str
    created_at: datetime
```

`producer_configuration_hash` makes replay deterministic — for the LLM it fingerprints
model/prompt/schema/generation-settings; for a parser/taxonomy it fingerprints the rule-set + concept
registry snapshot + locale + normalization. `producer_ref` must resolve the raw record (e.g. `llm_call`).

### 5.2 field_decision_events (append-only, replayable) [item 3]

```python
@dataclass(frozen=True)
class FieldDecisionEventV1:
    decision_event_id: str; logical_ref: LogicalObjectRef; field_name: str
    event_type: Literal["resolved","confirmed","rejected","staled","superseded"]
    selected_evidence_ids: tuple[str, ...]; evidence_set_hash: str
    display_effective_value: object | None
    load_bearing_effective_value: object | None       # None when authority insufficient
    conflict_status: str; reason_codes: tuple[str, ...]
    field_policy_version: str; resolver_version: str
    actor_ref: str | None; supersedes_event_id: str | None; created_at: datetime
```

The two-output resolver persists **both** effective values in one event (same evidence set + policy
version). Supersession chains give replay; nothing is mutated in place.

### 5.3 Typed-fact boundary [item 10]

For `resolution_mode == SPECIALIZED_FACT` (grain, availability, join):

```text
field_evidence            -> PROPOSAL evidence only (a grain candidate, a join proposal)
specialized fact event    -> references the evidence_ids; is the OPERATIONAL truth
graph projection          -> reads the specialized fact projection for the load-bearing value
```

The generic resolver may still emit a **display** candidate (`grain_candidate = transaction`), but the
**load-bearing** value comes *exclusively* from the specialized fact projection — there is **no**
independent load-bearing `FieldDecisionEvent` for a specialized field. Rejecting a grain proposal marks
its evidence `rejected` and emits no grain fact.

### 5.4 events + graph
`events` = the universal append-only lifecycle. `grain`, `availability_time`, `approved_join` keep their
typed schemas. `graph_node`/`graph_edge` stay flat for search/lineage; a `graph_field_evidence` view
links each effective property → its decision event + evidence for provenance queries.

---

## 6. Resolver — two outputs, field-specific merge, staleness

### 6.1 One resolver, two outputs
Reads all `ACTIVE` evidence for `(logical_ref, field)`; emits `display_effective` (satisfies the display
predicate) and `load_bearing_effective` (satisfies the operational predicate AND no disqualifier fires,
else `unresolved`). One function — the outputs never drift.

### 6.2 Conflict lattices are field-specific [no generic max()]
- **Ordered severity floor:** `sensitivity` `public < internal < confidential < restricted < prohibited`
  — most restrictive wins; taxonomy is a floor only (§7).
- **Accumulated set:** `sensitivity_classes` (personal/financial/health/protected) — union of non-rejected
  evidence.
- **Boolean safety flags** (`leakage_anchor`): `true` dominates.
- **Grain/join conflict:** disagreement → unresolved, no winner → review item (§10).

### 6.3 Staleness & re-upload [items 9-context]
Evidence carries `source_snapshot_id + object_identity + input_hash + producer_configuration_hash`. On
re-upload: same object + same input_hash → reusable; changed input → `STALE`; renamed with a confirmed
identity mapping → migrated; removed → inactive. Confirmed overrides survive but **revalidate** on
material change (→ `CONFIRMATION_PENDING_REVALIDATION` disqualifier until re-confirmed). Resolver order:
source → taxonomy derivations (strength-propagated) → reusable-fresh proposals → confirmed overrides
(revalidated) → per-field conflict merge → disqualifier check.

---

## 7. Safety floor + governed override [item 6]

Concept-registry sensitivity is a **baseline minimum restriction floor**. It may only be made *more*
restrictive by source/human evidence. A downgrade **below** the floor is not a normal confirmation — a
data owner asserting `RESTRICTED → PUBLIC` must not silently win. It requires a distinct governance act:

```python
class GovernanceAuthority(StrEnum):
    DATA_OWNER = "data_owner"; SECURITY = "security"; PRIVACY = "privacy"; MODEL_RISK = "model_risk"

@dataclass(frozen=True)
class SafetyOverride:
    field: str; previous_floor: str; override_value: str
    approved_by_authority: GovernanceAuthority; rationale: str
    policy_reference: str; effective_until: datetime | None
```

A reduction below a safety floor requires the specific authority + explicit reason + scoped effective
period + policy reference. Absent a `SafetyOverride`, the floor holds (fail-closed).

---

## 8. Concept — advisory vs operational [item 7]

"Registry-valid" only proves the value *exists* in the taxonomy, not that it's *correct* — a valid but
wrong `monetary_flow` must not silently authorize an additive aggregation. Split the uses:

| Concept use | Requirement |
|---|---|
| **Advisory** (search, retrieval, candidate recipe matching) | registry-valid LLM proposal |
| **Operational** (derives additivity / sensitivity / leakage / PIT / entity / join semantics) | source-attested, human-confirmed, or *permitted calibrated auto-promotion for low-risk concepts* |

There is no single operational concept rule — the required authority scales with the **blast radius of
what the concept derives**. A concept feeding a safety derivation inherits that field's stricter bar.

---

## 9. Feature readiness — scoped; load-bearing is recipe/run-scoped [item 9]

Catalog-wide readiness misleads (one unresolved sensitivity on an unused archive table must not block all
generation). Readiness is computed at multiple scopes; the **gating** decision is recipe/run-scoped:

```python
class ReadinessScopeType(StrEnum):
    CATALOG = "catalog"; TABLE = "table"; GENERATION_RUN = "generation_run"; RECIPE = "recipe"

@dataclass(frozen=True)
class ReadinessRequirement:
    requirement_id: str; scope: ReadinessScopeType
    status: Literal["confirmed","proposed","missing","conflicting"]
    blocking: bool; authority_required: AuthorityPredicate

@dataclass(frozen=True)
class FeatureReadiness:
    scope: ReadinessScopeType; operational_status: Literal["ready","blocked"]
    blocking_requirements: tuple[ReadinessRequirement, ...]
    review_requirements: tuple[ReadinessRequirement, ...]
    advisory_gaps: tuple[str, ...]; summary_scores: dict[str, float]   # DISPLAY only
```

Feature generation asks: *are all fields actually used by THIS plan sufficiently authoritative?* Catalog
readiness is for planning/UI; percentages are derived from the requirement list, never the gate.

---

## 10. Conflict-review lifecycle [item 13]

Conflicts (esp. human-confirmed-vs-new-source) need a stable identity so a re-upload doesn't spam
duplicates:

```python
conflict_fingerprint = hash(logical_ref, field_name, sorted(competing_value_hashes), field_policy_version)

class ConflictState(StrEnum):
    OPEN="open"; ACKNOWLEDGED="acknowledged"; RESOLVED="resolved"
    DISMISSED="dismissed"; STALE="stale"; REOPENED="reopened"
```

A conflict record carries the fingerprint, competing evidence ids, severity, owner, and lifecycle;
re-detecting the same fingerprint reopens/updates rather than duplicates.

---

## 11. Human confirmation — scope + expiry + revalidation [item 14]

Not every confirmation lives forever. Confirmation records carry:

```python
effective_from: datetime; effective_until: datetime | None
scope: str                 # column / table / recipe / global
review_due_at: datetime | None
```

Re-upload revalidation distinguishes: still-valid / stale (input changed) / expired (`effective_until`
passed) / conflicts (new source disagrees). A grain confirmation may be durable; a sensitivity override
may need annual review; a join approval may be schema-version-bound.

---

## 12. Relationships (Pass C) — evidence matrix, governed lifecycle

Semantic similarity is never authority (two `ACCOUNT_ID`s may be different namespaces). Promotion uses
the join evidence matrix (LLM-only → proposal; +profile overlap/cardinality → review candidate; declared
FK / approved catalog join → usable; human-confirmed → usable). Default banking posture: source or human
authority required to *use* a join. Proposals flow through the existing `approved_join` two-endpoint fact
+ dual-owner `join_confirmation` lifecycle. Profiling **supports** grain/join/cardinality candidates but
**cannot** promote semantic entity identity alone (needs source / governed namespace / taxonomy / human,
expressed as `AllOf`).

### 12.1 Retire the ungoverned `joins_to` bypass — Phase 0/1 hardening [item 11]
Today a declared `joins_to` writes a directly-usable `graph_edge 'joins'` with no event — a **bypass of
the entire authority model**. This is not a Phase-3 cleanup. Required, dated:
```text
declared joins_to  ->  source evidence (strength=attested per source capability)
                   ->  approved_join PROPOSED/ATTESTED event
                   ->  governed projection (display now; feature-use per join policy)
```
No direct operational edge write. Migrate existing declared joins into attested fact records; keep
rendering them in the display graph; gate feature use through the join policy. Retirement deadline set in
the Phase 0/1 plan.

---

## 13. Type model + corrected invariant
Three type layers — `physical_type` (DB source), `logical_type` (`numeric_string`), `semantic_type`
(`identifier`). Never label a prose-derived type as a database type.

**Corrected invariant:** the existing ingestion lifecycle remains the orchestration backbone; new
evidence, authority resolution, and merge stages are **inserted** without weakening validation, the
append-only fact history, drift, quarantine, or rebuildability. Reader output, evidence persistence, the
graph projection, re-upload merge, field resolution, review diagnostics, and some fact types **do** change.

## 14. Calibration — policy-gated, never authority-granting
Auto-promotion requires the field policy to *permit* calibrated auto-promotion **and** the calibrated
threshold to pass; calibration never creates authority for a safety/structural field. Store
`model_reported_confidence` and `calibrated_reliability` separately. Reuse the batched-enrichment gold-set
harness; per-field gates (sensitivity: zero false-negative on the critical set; join: zero false-confirmed;
grain: zero incorrect auto-promotions). Gate controls promotion, not proposal generation.

---

## 15. Passes — honest status + destinations

### 15.1 Pass A — transport shipped, evidence schema to extend [item 8]
- **Shipped:** the batched-enrichment *transport + degradation substrate* (governed batch seam, ref-set
  validation, chunking, degradation ladder, per-item audit, kill switch) — and it currently returns
  `concept / definition / domain` only.
- **To extend for Phase 1:** logical/semantic type, identifier/temporal roles, evidence spans, per-field
  confidence, `source_snapshot_id`, `input_hash`, `producer_configuration_hash`, and **item-level
  FieldEvidenceRecords**. Phase 1 is not "wire in an existing Pass A" — it extends Pass A's evidence schema.
- **Legacy-cache migration:** existing `enrichment_*` rows → backfill producer/version where possible;
  **do not invent evidence spans retrospectively**; mark legacy concept values **display-only** unless
  they meet the operational concept bar (§8).

### 15.2 Pass B — table synthesis destinations [item 15]
| Pass B output | Destination |
|---|---|
| Table role | advisory field evidence |
| Primary entity | field evidence / confirmation |
| Grain candidate | typed **grain** fact proposal (`resolution_mode=specialized_fact`) |
| As-of candidate | typed **availability** fact proposal |
| Event/snapshot classification | advisory or structural per use |
| Time columns | evidence linked to the availability proposal |

### 15.3 Pass C / Pass D
Pass C = §12 (blocked candidates → join evidence → governed proposals). Pass D = reconciliation worklist
only (contradictions / islands / unresolved entities / review priorities) — **no graph-writing authority.**

---

## 16. Field policy matrix (three-tier)

| Field | Producers that may propose | Max influence tier | Operational rule (if OPERATIONAL) |
|---|---|---|---|
| definition | llm, source | RECOMMENDATION | — |
| domain | llm, source | RECOMMENDATION | — |
| concept (advisory) | llm | RECOMMENDATION | registry-valid |
| concept (operational) | llm→confirm, source, human | OPERATIONAL | source-attested / human-confirmed / low-risk calibrated |
| feature_role | llm | RECOMMENDATION | — |
| logical_type | llm, parser, source | OPERATIONAL (limited) | parser corroboration or source attested |
| additivity | taxonomy(confirmed concept), human | OPERATIONAL | taxonomy from a **confirmed** concept, or confirmed |
| temporal_role | taxonomy(confirmed concept), source, human | OPERATIONAL | taxonomy(confirmed) / source / human |
| sensitivity | taxonomy(floor), source, human, governance | OPERATIONAL | floor + most-restrictive; downgrade needs SafetyOverride |
| leakage_anchor | taxonomy, human | OPERATIONAL | governed taxonomy / human |
| entity | llm, source, human, (profiler+taxonomy) | OPERATIONAL | source / human / (profile **AND** namespace/taxonomy) |
| grain | llm, profiler, structural, human | OPERATIONAL (specialized_fact) | structural or human (profile supports, not alone) |
| as_of / availability | structural, human | OPERATIONAL (specialized_fact) | structural or human |
| join | llm, profiler, structural, human | OPERATIONAL (specialized_fact) | approved structural or human |
| cardinality | profiler, structural, human | OPERATIONAL | structural / (profile-supported AND confirmation) |

---

## 17. Sequencing + approval gates

### Phase 0 — Authority kernel (EXTENSION of the overlay substrate; before any new enrichment)
Per §0, this **extends** the existing event/fold/resolve/authority/confirmation machinery rather than
rebuilding it. New surface only: (a) strength axis on `overlay_evidence`; (b) `ObjectIdentityStatus` +
no-attach-when-ambiguous; (c) field-authority policy over (producer, strength) + reified disqualifiers;
(d) safety-override authority + sensitivity floor; (e) conflict-review lifecycle; (f) governed-`joins_to`
wiring (§12.1). The decision log, typed-vs-generic resolution, and confirm/expiry/revalidation are reused
as-is. **Readiness scope (contract 8) is deferred to Phase 2/feature-gen.**

**Phase 0 may proceed once these 10 contracts are finalized:** (1) authority-predicate semantics; (2)
assertion-strength vs evidence-lifecycle; (3) field-decision event schema; (4) object identity/binding;
(5) disqualifier semantics; (6) safety-override authority; (7) generic-field vs specialized-fact
resolution mode; (8) readiness scope model; (9) conflict-review lifecycle; (10) `joins_to` retirement.

### Phase 1 — FTR semantic front door (no joins, no grain promotion)
Glossary reader (+ BIAN/FIBO provenance sidecar); deterministic sample-value parser; **Pass A evidence
extension**; concept normalization; taxonomy derivation (strength-propagated); flat projection with
provenance via the resolver; scoped readiness diagnostics.

**Phase 1 must prove:** FTR row → stable logical object; rich context reaches the LLM; Pass A writes
item-level evidence; display projection shows proposals; operational projection stays `unresolved` where
authority is insufficient; **no safety/structural field becomes load-bearing from LLM evidence alone**;
re-upload stales changed proposals; human-confirmed values survive but revalidate.

### Phase 2 — Table facts
Pass B (destinations §15.2); grain/availability candidates → typed fact proposals; review queue; human
confirmation persistence (scoped/expiring).

### Phase 3 — Structural provider fusion + relationships
Logical/provider model in anger; OpenMetadata/DDL pairing by `LogicalObjectRef`; profiling evidence;
deterministic candidate blocking; Pass C; `approved_join` proposals + confirmation.

### Phase 4 — Reconciliation & operating model
Pass D; calibration; prioritized HITL (risk × feature-unlock × evidence strength × reuse) with unlock
analysis; conflict-resolution workflows; dashboards.

---

## 18. Invariants, open dependencies, readiness
Enrichment/evidence stays advisory + fail-soft; governed egress + audit on every LLM call; the graph is a
rebuildable projection; `events` / `field_evidence` / `field_decision_events` / `llm_call` / typed facts
are the truth. Open deps: structural-source availability (governs Phase 3); confirming the `approved_join`
envelope carries every relationship dimension; the flat-graph → decision/evidence-link migration without
regressing search/lineage. **Architecture: approved. Spec: ready for Phase 0 planning once the 10 Phase-0
contracts above are the acceptance criteria — they now are.** The guarantee: aggressive LLM proposals,
with deterministic validation or human confirmation the only path by which a value acquires operational
authority over features, safety, point-in-time correctness, or joins.
