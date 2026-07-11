# Evidence-Authority Ingestion — Design Spec (v1)

Date: 2026-07-11. Status: DESIGNED (architecture agreed in a design dialogue; not yet planned/built).
Supersedes the scope of `2026-07-11-batched-enrichment.md` — that batching work (now merged to main
`6345296`) is **Pass A** of this larger architecture.

## The one-sentence architecture

> The platform is not "the LLM enriches the graph." It is **multiple evidence producers propose graph
> attributes, and a field-specific authority policy decides what may become operational.** The graph
> projection exposes the strongest currently-accepted value; the event/evidence log retains every
> proposal, conflict, promotion, and supersession.

Two invariants sit under everything below:

- **provenance ≠ authority.** Recording *where* a value came from does not grant it permission to act.
- **confidence ≠ permission.** A field may have excellent provenance and high model confidence and
  still be prohibited from influencing feature construction, safety, point-in-time correctness, or joins.

---

## 1. Why — the problem this solves

The upload pipeline expects a **technical schema + facts catalog**: one row per column with
`table, column, type` (required) plus feature facts (`grain, as_of, sensitivity, joins_to, additivity,
…`). The artifact enterprises actually maintain and export is a **business glossary** — term name, long
business definition, domain, BIAN/FIBO lineage, synonyms, stewardship (e.g. the FTR
`FTR_Column_Mapping.csv`: `schema.table.column`, `description_business_definition`, `data_domain`,
`term_type`, `bian_level_1..4`, `fibo_level_1`).

Uploaded as-is, a glossary **fully quarantines** — its headers don't supply `table/column/type`, so
every row fails validation and the graph is empty. And even after a header/shape transform, a glossary
carries almost none of the *structural facts* (joins, grain, as-of) that make features build. A glossary
is rich in **meaning** and poor in **structure**; the technical catalog is the reverse; and a few facts
(`additivity`, `grain`, `as_of`) are FeatureGen's own feature-engineering judgments that nobody exports.
No single upload has everything. The architecture must fuse several partial sources — and must never let
a confident guess about a load-bearing field masquerade as an attested fact.

---

## 2. The five-layer pipeline

```text
Evidence producers        (glossary reader · deterministic parser · LLM · profiling · taxonomy · human)
        ↓
Proposal generation       (each producer emits FieldEvidence: value + producer + confidence + evidence refs)
        ↓
Deterministic validation  (schema / enum / taxonomy / consistency / conflict detection)
        ↓
Field authority policy    (per-field: may-propose · may-search · may-gate-features · min authority)
        ↓
Effective-value resolver  → flat query-optimized graph  +  feature-generation eligibility
```

The LLM is used **broadly** to generate proposals. Deterministic code and human confirmation decide what
becomes operational. The graph is a projection; the event/evidence stores are the truth.

---

## 3. Reuse — most of the authority substrate already exists (verified in code)

This is not greenfield. The platform already has the hard parts of the authority model; the new work is
mostly *evidence producers* feeding existing confirmation machinery.

- **PROPOSED → CONFIRMED is already how facts work.** Facts are event pairs (`OVERLAY_FACT_PROPOSED` +
  `OVERLAY_FACT_CONFIRMED`, plus `PARTIALLY_CONFIRMED / REJECTED / EXPIRED / STALED`) in the append-only
  `events` log. A "proposal" is an unconfirmed fact; "promotion" is a confirmation event. **Do not build
  a second authority framework.**
- **The join-authority layer largely exists.** `approved_join` is a first-class governed fact type;
  `ApprovedJoinRef` is a **two-endpoint** fact key (column pairs sorted as units so distinct joins can't
  alias); `join_confirmation.py` runs a **dual-owner** PROPOSED→CONFIRMED lifecycle with join-side roles.
  LLM relationship proposals land in the existing `approved_join` PROPOSED state — they do not get a new
  edge type. (Note the *ungoverned* shortcut today: a declared `joins_to` writes a `graph_edge 'joins'`
  directly in `build_graph` with no event — treat a declared join as a `SOURCE_ATTESTED` proposal, and
  reconcile that path with the governed one.)
- **The concept registry is the semantic control plane.** Each `Concept` carries `additivity`,
  `pit_role`, `sensitivity`, `entity_link`, `leakage_anchor`, `near_label`; `templates.py` reads them
  deterministically. Behavioral/safety semantics are **derived** from a governed taxonomy, not
  independently hallucinated (see §5, §6).
- **`llm_call` is the evidence store.** Every governed call records prompt/output/repair-trail/tokens.
  `FieldEvidence.evidence_refs` point at `llm_call` rows rather than duplicating them.
- **Human-confirmed-survives-reupload already exists.** `entity_suggestion` with `status='applied'` is
  re-applied on every `build_graph`. Generalize that pattern to all promoted fields (§7, §11).
- **Pass A already shipped.** The batched-enrichment engine (merged `6345296`): governed batch seam
  (`audited_batch_call`), ref-set validation, token-aware chunking, bounded degradation ladder (=
  focused retry), per-item audit, kill switch. Column-level enrichment is this engine.

---

## 4. Field authority policy (first-class registry)

A registry, not scattered `if`s. For every graph field:

```python
class FieldRiskClass(StrEnum):
    ADVISORY = "advisory"; STRUCTURAL = "structural"; SAFETY_CRITICAL = "safety_critical"

class EvidenceAuthority(StrEnum):
    SOURCE_DECLARED = "source_declared"; STRUCTURAL_SOURCE = "structural_source"
    DETERMINISTIC_DERIVATION = "deterministic_derivation"; LLM_PROPOSED = "llm_proposed"
    PROFILE_SUPPORTED = "profile_supported"; GOVERNED_TAXONOMY = "governed_taxonomy"
    HUMAN_CONFIRMED = "human_confirmed"

@dataclass(frozen=True)
class FieldPolicy:
    risk_class: FieldRiskClass
    permitted_producers: frozenset[str]
    graph_min_authority: frozenset[EvidenceAuthority]        # to appear in the (display) projection
    feature_use_min_authority: frozenset[EvidenceAuthority]  # to GATE feature construction
    conflict_behavior: str        # "prefer_confirmed" | "most_restrictive" | "block_feature_use"
    reupload_merge: str           # "refresh" | "preserve_confirmed"
    fail_mode: str                # "open" | "closed"
```

The load-bearing distinction: **`graph_min_authority` is usually permissive (an LLM proposal may appear
for search/display); `feature_use_min_authority` is strict for structural/safety fields.** A field can be
visible-but-not-load-bearing.

**Tier by blast radius** (this is the policy, not just provenance):
- **Advisory** (wrong → worse search/suggestions): `definition`, `domain`, `feature_role`, cardinality
  hints. May gate on `LLM_PROPOSED`.
- **Structural** (wrong → broken/leaky features): `logical_type`, `grain`, `as_of`, `join`,
  `cardinality`. Gate only on `STRUCTURAL_SOURCE` / `PROFILE_SUPPORTED` / `HUMAN_CONFIRMED`.
- **Safety-critical** (wrong → PII exposure or leakage): `sensitivity`, `leakage_anchor`, and grain/join
  where they feed the PIT gauntlet. Never gate on `LLM_PROPOSED`; `sensitivity` fails **closed** (an
  inferred sensitivity rounds toward *more* restrictive, never less).

### Two projections from one resolver

The graph node may carry a proposed value immediately (`grain_candidate = transaction @ LLM_PROPOSED`),
while feature grounding sees `effective_grain = unresolved` until the promotion authority is met. This
MUST be **one resolver emitting two outputs** (display-effective = best accepted proposal;
load-bearing-effective = authority-gated, else unresolved) from the same evidence set — never two code
paths that can drift.

---

## 5. The authority-propagation rule (closes the concept back-door)

Because safety fields are *derived* from `concept` via the governed registry, and `concept` itself may be
`LLM_PROPOSED`, a naive design lets an LLM-chosen concept smuggle safety semantics past the safety gate.
Example: the LLM labels a balance column `monetary_flow` (additive) instead of `monetary_stock`
(semi-additive) → `additivity=additive` derived from the governed taxonomy → a **sum-over-time on a
balance** (double-count leak), promoted on nothing but concept confidence.

> **Rule: the authority of a derived value is the *minimum* of the derivation rule's authority and its
> inputs' authorities — never granted by the derivation step.**

So `additivity = registry(concept)` where `concept @ LLM_PROPOSED` yields `additivity @ LLM_PROPOSED`,
**not** `@ GOVERNED_TAXONOMY`. The governed mapping makes the *rule* trustworthy; it cannot launder the
*input concept's* authority. Derived safety fields therefore cannot gate until the concept is confirmed
(or the derived value is independently attested). The registry lookup determines the *value*; propagation
determines the *authority*.

---

## 6. Evidence producers

Each producer emits a uniform record; a deterministic resolver (code, never another LLM call) fuses them:

```python
@dataclass(frozen=True)
class FieldEvidence:
    object_ref: str; field_name: str; proposed_value: object
    producer: Literal["source","parser","llm","profile","taxonomy","human"]
    producer_ref: str; authority: EvidenceAuthority
    confidence_band: str | None; evidence_refs: tuple[str, ...]   # -> llm_call rows / sample spans
```

- **Glossary reader** — maps the FTR shape → canonical rows (split `schema.table.column`; carry the
  BIAN/FIBO/aliases/process context as provenance input, not thrown away). `SOURCE_DECLARED`.
- **Deterministic sample-value parser** — the glossary embeds representative values ("…values such as
  `3708484836801`"). Parse them: all-digits → numeric; `15:07:08` → time; fixed-length digit strings →
  *identifier* (so `logical_type=numeric_string, semantic_type=identifier, allowed_numeric_aggregation=
  none` — preventing a naive `numeric` that would license invalid sums). `DETERMINISTIC_DERIVATION`.
- **LLM** — column semantics (concept, normalized definition/domain, roles), table synthesis, and
  relationship *proposals* (§10). `LLM_PROPOSED`.
- **Profiling** — uniqueness (grain support), value-overlap + observed cardinality (join support).
  `PROFILE_SUPPORTED` (support, **not** proof — see §9).
- **Taxonomy derivation** — behavior from the concept registry, with §5 propagation. `GOVERNED_TAXONOMY`
  (capped by the input concept's authority).
- **Human** — confirmations through the existing confirmation commands. `HUMAN_CONFIRMED`.

**Precedence** when producers disagree: `human-confirmed override → source-declared governed → concept-
registry derivation → deterministic parser/profile → LLM proposal → unknown`. For safety fields, take the
**most restrictive** result on disagreement.

---

## 7. Type model — three layers, not one "type"

Separate `physical_type` (a DB source: `VARCHAR(20)`), `logical_type` (representation: `numeric_string`),
and `semantic_type` (role: `identifier`). Glossary-only ingestion yields
`physical_type=None, logical_type=numeric_string, semantic_type=identifier`; a later structural source
fills `physical_type` — not necessarily a contradiction (a numeric-looking id stored as text). Never
label a prose-derived type as a database type.

---

## 8. Relationship promotion — semantic similarity is not a join

The highest-risk, lowest-precision inference. `ACCOUNT_ID` in two tables may be *different namespaces*
(customer's account vs counterparty's), same name — a confident LLM join here becomes a silent leakage
vector the whole PIT gauntlet then trusts. Promotion uses an **evidence matrix**, never model confidence:

```python
@dataclass(frozen=True)
class JoinEvidence:
    semantic_match: bool; identifier_namespace_match: bool | None; physical_fk_present: bool
    profile_overlap_score: float | None; observed_cardinality: str | None
    declared_cardinality: str | None; temporal_compatibility: str | None
```

| Evidence | Result |
|---|---|
| LLM semantic similarity only | Proposal (not usable) |
| Matching name/entity only | Proposal |
| LLM + identifier-namespace match | Strong proposal |
| LLM + profile overlap/cardinality | Profile-supported proposal (review candidate) |
| Declared FK / approved catalog join | Structurally confirmed (usable) |
| Human approval after reviewing evidence | Human-confirmed (usable) |

The **default banking posture** requires source or human authority to *use* a join; profile-supported
joins are usable only behind an explicit, per-project policy for low-risk exploration. Proposals flow
through the existing `approved_join` lifecycle (§3): `OVERLAY_FACT_PROPOSED` → evidence attached →
`OVERLAY_FACT_CONFIRMED` (dual-owner). Confirm the existing envelope expresses endpoints/direction/
cardinality/namespace/evidence/effective-time/supersession — it largely does via `ApprovedJoinRef`;
extend it rather than forking a new authority system.

---

## 9. Profiling is support, not proof

"Unique in the current sample" does not prove a durable business key; "95% value overlap" does not prove
the business relationship. Profiling emits `PROFILE_SUPPORTED`, never auto-`STRUCTURALLY_ATTESTED`.

---

## 10. Hierarchical LLM batching

- **Pass A — column semantics** (SHIPPED, the merged batching engine): concept, logical type, roles,
  sensitivity *proposal*, normalized definition/domain — 20–40 columns per call, focused retry.
- **Pass B — table synthesis** (one table per call for wide tables): primary entity, grain candidate,
  grain key candidates, event-vs-snapshot, time columns, measure/dimension/identifier columns.
- **Pass C — relationship synthesis**: deterministically **block** candidates first (shared identifier
  concept/namespace, entity match, BIAN/FIBO adjacency, related-terms, name/def similarity), then propose
  joins with the §8 evidence. Hard caps (candidate is O(tables²)): `max_join_candidates_per_table`,
  `max_relationship_pairs_per_run`, `max_llm_relationship_batches`; emit
  `relationship_search_truncated=true` when a cap is hit — never claim full coverage silently.
- **Pass D — catalog reconciliation** (over table-level summaries, not raw columns): surface
  contradictions, disconnected islands, unresolved entities, review priorities. Produces a **review
  worklist only — no graph-writing authority.**

Everything is deterministically validated before it can be promoted; unknown values remain unresolved or
go to review — never dynamically invented.

---

## 11. Re-upload merge policy

A re-upload may refresh source-declared values, regenerate LLM proposals, and invalidate stale evidence,
but MUST NOT silently downgrade `HUMAN_CONFIRMED` → `LLM_INFERRED` (generalize `entity_suggestion`'s
re-apply). If a new source conflicts with a confirmed value, create a **review item** — overwrite
neither. `build_graph` order: (1) build from current source, (2) apply taxonomy derivations (with §5
propagation), (3) apply current LLM proposals, (4) re-apply confirmed overrides, (5) most-restrictive
safety merge.

---

## 12. Conflict resolution — contradiction vs different layers

Distinguish `physical=VARCHAR` / `logical=numeric_string` (compatible) from `physical=TIMESTAMP` /
`llm_logical=monetary_amount` (true conflict) via a deterministic `TYPE_COMPATIBILITY` table. For physical
properties the structural source wins operationally, but the conflict stays **visible** as a review signal.

---

## 13. Calibration — a program, and it mostly governs the soft fields

Per-field reliability requires a labeled gold set per field, re-measured per model/prompt version, with
enough samples per confidence bucket to be meaningful — sustained human labeling, not a function. Scope it
honestly: for **safety-critical** fields the LLM is already barred from sole authority, so the *authority
gate* does the protecting and the confidence number is secondary. Calibration earns its keep on
**advisory/soft** fields, where you decide whether an `LLM_PROPOSED` value is reliable enough to
auto-promote without review. Store `model_reported_confidence` and `calibrated_reliability` separately;
never promote on model-reported confidence alone. Reuse the batched-enrichment gold-set harness; add
per-field gates (e.g. sensitivity: zero false-negative on the critical set; join: zero false-confirmed;
grain: zero incorrect auto-promotions). The gate controls **promotion**, not proposal generation.

---

## 14. HITL is a prioritized queue

A system that emits 10,000 proposals into a flat queue has failed; human confirmation bandwidth — not LLM
cost — is the real constraint. Prioritize by `risk × feature-unlock value × evidence strength × reuse`.
Suggested order: (1) potential sensitivity false-negatives, (2) grain/as-of blocking many recipes, (3)
joins unlocking many high-value recipes, (4) identifier-namespace conflicts, (5) low-value normalization.
Show the unlock ("confirming this join enables 14 churn recipes + 8 early-warning recipes") so a reviewer
has a concrete reason to spend attention. The `FeatureReadiness` profile drives this.

```python
@dataclass(frozen=True)
class FeatureReadiness:
    semantic_completeness: float; structural_completeness: float; temporal_completeness: float
    safety_completeness: float; relationship_completeness: float
    blockers: tuple[str, ...]; warnings: tuple[str, ...]   # each proposal tagged proposed|confirmed
```

---

## 15. Field policy matrix (goes into the plan verbatim)

| Field | LLM may propose | Aids search now | May gate feature-gen | Min load-bearing authority |
|---|---|---|---|---|
| definition | yes | yes | not directly | LLM allowed |
| domain | yes | yes | advisory routing only | LLM allowed |
| concept | yes | yes | yes (registry-validated) | validated controlled concept |
| feature_role | yes | yes | ranking only | LLM allowed |
| logical_type | yes | yes | limited | deterministic corroboration or source |
| additivity | fallback only | yes | yes | concept registry (auth-propagated) or confirmation |
| temporal_role | fallback only | yes | yes | concept registry / source / human |
| sensitivity | yes, conservative | yes | yes | source / taxonomy / human; most-restrictive |
| leakage_anchor | proposal only | yes | yes | governed taxonomy / human |
| entity | yes | yes | soft until confirmed | source / human / profile-supported policy |
| grain | yes | display only | no | structural or human confirmation |
| as_of / availability | yes | display only | no | structural or human confirmation |
| join | yes | display only | no | approved structural or human confirmation |
| cardinality | yes | display only | no | structural / profile-supported + confirmation |

---

## 16. Sequencing — ship a thin vertical slice first

Do NOT build all layers before shipping. Phases, each independently valuable:

1. **Phase 1 — semantic front door (first shippable, prove on FTR):** glossary reader → Pass A column
   enrichment (concept + **deterministic sample-value logical/semantic type**) writing `FieldEvidence`
   with authority; taxonomy-derived behavior with §5 propagation; a `FeatureReadiness` profile; LLM join
   *proposals routed into the existing `approved_join` PROPOSED state, none auto-promoted*. Label the
   source "semantics-vouched, structure-incomplete."
2. **Phase 2 — field authority registry + resolver:** the `FieldPolicy` table, the two-output resolver,
   re-upload merge, conflict detection.
3. **Phase 3 — structure & relationships:** structural-source pairing (DB introspection / OpenMetadata /
   DDL) matched by FQN; Pass B table synthesis; Pass C relationship synthesis + profiling leg + caps.
4. **Phase 4 — reconciliation, calibration, prioritized HITL:** Pass D worklist; per-field gold gates;
   the prioritized review queue with feature-unlock analysis.

A structural source (even one) is the single input that turns the riskiest proposals (joins, grain,
physical types) from LLM-guessed into attested — its availability materially changes Phase-3 effort.

---

## 17. What stays invariant

The deterministic ingestion backbone — validation gate, large-change brake, event-sourced facts, drift,
quarantine, graph rebuild — is untouched. Enrichment/evidence remains advisory and fail-soft: a producer
failure degrades the graph, never a fact. Governed egress + audit on every LLM call. The graph is a
rebuildable projection; the `events`/`llm_call`/evidence stores are the truth.

## 18. Open dependencies
- Availability of a structural source for the target tables (governs Phase 3).
- Extending the `approved_join` envelope if it can't already carry every relationship dimension (§8).
- Reconciling the ungoverned declared-`joins_to` shortcut with the governed `approved_join` path (§3).
- Provenance storage: a `graph_field_evidence` side table (object_ref, field, effective_value_hash,
  evidence_id, authority, status) keeping `graph_node`/`graph_edge` flat for query speed.
