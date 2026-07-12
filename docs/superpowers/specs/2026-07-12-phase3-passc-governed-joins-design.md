# Phase 3 / Pass C — Governed Relationship Discovery (CSV-only) — Design (v2)

**Status:** design, review round 2 (v1 review folded in). Ready for review → implementation plan.
**Goal:** Deterministically discover *join candidates* between catalog columns from the uploaded glossary alone, file each as a governed `approved_join` **proposal**, let a human confirm it, and project a confirmed join into an operational graph edge — so table-to-table relationships become known, trustworthy, and traversable by the feature planner. No OpenMetadata, no profiling, no LLM-authored joins.

**The one principle to protect above all:**
> Pass C may *discover and explain* candidate relationships, but the only operational join path is **`approved_join VERIFIED → operational graph edge → feature-planner traversal`**.

---

## 1. Context & the problem

Phase 1 gave us **meaning**; Phase 2 lets a human confirm **per-table facts** (grain, availability). Phase 3 is about **relationships between tables** — the one place the LLM must stay out of the authority loop. A wrong grain is recoverable; a **wrong join is a leakage bomb** (wrong customer linkage, many-to-many explosion, point-in-time leakage, PII propagation, false aggregation). A join can never be *guessed* — it needs **explainable, reproducible evidence** plus **human confirmation**.

**Constraint (decided):** built from **only the uploaded glossary CSV** — no live OpenMetadata, no data profiling. Profiling / declared FKs from a structural source become *additional evidence producers* in a later phase; out of scope here.

### 1.1 What the FTR CSV actually gives (verified against the 127-row sample)

Columns: `schema.table.column`, `term_name`, `description_business_definition`, `data_domain`, `term_type`, `related_terms`, `synonyms_aliases`, `bian_level_1..4`, `fibo_level_1`, business-process levels. Empirically:

- **`term_type`** ∈ {Business Term (81), Dimension (27), Code Value (9), Measure (6), Reference Data (3), Regulatory Term (1)} — a usable structural hint: `Measure` → never a key; `Dimension`/`Reference Data`/`Code Value` → key-eligible when the name is id-like.
- **`related_terms` is a *broad* relation, not a key link.** It points a column at its parent table concept (`TRAN_DATE → "Financial Transaction Record"`) or a table at business processes, and is **blank on the actual identifier columns** (`FORACID`, `CIF_ID`, `TRAN_ID`, `REF_NUM`). It means "associated with," not "same identifier."
- **`bian_level_4` is coarse and namespace-mixing.** 6 leaves / 127 rows; 14 columns share **`"Customer and Counterparty Identification"`** — one leaf covering *both* customer and counterparty ids. Same-leaf ≠ same key.
- **The precise identifier concept lives in `term_name`.** `CIF_ID` = "Customer Information File Identifier" vs `FORACID` = "Customer Account Number" — same BIAN leaf, **different keys**. This is the real discriminator.

**Design consequence:** the primary same-key signal is the **normalized identifier concept (from `term_name` + column name + synonyms)**; BIAN/FIBO are **coarse priors, never sufficient**; `related_terms` is **gated** (fires only when it links two id-like columns by concept); a **mixed BIAN leaf defaults the namespace to AMBIGUOUS**; and the **namespace-compatibility gate** (§7) does the heavy lifting.

## 2. Core invariants

1. **Deterministic-first.** The authoritative candidate stream is rules + scoring over glossary signals — never an LLM. (Exploration mode — flagged, off by default — may add a separate, clearly-labelled `LLM_SUGGESTED`, review-only stream; §12.)
2. **The LLM is monotonic toward caution.** It may only explain / adjudicate a weak signal / flag a namespace mismatch / suggest a bridge — it can *lower a score or annotate*, never raise a score, mint a candidate, or promote to operational.
3. **No operational join without confirmation.** A candidate is a proposal; only human confirmation makes an `approved_join` `VERIFIED`, and only a `VERIFIED` join projects to an operational edge. `provenance ≠ authority; confidence ≠ permission.`
4. **Governed source of truth.** In governed mode, the feature planner's operational join traversal requires a `VERIFIED approved_join`. The pre-existing permissive shared-entity traversal is demoted to candidate-only (§13.1).
5. **Everything explains itself and is versioned** (§10, §19).

## 3. Scope & sequencing

**In scope — the done-line:**
```
candidate discovered (deterministic)  → scored, namespace-typed, self-explaining evidence
  → [3B, optional] LLM challenger annotates / demotes
  → approved_join PROPOSED (governed)
  → human confirms/rejects (direction + cardinality)
  → approved_join VERIFIED
  → projected to an operational graph edge (find_join_path traverses)
  → relationship readiness = confirmed
```

**Implementation sequencing (decided — v1-review Issue 4):**
- **Phase 3A (the load-bearing spine):** deterministic blocker/scorer → governed proposal → human confirmation → reverse projection → relationship readiness + the `find_join_path` proof. **No LLM client required.**
- **Phase 3B (additive):** the LLM challenger/explainer (demotion/bridge notes, bounded audited egress) and exploration mode.

Both ship in this one design; the *plan* builds 3A first and proves the join lifecycle before adding 3B.

**Out of scope (deliberate):** end-to-end feature construction (recipe stream); OpenMetadata/structural-connector evidence, profiling, cross-provider fusion, F4 relaxation, real ownership registry; join calibration / any threshold auto-promotion (Phase 4); composite-key discovery (§17); self-joins (§18, default off).

## 4. Reuse map — the join governance spine already exists (~85%)

Verified in code. Pass C reuses these **verbatim**; it adds a candidate producer in front and a projector behind.

| Capability | Status | Home |
|---|---|---|
| `approved_join` fact + value schema (`from_ref`/`to_ref`/ordered `column_pairs`/cardinality; composite supported) | REUSE | `overlay/facts.py:79-106`; `ApprovedJoinRef`/`ColumnPair` `identity.py:24`; `fact_key` sorts pairs as units |
| Propose a governed join (validates, F4/consistency, mints evidence, one gate task per owner side) | REUSE | `overlay/proposal_commands.py:34-161` — Pass C calls the same `propose_fact` with a candidate-evidence payload |
| Dual-owner confirmation lifecycle (`PARTIALLY_CONFIRMED→VERIFIED`, four-eyes, side coverage, referent-gap, expiry) | REUSE | `overlay/join_confirmation.py:58-183`; `confirmation_commands.py:47-181` |
| Per-side owner resolution (governance queue if unknown; one task per side) | REUSE | `overlay/authority.py:92-137` |
| Join drift / expiry / re-verify (both sides staled on referent change) | REUSE | `overlay/expiry.py:125`; `catalog_changes.py:230`; `reverify_tasks.py` |
| Feature-gen consumes **only VERIFIED** joins, fail-closed | REUSE | `overlay/resolve.py:183-308`; `overlay/join_path.py:38,53` (`authority='operational'` only) |
| Governed joins_to seam (display-only vs operational edge; env-gated) | REUSE | `overlay/upload/graph.py:16,24`; `OVERLAY_GOVERNED_JOINS` |
| Id-like detector + entity tagging (human-confirmed, survives re-upload) | REUSE | `entity.py:112` `_is_id_like`; `:93` `suggest_entity` / `:177` `apply_entity_suggestion` |
| Entity-relationship registry + directed grain-pair lookup | REUSE | `entity_registry.py:41`; `entity_relationships.py` |
| `ApprovedJoinRef` builder + fail-soft per-item dispatch loop | EXTEND | `graph.py:66` `governed_join_proposal`; `ingest.py:136-179` `_propose_governed_joins` — Pass C is a sibling that adds the evidence arg |

## 5. New surface

1. **Deterministic candidate blocker + scorer** (NEW, pure) — §6/§7.
2. **Candidate evidence + persistence** (NEW) — §10.
3. **Dedupe / re-ingest lifecycle** (NEW) — §11.
4. **Propose wiring** (NEW glue, reuse `propose_fact`) — §13.
5. **Reverse projector** (NEW, small) — §14.
6. **Relationship readiness** (EXTEND Phase-2 readiness) — §16.
7. **LLM challenger** (NEW, Phase 3B, advisory) — §12.

## 6. The deterministic candidate model

**Hard gates** (a pair is considered only if *all* hold):
- both columns are **identifier-like** (`_is_id_like` AND `term_type ∉ {Measure}` AND concept/semantic-type is identifier/reference);
- **namespace compatibility ∈ {COMPATIBLE, POSSIBLE}** (§7);
- **neither is a measure/date/free-text/sensitivity-only field** (negative filter below);
- **not the same table** (self-join only when explicitly enabled, §18).

**Negative filters (hard `NEVER` a key, even on exact name match).** Disqualified if concept / semantic-type / `term_type`=`Measure` maps to any of: `amount, balance, rate, date, timestamp, description, name, status, free_text, address, phone, email, currency, flag, score`. (`CUST_NAME ↔ CUST_NAME` is not a join.)

**Positive signals (weighted v1 defaults — configurable, §19). Order reflects the FTR finding:**

| Signal | Score | Note |
|---|--:|---|
| Same normalized **identifier concept** (from `term_name`+name+synonyms) | **+40** | the real same-key signal |
| Same curated `related_terms` link **that ties two id-like columns by concept** | +50 | gated (§8); rarely fires in FTR |
| Same canonical **column name** | +30 | e.g. `FORACID↔FORACID` |
| Same **`term_name`** (normalized) | +25 | |
| Confirmed **entity tag** match (`entity.py`) | +25 | high-confidence when present |
| Same **BIAN leaf** (coarse prior) | +10 | never sufficient; capped by namespace status |
| Same **FIBO leaf** | +10 | |
| Compatible **Phase-2 table entities** | +15 | |
| One side has **confirmed grain** (directional support) | +10 | |
| Compatible source / `data_domain` / process hierarchy | +10 | |

**Suppression:**

| Condition | Score |
|---|--:|
| Non-identifier semantic type / negative-filter field | **−100** |
| Namespace **INCOMPATIBLE** (incl. LLM-challenger mismatch flag) | **−50** |
| Namespace **AMBIGUOUS** (e.g. a mixed BIAN leaf, generic `reference_id` with no concept) | −40 |
| Both sides are grains of *different* entities with no bridge semantics | −30 |

**Buckets:** `≥ 80` → **strong** (propose); `50–79` → **weak** (readiness diagnostic, no proposal by default; §15); `< 50` → **suppress** (telemetry only). A candidate whose namespace is only **POSSIBLE** is **capped at weak** unless reinforced by a confirmed entity tag or a gated `related_terms` link.

**Every candidate self-explains** — a `SignalEvidence` list + the namespace status + a human sentence (§10). The explanation is worth more to the reviewer than the number.

## 7. Namespace compatibility (the load-bearing gate)

Because "same-looking id, different namespace" is the hardest banking bug (`account_id`=customer vs counterparty; `party_id`=customer vs beneficiary; the FTR leaf *"Customer and Counterparty Identification"* itself), namespace compatibility is an explicit, auditable status — **necessary but not sufficient**, and *not provable from the CSV*.

```python
class NamespaceCompatibility(StrEnum):
    COMPATIBLE   = "compatible"    # same confirmed entity tag, OR same normalized identifier concept + reinforcement
    POSSIBLE     = "possible"      # id-like + same specific concept, but no entity-tag/related_terms reinforcement
    AMBIGUOUS    = "ambiguous"     # mixed BIAN leaf, generic reference_id w/o concept, or conflicting signals
    INCOMPATIBLE = "incompatible"  # different confirmed entities, or an LLM-challenger namespace-mismatch flag
```

**Entity is column-level, not table-level.** An id column carries a `column_entity` (its identifier namespace) distinct from its table's `table_entity` — `transactions.customer_id → customer.customer_id` has *different table entities* (transaction vs customer) but the *same `column_entity`* (customer). Namespace compatibility keys on **`column_entity`**; a different `table_entity` alone is **not** incompatible.

Deterministic derivation (from the CSV alone): **COMPATIBLE** = same `column_entity`, OR same normalized identifier concept **+ a corroborator (same canonical column name, synonyms, or a gated `related_terms` key-link)** — safe because a COMPATIBLE candidate is only ever *proposed*; the **dual-human confirm gate is the namespace safety net** for a wrong same-name/same-concept pair. **POSSIBLE** = same identifier concept with a *different* column name and no corroborator (reachable). **AMBIGUOUS** = same coarse BIAN leaf only / a "mixed" leaf (customer+counterparty) / a generic `reference_id` with no distinguishing concept. **INCOMPATIBLE** = different `column_entity`, or (3B) a challenger mismatch flag.

Gate/bucket interaction: gate admits `COMPATIBLE|POSSIBLE`; `COMPATIBLE` is strong-eligible; `POSSIBLE` caps at weak unless reinforced; `AMBIGUOUS` → reviewer-only/suppress (−40); `INCOMPATIBLE` → suppress (−50).

**Reason codes** on every candidate (auditable): `same_confirmed_entity`, `same_identifier_concept`, `related_terms_link`, `same_bian_leaf_only`, `mixed_bian_leaf`, `generic_reference_without_context`, `counterparty_namespace_detected`, `different_confirmed_entity`.

## 8. `related_terms` & synonym semantics

The FTR data shows `related_terms` is a broad relation (parent/associated), not a key link, and blank on ids. Treatment (v1-review Issue 2):
- `related_terms` scores as a **strong** signal **only** when **both columns are identifier-like AND the related-term text normalizes to the other column's `term_name`/name/concept**. A bare `related_terms` overlap (e.g. two columns both related to "Financial Transaction Record") is **supporting, not sufficient** (and usually blocked because one side isn't id-like).
- `synonyms_aliases` feed the **normalized identifier concept** (e.g. `CIF_ID`↔`CIF`), not a standalone signal.
- If a future glossary encodes typed relations (`same_as_identifier`, `foreign_key_to`, `alias_of`, `business_related_to`, `parent_child`), Pass C consumes the type directly; absent a type (FTR), it uses the normalize-and-require-id-like rule above.

## 9. Direction & cardinality

Inferred from **Phase-2 grain**, with an explicit status and full case coverage (v1-review Issue 5):

```python
class CardinalityInferenceStatus(StrEnum):
    INFERRED_FROM_CONFIRMED_GRAIN = "inferred_from_confirmed_grain"
    MISSING_GRAIN                 = "missing_grain"
    AMBIGUOUS_BOTH_GRAINS         = "ambiguous_both_grains"
    MANY_TO_MANY_RISK             = "many_to_many_risk"
```

| Left grain? | Right grain? | Proposed | Status |
|---|---|---|---|
| no | yes | `left → right` N:1 | `INFERRED_FROM_CONFIRMED_GRAIN` |
| yes | no | `right → left` N:1 (1:N stated) | `INFERRED_FROM_CONFIRMED_GRAIN` |
| yes | yes | 1:1 / dimension-dimension — **caution** | `AMBIGUOUS_BOTH_GRAINS` |
| no | no | many-to-many risk / bridge needed | `MANY_TO_MANY_RISK` (confidence lowered, gap surfaced) |
| unknown | known | lower confidence | `MISSING_GRAIN` on the unknown side |

**Only `INFERRED_FROM_CONFIRMED_GRAIN` is proposable.** A join value *requires* a `1:1|1:N|N:1` cardinality, and two unique columns are not necessarily 1:1 business-equivalent (`account.account_id`≠`card.card_id`). So **both-grain (`AMBIGUOUS_BOTH_GRAINS`, would be 1:1) and neither-grain (`MANY_TO_MANY_RISK`) are forced to `weak` — a readiness diagnostic, never an `approved_join` proposal.** Only a one-side-confirmed-grain candidate (an inferable `N:1`) is proposed. A **missing grain is visible** in the worklist and recorded as a **separate** confirmation (grain via the Phase-2 grain lifecycle; join via the `approved_join` lifecycle). The proposed value always **explains what it assumed**.

**Correction path (v1-review Issue 13; corrected in v2 after code review).** For `approved_join`, **the entire value — endpoints, `column_pairs`, AND `cardinality` — participates in the `fact_key`** (`identity.py:71-81`), and the dual-owner confirm path derives the confirmed value from the proposal and ignores an `args['value']` override. So there is **no confirm-time value override** for a join (unlike a Phase-2 grain, whose overridable field is *not* in its key). Any correction — wrong cardinality, direction, or endpoints — is **reject-with-reason → re-propose the corrected candidate** (a new `fact_key`). `_confirm_join`'s cardinality argument is used only to reconstruct the *identical* proposed value for confirmation, never as an edit.

## 10. Candidate evidence payload (attached to every proposal)

A reviewer must confirm/reject without reverse-engineering the score. Every proposal carries:

```python
@dataclass(frozen=True)
class SignalEvidence:
    signal_name: str            # e.g. "same_identifier_concept"
    score_delta: int
    evidence_refs: tuple[str, ...]   # e.g. ("...txn.cif_id", "...customer.cif_id")
    explanation: str

@dataclass(frozen=True)
class JoinCandidateEvidenceV1:
    candidate_id: str
    from_ref: str
    to_ref: str
    column_pairs: tuple[ColumnPair, ...]
    proposed_direction: str | None
    proposed_cardinality: str | None
    cardinality_status: CardinalityInferenceStatus
    bucket: Literal["strong", "weak", "suppressed"]
    score: int
    positive_signals: tuple[SignalEvidence, ...]
    negative_signals: tuple[SignalEvidence, ...]
    namespace_compatibility: NamespaceCompatibility
    namespace_reason_codes: tuple[str, ...]
    grain_evidence: tuple[str, ...]
    missing_requirements: tuple[str, ...]        # e.g. ("grain: customer.cif_id",)
    llm_annotations: tuple[str, ...]             # 3B only; demotions/notes
    explanation: str                             # the human "proposed because…"
    producer: Literal["deterministic_pass_c"]
    config_version: str
    candidate_algorithm_version: str
    source_snapshot_id: str
```

**Durable storage (two homes, by purpose):** (1) every candidate of *every* bucket is written to a durable **`pass_c_candidate_evidence`** ledger (keyed by the unordered column-ref pair) — this is the home for weak-candidate persistence, re-ingest dedup (prior fingerprint/bucket/namespace), and audit. (2) For a *proposed* candidate, the evidence also rides the reuse-verbatim **`evidence_ref`** channel (a pre-minted `write_evidence` row with the breakdown in `metric_values`), so `get_task_proposal` surfaces score / reason codes / explanation to the reviewer with no change to the propose/read spine. The closed `approved_join` value schema (`additionalProperties:false`) means the evidence cannot ride `proposed_value`.

## 11. Candidate fingerprint, dedupe & re-ingest lifecycle

Pass C runs at ingest; re-ingesting the same glossary must **not** create duplicate proposals. Fingerprint = `(source, from_ref, to_ref, ordered column_pairs, candidate_algorithm_version)`. Per run, keyed on the current folded state of the candidate's `approved_join` fact:

| Existing state | Same candidate (same fingerprint) | Different candidate (same pair, new direction/pairs/evidence) |
|---|---|---|
| none | **propose** | propose |
| PROPOSED (`DRAFT`) | update evidence in place / skip if identical | **conflict → review item** |
| VERIFIED | **skip** | conflict if contradictory (do not silently supersede) |
| REJECTED | do **not** reopen unless evidence *materially* changed (score bucket or namespace status changed) | may propose |
| STALE / REVERIFY / EXPIRED | allow re-proposal | may propose |
| display-only raw edge (from a declared `joins_to`) | attach candidate evidence; **do not** operationalize (only confirmation does) | — |

Reuses Phase-2's folded-state discipline (skip-quiet on active/VERIFIED; let `propose_fact` adjudicate sticky-rejected). "Materially changed" = a change in `bucket` or `namespace_compatibility`, not a cosmetic score delta.

## 12. The LLM challenger contract (Phase 3B, advisory, monotonic toward caution)

On the bounded deterministic candidate set only, the LLM may produce **annotations on existing candidates** — never new operational facts:
1. **Explain** the deterministic evidence.
2. **Adjudicate a weak signal** (`same/different identifier / bridge needed / insufficient`) — result stays `LLM_PROPOSED`.
3. **Namespace mismatch (challenge)** → sets `NamespaceCompatibility=INCOMPATIBLE` (demotion `−50`).
4. **Bridge suggestion** ("needs `account → customer` bridge") → demotes the direct candidate + a reviewer note. *Building* a bridge is a reviewer action / future.

**Guarantee:** an LLM annotation can only *lower a score, add a caution, or explain* — never raise, create, or promote. Egress is bounded (only the surfaced candidate set, metadata-only, via the existing audited enrich seam) so the mass-column-dump anti-pattern is structurally impossible.

**Flags:** `OVERLAY_PASS_C` (feature, default 0) · `OVERLAY_PASS_C_LLM_CHALLENGER` (default 0) · `OVERLAY_PASS_C_EXPLORATION` (default 0). Deterministic candidate generation runs with **no LLM client**; the challenger/exploration modes are strictly additive. Exploration mode may surface `LLM_SUGGESTED` weak candidates to a review-only worklist, never operational.

## 13. Confirmation & authority

### 13.1 Governed source of truth (v1-review Issue 9 — decided)

In **governed mode** (`OVERLAY_PASS_C` on), the feature planner's **operational** join traversal requires a `VERIFIED approved_join`. The pre-existing permissive shared-entity traversal (`entity.py` `cross_join_via_entity` / runtime `EntityBridge`) is **demoted to candidate-only** — it may *feed* Pass C candidates but is **not itself operational**. With the flag **off**, current behaviour is preserved byte-for-byte (transition safety). The plan wires the feature-planner's governed-mode filter and treats the permissive path's retirement as the §12.1 governed-joins retirement milestone.

### 13.2 CSV-only confirmation mode (v1-review Issue 10 — corrected in v2 after code review)

**An `approved_join` with both owners unknown is DUAL, not single-confirmer.** `owner_of → None` for both endpoints ⇒ `resolve_authority` sets `same_owner = False` ⇒ **`Authority.dual = True`** and `governance_queue = True` (`authority.py:118-124` — the docstring is explicit: "both-unknown is still dual (two distinct governance approvals)"). So the join opens **two side-labelled platform-admin gate tasks** and requires **two DISTINCT platform-admin confirmations**: the first appends `PARTIALLY_CONFIRMED`, a second *distinct* platform-admin appends `VERIFIED` (`join_confirmation.py:97-120`). A single confirmer can never reach VERIFIED. Labelled honestly:
```
owner_resolution   = unavailable
confirmation_mode  = governance_fallback_dual_confirmer   (two distinct platform-admins)
four_eyes          = service proposer ≠ each confirmer, AND the two confirmers ≠ each other
```
This is stronger, not weaker, than single-confirmer — two-party accountability holds even under the governance fallback. When a real ownership registry supplies distinct `owner_of`, the same path routes to the two real table stewards with no change.

## 14. Reverse projection — close the loop (idempotent, fail-closed)

A `VERIFIED approved_join` projects to an `authority='operational'` `joins` edge; **anything else demotes it**. The projector is **clear-then-apply idempotent** (like Phase 2's grain projector), not upgrade-only.

**Promotion:** on `VERIFIED`, set the pair's `joins` edge to `operational` with links back to the fact.
**Demotion triggers (all):** `approved_join` `REJECTED` / `EXPIRED` / `STALE` (referent drift) / superseded by a different join / an endpoint column removed / an endpoint identity AMBIGUOUS. Any of these → the edge reverts to `display_only`.

**Edge fields (new — mirror migration 0984's node decision-links):** `authority`, `approved_join_fact_key`, `approved_join_event_id`, `approved_join_status`, `authority_updated_at`. Operational traversal filters, flag-off-safe (a declared edge has a NULL link and still traverses):
```
kind='joins' AND (approved_join_fact_key IS NULL OR approved_join_status='VERIFIED')
```
**Edges are COLUMN-keyed** (`from_ref`/`to_ref` = `public.table.column`), so multiple joins between one table pair (on different columns) coexist; the projector and conflict logic operate on the **unordered COLUMN-ref pair**, not the table pair. **Orientation-safe:** the confirmed (grain-derived) direction may differ from a declared display edge — the projector **deletes any edge for that column pair in either orientation and writes exactly one operational row** in the confirmed direction (never a duplicate); demotion clears **both** orientations. **Scope-safe:** endpoints render in the **public** graph scope (`public.t.c`), matching `graph_node.object_ref`, not the `src::public.…` evidence form. **Declared-spare:** the projector only ever demotes edges whose `approved_join_fact_key IS NOT NULL`, so a file-declared edge is byte-for-byte untouched (the flag-off guarantee). **Async demotion:** a reject/expiry taking a join out of VERIFIED demotes its linked edge immediately (not only at next ingest).

## 15. Weak / suppressed candidate behaviour (v1-review Issue 12)

- **strong (≥80)** → propose an `approved_join` (governed queue).
- **weak (50–79)** → **persist as a candidate/readiness diagnostic; NO proposal** by default (keeps the governed queue from flooding). Surfaced in the relationship-readiness view as "possible relationships, unconfirmed."
- **suppressed (<50)** → **telemetry/counters only** (reason codes), not persisted as candidates.
- **exploration mode** → `weak + LLM_SUGGESTED` may go to a **review-only** worklist, clearly labelled, never operational without explicit promotion.

## 16. Relationship readiness

Extend the Phase-2 readiness machinery with a per-table **relationships** dimension: `no_candidates / candidate_proposed / weak_candidates_only / confirmed / conflicting`, cause-labelled. This is the **done-line proof**: after a human confirms a join, readiness flips to `confirmed` and `find_join_path` traverses it.

## 17. Composite joins — explicitly deferred (v1-review Issue 6)

`ApprovedJoinRef` supports composite `column_pairs`, but **Phase 3A candidate generation is single-column-pair only.** Composite-key discovery (`country_code + customer_id`, `source_system + account_id`, `as_of_date + id`) is deferred to the structural/profiling phase (where inclusion-dependency evidence can validate a composite). The `approved_join` schema's composite support is preserved untouched. (A future heuristic — "multiple strong id candidates on one table pair → possible composite, review-only" — is noted, not built.)

## 18. Self-joins — deferred, default off (v1-review Issue 15)

Same-table joins (`employee.manager_id → employee.employee_id`, `account.parent_account_id → account.account_id`) are **excluded by default**; the hard gate has an explicit `allow_self_join` switch (off) for a future need.

## 19. Versioning (v1-review Issue 14)

Every `JoinCandidateEvidenceV1` carries `config_version` (the scoring-weights/threshold config) and `candidate_algorithm_version` (the blocker/scorer logic). Replays and audits explain why old candidates differ from new ones; calibration (Phase 4) bumps the config version, not code.

## 20. Data flow (end to end)

```
ingest_upload (glossary) — flag OVERLAY_PASS_C, default OFF
  Pass A/B run as today
  ─ Pass C (3A) ─────────────────────────────────────────
  1. block:   identifier-like pairs; namespace ∈ {COMPATIBLE,POSSIBLE}; negative filters
  2. score:   signals → score + bucket + namespace status + self-explanation
  3. persist: strong → propose_fact(approved_join, evidence=JoinCandidateEvidenceV1)  [fail-soft, savepointed, deduped §11]
              weak   → readiness diagnostic (no proposal);   suppressed → telemetry
  4. [3B, opt] LLM challenger: explain/adjudicate/namespace-flag/bridge  (demote-only)
  5. readiness: relationships = candidate_proposed / weak_candidates_only
  ───────────────────────────────────────────────────────
(later, async — TWO distinct platform-admins, governance fallback)
  6. confirm: PROPOSED → PARTIALLY_CONFIRMED (admin #1) → VERIFIED (distinct admin #2)
             (any correction — cardinality/direction/endpoints — is reject → re-propose; no in-place edit)
  7. project: VERIFIED → operational edge (declared-spare, unordered-pair, public-scope; + fact link);
             non-VERIFIED / async reject / expiry → demote to display_only (async hook + at ingest)
  8. readiness: relationships = confirmed;  find_join_path traverses it
```

## 21. Module / file structure

- `overlay/upload/passc/candidates.py` — blocker + scorer + namespace classifier (pure; no I/O/LLM). `block(...)→[Candidate]`, `score(...)→JoinCandidateEvidenceV1`.
- `overlay/upload/passc/lifecycle.py` — fingerprint + dedupe/re-ingest state machine (§11).
- `overlay/upload/passc/propose.py` — build `ApprovedJoinRef`, `propose_fact` with evidence; fail-soft/adapter-gated/counters.
- `overlay/upload/passc/projection.py` — reverse projector (clear-then-apply, promote/demote, fact link).
- `overlay/upload/passc/challenger.py` — Phase 3B LLM challenger (advisory, bounded egress, default-off).
- `overlay/upload/readiness.py` — EXTEND: relationships dimension.
- `overlay/upload/ingest.py` — wire Pass C at the governed-joins seam (behind `OVERLAY_PASS_C`, savepointed fail-soft).
- migrations — `graph_edge` fact/decision-link columns (§14).
- config — one object holding weights/thresholds/negative-filter list/buckets (v1 defaults; `config_version`).
- tests under `tests/featuregen/overlay/upload/passc/`.

## 22. Testing

- **Blocker/scorer (pure):** negative filters suppress (`Measure`/date/`CUST_NAME` never candidate even on exact name match — word-boundary match so `"Mandate Reference"` is *not* tripped by `"date"`); same identifier concept **+ an independent corroborator (synonyms / gated `related_terms`)** → COMPATIBLE/strong-eligible, same concept **alone** → POSSIBLE (capped at weak); same BIAN leaf **alone → AMBIGUOUS → excluded from proposals** (surfaced only as a weak/telemetry diagnostic, never a scored strong candidate); a **mixed leaf → AMBIGUOUS**; a **neither-grain (`MANY_TO_MANY_RISK`) candidate is forced to weak** (never proposed); `related_terms` fires strong **only** when it links two id-like columns by concept; self-join excluded; every candidate carries a non-empty explanation + reason codes.
- **Dedupe/lifecycle:** re-ingest of an identical glossary creates **no** duplicate proposal; a rejected candidate does **not** reopen unless bucket/namespace changed; a VERIFIED candidate is skipped.
- **Challenger (3B):** a namespace-mismatch flag demotes; a bridge suggestion demotes + annotates; the LLM can never raise a score or mint a fact (assert monotonicity); egress bounded/metadata-only.
- **Propose:** strong → `approved_join` PROPOSED (not confirmed); evidence carries the breakdown; fail-soft; default-OFF byte-for-byte.
- **Confirm + project (integration) — the authority proof (v1-review Issue 11), both directions:**
  - strong candidate proposed → a display edge may exist → `find_join_path` does **not** traverse it (fail-closed);
  - human confirms → projector upgrades the edge → `find_join_path` **traverses** it → readiness = `confirmed`;
  - fact expires/reverify-stale/rejected → projector **demotes** the edge → `find_join_path` **no longer** traverses.
- **Grain interaction:** grain-missing lowers confidence + surfaces the gap; join-only vs grain+join are separate confirmations; cardinality-status cases (§9) covered.
- **Governed source of truth:** with the flag on, the permissive shared-entity path is not operational; with the flag off, current feature-gen behaviour is unchanged.

## 23. Out of scope / deferred / open

- **Structural evidence** (OM structural-connector facts, profiling/value-overlap, composite FKs, cross-provider fusion, F4 relaxation, real ownership registry): later phases; the lifecycle absorbs them as new producers without rework.
- **Feature construction / recipe realization:** separate stream.
- **Join calibration / auto-promotion:** Phase 4.
- **Composite & self-joins:** §17/§18.
- **Bridge tables:** the LLM may *suggest* one and demote a direct candidate; *building* a bridge relationship is future.
