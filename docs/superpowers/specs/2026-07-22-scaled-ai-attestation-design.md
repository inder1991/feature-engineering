# Scaled AI Attestation — two-layer enrichment, calibrated auto-attest, rule-grained human governance

Date: 2026-07-22 · Status: design v2 (revised after adversarial review) · Diagrams:
pipeline https://claude.ai/code/artifact/7da1cc53-45d4-426c-afcb-eff2042c3f2a ·
workbench https://claude.ai/code/artifact/9bcf322f-233b-4d72-a525-cda20de8ec9c

> **v2 note.** A three-lens adversarial review (architect / trust / scope) read v1 against the code and
> found the central integration claim false and the phasing inverted. This version relocates the gate,
> excludes the safety fields, inverts to a shadow-first phasing, and adds learning-loop rails. §11 maps
> each change to its finding.

## 1. Problem

The catalog AI-proposes column metadata (concept, definition, domain, sensitivity, additivity, temporal
role, leakage anchor). Every AI output is filed as an **unconfirmed hint** whose only path to "trusted"
is a human confirming it, one column at a time — which cannot scale to a bank's ~150,000 columns.

Observed on the live FTR ingest (126 columns) this session:
- The asset-detail read model **already emits** an evidence layer (`asset_detail._evidence_section`
  queries `field_evidence` for producer/strength/proposed_value/confidence_band). The defect is that
  the **frontend leads with the empty `effective_metadata`/decision layer** and renders a value with a
  known author (source-attested definition, LLM-proposed concept) as **"unattested"**.
- The **Readiness tab shows ~400 `unresolved_authority` blockers** — every column × {sensitivity,
  additivity, temporal_role, leakage_anchor} lacks an accepted authority.
- The **Relationships tab** leads with a 125-row containment dump (`unknown` types) and hides the
  candidate layer (the 42 D4 semantic-binding candidates, pending joins).

**Core failure:** AI has no scalable path from "proposed" to an authority the catalog trusts, and the
human unit of work is the column, not the convention.

## 2. Principle

**AI attests at scale; humans govern conventions, not columns.** Human effort tracks the number of
distinct **conventions** in the bank (bounded — hundreds), not the number of columns. Extends the coded
"LLM = proposer / governance = disposer" stance to "**LLM attests the advisory bulk; the deterministic
taxonomy, the source, and humans remain the only authorities for safety.**"

## 3. Architecture

Async on a worker. Five units, each independently testable.

### 3.1 Layer 1 — Proposer (extend existing)
Reuses `enrich.py` / `enrich_batch.py`, extended to propose the fields feature-gen needs
(demand-driven). Unchanged egress guard, vocab-cached prompts, accept gates.

### 3.2 Layer 2 — Triangulation Validator (NEW), with a grounding-coverage floor
Three signals, but **decorrelation is gated on grounding, not assumed**:
1. **Independent re-classification** — a **different model family** classifies the column blind; compare.
2. **Deterministic grounding** — non-AI cross-checks: observed `operational_type`, the file's attested
   `bian_path`/`fibo_path`/`business_term`, cross-field/sibling consistency.
3. **Adversarial refutation** — *deferred* (§4); a model argues the proposal is wrong. Not a mandatory
   third call until re-classification + grounding are shown insufficient on the gold set (cost: it
   ~triples per-column LLM spend at 150K scale).

**Grounding-coverage rule (blocker fix).** On a column-mapping upload, `operational_type` is `unknown`
and path/term agreement is source metadata the proposer already read — so grounding can be *thin or
non-independent*. Where per-field grounding coverage is below a floor, **auto-attest is forbidden and
the field escalates**, rather than fusing two correlated LLMs. Decorrelation is proven empirically in P0
(§8), not asserted.

Output: a calibrated confidence per (column, field). Self-reported model confidence is never the gate.

### 3.3 Attestation Gate — a write-time policy + a read-time disqualifier (NOT a predicate leaf)
The authority model (`field_authority.py`) is pure set-membership — no numeric threshold, no per-column
risk. So the gate lives in two places, explicitly:
- **Write-time policy:** whether to emit an AI-attested evidence row at all — `confidence ≥ threshold`
  AND `risk tier = low` AND `grounding coverage ≥ floor` AND the field is AI-attestable (§3.4).
- **Read-time disqualifier:** usage-driven re-escalation is a **`Disqualifier`** (the model already
  supports these, honored per-read via `active_disqualifiers`) computed by C1 from **live feature
  usage** — not an un-removable evidence row. This is the only way "human attention follows regulatory
  weight" is expressible against append-only evidence.

**Blocking, not post-hoc (blocker fix).** The first time a governed feature references an AI-attested-only
column, the disqualifier makes that a **blocking precondition**: the feature cannot become
operational/eligible until the triggered human review resolves. Re-escalation *gates*, it does not merely
notify.

**Single global threshold to start** (per-domain deferred until one global false-attest number is
measured). Threshold set from the measured false-attest rate (§6); ~70% is an *observed outcome*, never a
target (§9).

### 3.4 AI-attested authority — advisory fields only; safety fields never
**The correction at the heart of v2.** Verified against the code:
- C1 `read_operational_value` grants `status="resolved"` **only** for
  `_GOVERNED_DECISION_FIELD = {additivity, logical_representation}` (`operational_facts.py:152`,
  `column_authority.py:35-38`). `sensitivity`, `temporal_role`, `leakage_anchor`, `concept` are **hint**
  fields → C1 returns `not_operational` regardless of evidence strength.
- `field_policies.py` encodes a deliberate invariant: **sensitivity and the behavioural fields
  (additivity/temporal_role/leakage_anchor) are never certified by an LLM alone** —
  `sensitivity = SOURCE_OR_HUMAN`; behavioural = `AnyOf(TAXONOMY/confirmed, SOURCE/attested,
  HUMAN/confirmed)`.

Therefore:
1. **AI attests only genuinely advisory fields** — `concept`, `definition`, `domain`. It **never** gets
   an accepting leaf on a safety field. The coded invariant stands.
2. **The behavioural fields clear via the taxonomy, not via AI.** A validated, high-confidence
   AI-attested **concept** seeds the existing deterministic taxonomy derivation of additivity /
   temporal_role / leakage_anchor as **`taxonomy/confirmed`** — a non-AI authority. So the readiness wall
   for those three clears through the *rulebook*, downstream of an AI-attested concept, never by AI
   certifying the safety field directly. **Open design point (P2, needs blast-radius analysis):** whether
   an ai-attested concept counts as a valid *seed* for taxonomy derivation, and the effect on
   `is_feature_eligible` — this is a resolution-core decision, not "unchanged in shape."
3. **`sensitivity` is never AI-attested and is not taxonomy-derivable.** Value-level detection (capability
   B) *proposes* it, but it stays a blocker until **source-attested or human-confirmed**. Correct for a
   bank — no AI-alone PII decisions.
4. **Evidence encoding (strength-collision fix):** reuse the existing `llm` producer with a **new,
   strictly-lower strength** (e.g. `corroborated`) ranked **below** `source/attested` and `parser/
   supported` in `_STRENGTH_RANK` — so an AI value can never tie or outrank source/human/parser evidence
   in `_select` (reusing `attested` would tie source-attested and return `_CONFLICT`). No new `ai`
   producer. Enumerate the touchpoints changed: `field_policies` advisory-field rules, `_STRENGTH_RANK`,
   `stale_source_evidence` for the new strength on re-upload.
5. **Four-eyes equivalent (governance fix):** AI-attested is a **new operational authority reached with
   zero human confirmers**, where join/table-fact/semantic-binding governance require distinct humans. It
   is subject to a **policy-signed calibration attestation** recorded per gold-set version, with an
   auditable actor stamped on every auto-attest batch. **Hard rule:** any field whose operational_rule is
   `_HUMAN_CONFIRMED` or `_SOURCE_OR_HUMAN` (i.e. sensitivity) can never be satisfied by AI alone.

### 3.5 Rule / Cluster human layer + Learning loop (NEW), with rails
Human unit of work is the **rule**, never the column. Clustered escalations → the AI **drafts a rule**
(plain English + machine predicate + column preview + flagged exceptions) → a human **approves / edits /
rejects**. Rules also seed from the bank's data dictionary and from generalizing repeated corrections.

**Safety rails (blocker fix — the loop must not amplify a bad rule):**
- Every rule-driven attestation stamps `rule_id` + `rule_version`.
- Back-fill is **staged**: a canary sample is applied and spot-checked before full application.
- **One-command revocation** stales exactly that rule's rows and re-resolves (bounded blast radius).
- **Validator firewall:** approved-rule examples update **only the Proposer (Layer 1)**, never the
  independent re-classification or grounding channels — so the check the loop is meant to pass stays
  independent of what the loop taught.

### 3.6 Escalation policy (exact triggers)
A field reaches a human iff ANY: signals diverge; confidence < threshold; **grounding coverage < floor**;
risk tier = high (intrinsic PII/leakage, or usage-driven per §3.3); resolver conflict; novel/OOD;
in the audit sample (§6); or drift re-opened it. Else it auto-attests (advisory fields only).

## 4. Further AI capabilities
- **A. Feature ideation** — the endpoint; a **thin demonstrator is pulled earlier** (§8) so the program
  shows its payoff before the full P4.
- **B. Value-level PII & quality** — proposes sensitivity/quality flags; sensitivity always needs
  source/human confirm (§3.4.3).
- **C. Plain-English "why"** per attestation — makes review fast, fixes the detail screen.
- **D/E/F — deferred** until the core gate is proven: cross-source entity resolution, unit/scale
  inference, drift + regulatory tagging.

## 5. Integration with existing code (corrected — no "unchanged in shape")
- Proposer: `overlay/upload/enrich.py`, `enrich_batch.py`.
- Evidence/authority: `overlay/field_evidence.py`, `field_authority.py` (add `corroborated` strength +
  a usage `Disqualifier`; the accepting logic is a **write-time policy**, not a predicate edit),
  `field_decision.py` (AI-attested must emit a `field_decision_event` with a value hash to be operable —
  not merely an evidence row).
- Resolution/read: `operational_facts.py` — **changes required** for an AI-attested advisory field to be
  operable, and for the concept→taxonomy cascade; explicitly *not* unchanged. Consumers `asset_detail.py`
  and `readiness.py` must be **reconciled onto one attested-ness judgment** (they diverge today:
  readiness clears on `load_bearing_value_hash`; asset_detail on C1 `_GOVERNED_DECISION_FIELD`) with a
  cross-surface consistency test.
- Human governance: a new Rules/Clusters surface beside `semantic_binding_governance.py` /
  `join_governance.py` / `table_fact_governance.py`.
- Async: refactor of `ingest_upload` (§6), reusing `runtime/worker.py`, `runtime/queue.py`, timers
  (0502), external_commands (0503).
- **New persistence (named, not implied):** migrations for rules, clusters, calibration/gold-set labels,
  confidence scores, the audit sample, and the usage→disqualifier lookup.

## 5a. UI surfaces (which screen each phase touches)
- **Asset Detail → Metadata & Evidence** (`AssetDetailScreen`): frontend leads with the evidence layer
  it already receives — "source attested / AI proposed / rulebook proposed — not confirmed" instead of
  "unattested"; the plain-English "why" (C) when present. *(P1a)*
- **Asset Detail → Relationships**: column-centric (entity/synonyms surfacing D4 candidates, joins as
  pending, feature lineage); containment demoted to a one-line link. *(P1a)*
- **Asset Detail → Readiness**: behavioural blockers clear only once the concept→taxonomy cascade lands
  (P2), sensitivity stays a blocker until source/human. *(P2)*
- **Governance → Rules & Clusters** (new tab, `GovernanceReviewScreen`): AI-drafted rules, approve/edit,
  column preview, revocation. *(P3)*
- **Governance Dashboard**: coverage, measured false-attest rate, pending rules, active disqualifiers. *(P2/P3)*
- **Workbench → Suggested features** (new, `WorkbenchScreen`): entity-grouped feature proposals; blocked
  ones honest. Mockup linked above. *(P4; thin demo earlier)*

## 5b. Read-model & screen redesign (P1a — AI-independent, ships first)
The evidence layer is **already emitted**; this is a **frontend-led** change plus promoting
`_evidence_section` into `effective_metadata`'s fallback. Principle: surface evidence → candidate →
decision, each labelled by honest authority; demote structure to a link. Metadata tab stops rendering
known-author values as "unattested"; Relationships tab redesigned per §5a. Zero AI-trust risk, delivers
value today. (`unknown` operational types are expected for a mapping upload — out of scope.)

## 6. Non-functional
- **Async worker as its own spec** — explicit transaction boundaries, idempotency keys, failure/retry,
  ingest-status surfacing, and the re-upload-vs-in-flight-enrichment concurrency contract (today
  enrichment runs inside the request's single transaction holding the source advisory lock; splitting it
  moves resolve+project+readiness async too). Preserve atomicity; not greenfield.
- **Measurement is a first-class deliverable, not a footnote.** Gold set = **stratified random sample
  across sources/domains** with confidence intervals; **re-calibrated per source** (convention drift
  means a source-1 threshold does not transfer); the regulatory artifact is a **per-source measured error
  bound**, not one FTR number generalized to 150K. State the **human labelling cost per new source** as
  part of the scaling budget; bootstrap labels from existing source-attested/taxonomy evidence where
  possible. Start with a **single global threshold**; the 1–5% continuous audit sample is a named
  ongoing cost.

## 7. Risks & failure modes
- **Correlated AI errors** → grounding-coverage floor + different-model-family re-classification;
  auto-attest forbidden where grounding is thin (the FTR mapping-file case).
- **Learning-loop amplification** → rule_id + staged canary + revocation + validator firewall (§3.5).
- **Governance regression** → policy-signed calibration attestation; safety fields excluded (§3.4.5).
- **Measurement bottleneck / unrepresentative gold set** → stratified per-source sampling with CIs (§6).
- **Trust window** → blocking usage-precondition, not post-hoc (§3.3).
- **Cost at 150K** → async, cached, refutation deferred, per-domain deferred.

## 8. Decomposition (re-phased — shadow first, irreversible last)
Each phase is its own spec→plan→impl cycle; later phases gate on earlier.

0. **P0 — shadow measurement (NEW, first, writes nothing).** Run re-classification + deterministic
   grounding against a labelled gold set over the already-ingested FTR columns; emit a **false-attest
   number** and grounding-coverage per field. Precedent: the planner shadow store (migration 0999).
   **All downstream tier/async work gates on this number clearing tolerance.**
1. **P1a — read-model / frontend honesty (AI-independent, ships now).** §5b. Fixes "everything
   unattested" and the useless Relationships tab with zero trust risk.
2. **P1b — async enrichment move (its own spec).** §6. Independent of the tier change.
3. **P2 — AI-attested tier + calibrated gate** *(gated on P0)*. The `corroborated` strength (advisory
   fields only), the write-time policy + usage disqualifier, the concept→taxonomy cascade (with
   blast-radius analysis), C1/readiness reconciliation, the four-eyes-equivalent calibration attestation,
   per-source calibration. A **thin feature-ideation demonstrator** rides here to show the endpoint.
4. **P3 — rule/cluster human layer + learning loop** with the §3.5 rails.
5. **P4 — feature ideation (full).**
6. **P5 — value-level PII/quality (B) + explanations (C).**
7. **P6 — deferred fast-follows (D/E/F).**

## 9. Success criteria
- **P0:** a measured false-attest rate + grounding coverage on the FTR gold set; go/no-go on whether the
  gate can be trusted at all — **before** any tier or async work.
- **P1a:** a known-author value renders with honest provenance (not "unattested"); Relationships tab
  column-centric.
- **P2:** measured false-attest rate below the chosen tolerance at the chosen threshold (**the only hard
  gate**); behavioural readiness blockers clear via the taxonomy cascade for concept-attested columns;
  asset_detail and readiness agree (cross-surface test). ~70% auto-attest is an *observed outcome*, not a
  target.
- **P3:** one approved rule back-fills its cluster and is revocable in one command; a same-convention
  re-upload escalates measurably fewer columns.
- **Program:** human decisions per new source trend toward the count of *new conventions*, not columns.

## 10. Out of scope / deferred
- AI attesting any safety field (sensitivity / behavioural) — always taxonomy/source/human.
- Removing the human from feature-feeding columns — always human/policy.
- Per-domain thresholds, adversarial refutation, and capabilities D/E/F — until the core gate is proven.
- Replacing the deterministic taxonomy — it remains a first-class non-AI authority.

## 11. Review response (what the adversarial review changed)
- **False C1 claim** (arch/trust/scope blockers) → §3.4: AI attests advisory fields only; behavioural
  fields clear via the taxonomy cascade; sensitivity never AI; "unchanged in shape" removed from §5.
- **Gate can't be a predicate leaf** (arch blocker) → §3.3: write-time policy + read-time `Disqualifier`;
  usage re-escalation is a disqualifier, and blocking.
- **Safety-invariant violation** (trust blocker) → §3.4.1/§10: safety fields excluded, invariant kept.
- **P1 incoherent / attests before measuring** (arch/trust/scope blockers) → §8: P0 shadow-first; the
  tier moves to P2 gated on P0's number; P1 split into AI-independent P1a + P1b.
- **Triangulation collapse on mapping files** (trust blocker) → §3.2: grounding-coverage floor,
  different-model-family, refutation deferred.
- **Learning-loop amplifier** (trust blocker) → §3.5: rule_id + canary + revocation + validator firewall.
- **Four-eyes bypass** (major) → §3.4.5: policy-signed calibration attestation; hard exclusion rule.
- **Strength collision** (major) → §3.4.4: `corroborated`, strictly lower rank; no new producer.
- **Two surfaces disagree** (major) → §5: reconcile readiness↔C1 with a cross-surface test.
- **Gold-set representativeness / burden** (major×2) → §6: stratified per-source sampling with CIs, named
  labelling budget, single global threshold to start.
- **§1/§5b overstated read-model gap** (minor) → §1/§5b: evidence layer already emitted; P1a is
  frontend-led.
- **70% Goodhart** (minor) → §9: demoted to observed outcome; false-attest tolerance is the only gate.
