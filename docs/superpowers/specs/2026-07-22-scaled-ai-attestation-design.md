# Scaled AI Attestation — two-layer enrichment, calibrated auto-attest, rule-grained human governance

Date: 2026-07-22 · Status: design approved, pre-implementation · Diagram: https://claude.ai/code/artifact/7da1cc53-45d4-426c-afcb-eff2042c3f2a

## 1. Problem

The catalog enriches uploaded columns with AI-proposed metadata (concept, definition, domain,
sensitivity, additivity, entity, temporal role). Today every AI output is filed as an **unconfirmed
hint** with exactly one path to "trusted": a human confirms it. Observed consequences on the live FTR
ingest (126 columns) this session:

- The asset-detail screen shows **everything "unattested"** — `_effective_metadata_section`
  (`asset_detail.py`) derives provenance only from the governed-decision layer (`read_operational_value`,
  C1), which is empty because nothing is confirmed; the populated evidence layer (`field_evidence`:
  127 source-attested definitions, 110 LLM concepts, 110 taxonomy-proposed sensitivity/temporal/leakage)
  is never surfaced.
- The readiness tab shows a **wall of ~400 `unresolved_authority` blockers** (`readiness.py`) — every
  column × {sensitivity, additivity, temporal_role, leakage_anchor} lacks an accepted authority.
- Nothing is feature-ready because nothing is attested, and a human confirming per-field cannot scale
  to a bank's ~150,000 columns.

**Core failure:** AI attestation is not a first-class authority. There is no scalable path from
"AI proposed" to "trusted", and the human unit of work is the column, not the convention.

## 2. Principle

**AI attests at scale; humans govern conventions, not columns.** The AI carries the low-risk bulk;
humans and the deterministic taxonomy stand behind the small part that carries regulatory weight.
Human effort is proportional to the number of distinct **conventions** in the bank (bounded — hundreds
of concepts, dozens of naming standards), not the number of columns.

This extends the existing "LLM = proposer / governance = disposer" stance to
"**LLM attests at scale, human governs the exceptions**".

## 3. Architecture

Five units, each independently understandable and testable. Async on a worker (two AI passes cannot sit
inside an upload request — a single pass already nears the 300s request ceiling).

```
Layer 1 Propose ─▶ Layer 2 Triangulate ─▶ Gate (confidence × risk) ─┬─▶ Auto-attest (~70%)
                                                                     ├─▶ Cluster & Rule (AI drafts → human approves)
                                                                     └─▶ Human sign-off (high-risk / feature-feeding)
        ▲                                                                        │
        └──────────────────────── Learning loop (rules back-fill + retrain) ◀────┘
```

### 3.1 Layer 1 — Proposer (extend existing)
Reuses `enrich.py` / `enrich_batch.py`. Extended so the fields feature generation *requires* (grain,
as-of, additivity, entity, join key, sensitivity) are proposed, **demand-driven**: the feature layer
declares the required fields; the proposer fills those specific holes with evidence. No change to the
egress guard, caching (now vocab-cached), or the accept gates.

### 3.2 Layer 2 — Triangulation Validator (NEW)
Not "a second model agrees" (correlated models rubber-stamp shared hallucinations). Three **independent**
signals converge into one score:

1. **Independent re-classification** — a second model classifies the column *blind* (own concept /
   sensitivity / additivity), then we compare. Decorrelated by a different prompt framing and/or model.
2. **Deterministic grounding** — non-AI cross-checks against recorded evidence: does the proposed
   concept's implied type match the parser's observed `operational_type`? Does it agree with the file's
   attested `bian_path` / `fibo_path` / `business_term` (already in `field_evidence`)? Cross-field and
   sibling-column consistency.
3. **Adversarial refutation** — a model argues the strongest case the proposal is *wrong*; refutation
   surfaces errors agreement hides.

Output: a **calibrated confidence** per (column, field), calibrated against a human-labelled gold set
(§6). Self-reported model confidence is not used as the gate.

### 3.3 Attestation Gate — confidence × risk
- **Auto-attest (~70%, a dial):** confidence ≥ threshold AND risk tier = low → write an `ai/attested`
  evidence row (§3.4). Threshold set from the measured false-attest rate on the gold set; can run
  tighter per domain (money-/risk-adjacent stricter).
- **Cluster & rule (~30%):** everything below threshold or diverging is grouped by pattern (name shape,
  proposed concept, disagreement kind) → §3.5.
- **Human sign-off (bounded):** high-risk always, regardless of confidence.

**Risk tiering** is both **intrinsic** (PII/restricted `sensitivity`, `leakage_anchor`) and
**usage-driven**: a low-risk auto-attested column **re-escalates** the moment a governed feature
references it — human attention follows real regulatory weight instead of being spent upfront.

### 3.4 The `ai/attested` authority tier (NEW)
Slots into the existing `(producer, strength)` evidence model (`field_evidence.py`) and predicate model
(`field_authority.py`: `HasEvidence`, `AnyOf`, `InfluenceTier`). A new producer `ai` with strength
`attested`, carrying the validation provenance (the three signals + calibrated confidence + gold-set
version). A field's authority predicate gains `HasEvidence(ai, attested)` as an accepting leaf **gated
by confidence ≥ threshold AND low risk**. Effect: `read_operational_value` (C1) resolves ai-attested
values as governed-for-low-risk, so `asset_detail` and `readiness` show them as attested with real
provenance — directly fixing the "everything unattested" screen and the readiness wall.

### 3.5 Rule / Cluster human layer + Learning loop (NEW)
The human unit of work is the **rule**, never the column.
- Escalated columns are **clustered** by pattern; the AI **drafts a rule** per cluster in plain English
  and a machine predicate, with a **preview of every column it would touch** and flagged exceptions
  (e.g. `name ~ *_amt AND type = numeric → concept=monetary_flow, additive`).
- A human **approves / edits / rejects** the rule (never writes logic). One decision covers thousands.
- Rules also seed from the bank's **existing data dictionary / naming standards**, and from
  **generalization** — repeated identical per-column corrections trigger "make this a rule?".
- **Learning loop:** an approved rule (a) **back-fills** every matching pending column immediately and
  (b) **updates the AI** (few-shot examples / rule library injected into Layer 1 and the validator), so
  the first big source teaches the conventions and later sources barely escalate.

### 3.6 Escalation policy (exact triggers)
A field reaches a human iff ANY of: signals diverge (re-classification mismatch, or contradiction with
grounding/source evidence); calibrated confidence < threshold; risk tier = high (intrinsic or
usage-driven); the resolver found an irreconcilable conflict; novel / out-of-distribution (proposed
value not in vocabulary, low grounding coverage); the field is in the continuous **audit sample**
(1–5% of auto-attested, statistical QA on gate calibration); or drift re-opened it. Everything else
auto-attests.

## 4. Further AI capabilities (on the same rails)

Ranked; the top three are must-adds. Each new proposal flows through the same triangulation + gate.

- **A. Feature ideation (must-add) — the endpoint.** Propose candidate features from attested metadata
  ("avg transaction value 30d", velocity, dormancy), each with recipe, columns used, and leakage/temporal
  safety pre-checked. Closes the loop from metadata to features.
- **B. Value-level PII & quality (must-add).** Scan *sample values* (not just names) for hidden PII
  (card numbers in `notes`, Emirates ID in `ref`) and quality issues (sentinels, dummy columns, type
  mismatches, all-null / near-constant). Feeds `sensitivity` and quality flags. (Value access honors
  read-scope + egress guard.)
- **C. Plain-English "why" (must-add).** One-line justification per attestation ("monetary_flow because:
  numeric, name matches amount, BIAN path = Payments.Amount, sibling currency present"). Makes review a
  5-second read and the trail audit-ready; fixes the confusing detail screen.
- **D. Cross-source entity & synonym resolution.** Recognise `cust_nm`/`customer_name`/`cif_id` as one
  customer; propose joins that let features span catalogs. Extends D4 semantic bindings.
- **E. Unit / currency / scale inference.** Fils vs dirhams, scaling factors, so feature math is correct.
- **F. Drift monitoring + regulatory tagging.** Re-open only attestations whose meaning shifted on
  re-upload; map columns to BCBS 239 critical data elements & AML typologies.

## 5. Integration with existing code
- Proposer: `overlay/upload/enrich.py`, `enrich_batch.py` (already vocab-cached this session).
- Evidence / authority: `overlay/field_evidence.py` (add `ai` producer / `attested` strength),
  `overlay/field_authority.py` (add gated `HasEvidence(ai, attested)` leaf), `overlay/field_decision.py`.
- Resolution / read: `overlay/upload/operational_facts.py` (C1 recognises ai-attested), consumed by
  `asset_detail.py` and `readiness.py` unchanged in shape.
- Human governance: new rule surface alongside the existing `semantic_binding_governance.py` /
  `join_governance.py` / `table_fact_governance.py`; cluster/rule review screen alongside
  `GovernanceReviewScreen`.
- Async: a worker stage (the codebase has `outbox` / `queue` / `timers` / dispatch-ledger tables) —
  move enrichment + validation off the request path.

## 5a. UI surfaces (which screen each phase touches)
Mostly existing screens; two are new.

- **Asset Detail → Metadata & Evidence tab** (`AssetDetailScreen`): the `ai/attested` badge with
  confidence + the plain-English "why" (capability C) replaces today's "unattested". Surfaces the
  evidence layer, not just the (empty) decision layer. *(P1, then P2/P5)*
- **Asset Detail → Readiness tab**: `unresolved_authority` blockers clear as fields become AI-attested. *(P1)*
- **Governance → Rules & Clusters review** (NEW tab in `GovernanceReviewScreen`, beside join /
  table-fact / semantic-binding): AI-drafted rules and clustered escalations, each with a column
  preview and Approve / Edit / Reject — the bulk human governance layer. *(P3)*
- **Governance Dashboard** (`GovernanceDashboardScreen`): attestation coverage — % auto-attested, %
  escalated, measured false-attest rate, pending rules. *(P2/P3)*
- **Workbench → Suggested features** (NEW surface in `WorkbenchScreen`): AI-proposed features grouped
  by **entity** (not column), each a card with its recipe, leakage + time-safety checks, relevance,
  and Accept / Edit / Dismiss. Blocked proposals (missing a cross-source input) shown honestly. A
  "feeds these features" cross-link on a column's Asset Detail deep-links here.
  Mockup: https://claude.ai/code/artifact/9bcf322f-233b-4d72-a525-cda20de8ec9c *(P4)*

## 6. Non-functional
- **Async worker**, cost-governed (per-column budget, sampling low-value columns, prompt caching).
- **Measurement first:** a human-labelled **gold set** (a few hundred columns) sets and monitors the
  auto-attest threshold via measured **false-attest rate**; the claim "AI attestation is trustworthy" is
  earned with a number. Gate calibration is re-checked continuously via the audit sample (§3.6).
- **Auditability:** every ai-attested value carries its three validation signals, confidence, gold-set
  version, and (capability C) a plain-English justification — defensible to a regulator for low-risk;
  high-risk always carries a human/policy signature.

## 7. Risks & failure modes
- **Correlated AI errors** → mitigated by grounding (non-AI) + adversarial refutation + independent
  (not confirmatory) re-classification; never "two models agreed".
- **Miscalibrated confidence** → gate uses gold-set-calibrated score, not self-reported.
- **Prompt injection via data** (a crafted definition steering the validator) → grounding is
  deterministic; value-scanning is sandboxed under the egress guard; refutation is adversarial.
- **Cost at 150K scale** → async, batched, cached, sampled; feature-usage-driven risk keeps the
  must-review set small.
- **Regulatory defensibility** → low-risk AI-attested is documented + explained; load-bearing (feature-
  feeding) columns always require human/policy sign-off.

## 8. Decomposition (this is a program, not one plan)
Each phase is an independent spec → plan → implementation cycle; later phases gate on earlier.

1. **P1 — AI-attested authority tier + async enrichment.** The `ai/attested` producer, C1 recognition,
   asset-detail/readiness surfacing, move enrichment to a worker. Unblocks the "everything unattested"
   symptom. Gold set v0 + a fixed conservative threshold (no calibration yet).
2. **P2 — Triangulation Validator + calibrated gate.** The three signals, fusion, gold-set calibration,
   the confidence × risk gate, the audit sample. Turns the fixed threshold into a measured one.
3. **P3 — Rule/Cluster human layer + learning loop.** Clustering, AI-drafted rules, approve/edit surface,
   back-fill + retrain, data-dictionary seed, generalization-from-corrections.
4. **P4 — Feature ideation (capability A).** The endpoint.
5. **P5 — Value-level PII/quality (B) + explanation narratives (C).** Can parallel P4.
6. **P6 — Fast-follows: cross-source resolution (D), unit/scale (E), drift + regulatory tagging (F).**

## 9. Success criteria
- P1: an auto-attested (fixed-threshold) FTR column renders `ai/attested` with provenance on the detail
  screen; readiness blockers for those fields clear.
- P2: measured false-attest rate on the gold set is below the chosen tolerance at the chosen threshold;
  ~70% of columns auto-attest on the FTR file.
- P3: a single approved rule back-fills its whole matching cluster; a re-upload of a
  same-convention source escalates measurably fewer columns than the first.
- Program: human decisions per new source trend toward the count of *new conventions*, not columns.

## 10. Out of scope / deferred
- Fully removing the human from load-bearing (feature-feeding) columns — always human/policy by design.
- Non-Anthropic providers; the model split (which model for re-classification vs refutation) is a P2
  tuning detail, not a design commitment here.
- Replacing the deterministic taxonomy — it remains a first-class, non-AI authority.
