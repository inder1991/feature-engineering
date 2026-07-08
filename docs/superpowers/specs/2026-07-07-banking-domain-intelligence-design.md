# Banking Domain Intelligence — Design

**Status:** draft for review · **Date:** 2026-07-07 · **Depends on:** the governed contract-flow spec
(this is the *brain* that plugs into that *skeleton*).

## 1. Problem & goal

Today the system does shallow, per-item tagging (11 flat concepts, free-text domain labels) and
**forgets everything between runs**. The rich `banking-domain-catalog.seed.json` exists but is **never
loaded**. So generation re-guesses each request from a blank prompt, with no banking knowledge, no
memory, and no regulatory awareness.

**Goal:** give the platform real banking domain intelligence — it **recognises** the banking problem,
**proposes** the known target, **generates** from proven parametric templates, **enforces** the
regulatory rules, and **improves** with use. Built on two solid foundations (a **strong vocabulary** and
**strong cases**) plus the machinery that leverages and grows them.

**Hard correction from review:** the intelligence must NOT rest on a thin static seed. Strength comes
from (a) a **solid concept vocabulary**, (b) **strong, deep use-cases**, (c) **real parametric
templates**, (d) **generalization** for the un-seeded, and (e) a **learning/curation loop**. The seed is
day-one bootstrap only.

## 2. Principles

1. **Knowledge is living + governed, not a shipped constant.** Seed → per-bank DB-backed store → local
   ratification → runtime growth → versioned/audited (§8).
2. **Vocabulary + cases are the foundation and must be SOLID** — the reasoning is only as good as its
   grounding. We invest here first (§3, §4).
3. **Templates are safe-by-construction AND a scaffold, not a cage** — parametric, groundable, PIT baked
   in; they SEED the LLM but never restrict it (the LLM always proposes beyond them; un-templated
   requests still work). Templates = priors; the LLM = adaptation. Both, always (§5, §5.1).
4. **LLM proposes; deterministic code + humans dispose.** Unchanged. Regulatory/leakage stay
   deterministic; the LLM never validates predictiveness (no data plane).
5. **Banking-scoped but open** — any *banking* request works (seeded or generalized); non-banking is
   rejected.

---

## 3. Foundation 1 — the concept vocabulary (make it SOLID)

> **The full, comprehensive lists** (≈50 concepts, ≈30+ entities, all 14 domains at universal-bank
> scale) live in the companion **[Banking Taxonomy Reference](2026-07-07-banking-taxonomy-reference.md)**.
> This section states the *design* of the vocabulary; that doc is the *content*.

Replace the flat 11 with a **structured banking concept ontology**: richer concepts, each carrying its
**behaviour** (additivity, point-in-time role, sensitivity/regulatory class, entity link) and related by
**is-a** edges so reasoning can generalise.

### 3.1 The concepts (proposed, grouped)

**Monetary — split stock vs. flow (the correctness fix):**
| Concept | Behaviour it carries | Example |
|---|---|---|
| `monetary_stock` | **semi-additive** (sum across entities, *latest* across time) | `balance`, `exposure` |
| `monetary_flow` | **fully additive** (sum across time AND entities) | `transaction_amount`, `fee`, `payment` |
| `monetary_rate` | **non-additive** | `interest_rate`, `apr` |
| `ratio` | **non-additive** | `utilization`, `loan_to_value` |

**Identifiers → entities:** `customer_id → Customer`, `account_id → Account`, `transaction_id →
Transaction`, `application_id → Application`, `product_id → Product`, `counterparty_id → Counterparty`.
(Carry the **join-key + grain + entity-link** role.)

**Temporal — point-in-time critical:** `as_of_date` (decision reference), `effective_date` (state
start), `event_timestamp` (generic), `duration_tenure` (derived time-delta: `days_since`, `account_age`).

**Quantities:** `count` (`num_transactions`); `score_probability` (`credit_score`, `risk_score`) —
**flagged leakage-risk** when it's a model output.

**Categorical:** `category_code` (`product_type`, `status`); `geographic` (`zip`, `region`) — **flagged
fair-lending proxy risk**; `boolean_flag` (`is_active`, `has_overdraft`) — often a label.

**Sensitive / labels:** `pii` (`email`, `ssn`); `protected_attribute` (`age`, `gender`, `ethnicity`) —
**regulatory-blocked** for credit/pricing; `outcome_label` (`churned`, `defaulted`, `is_fraud`) — **this
IS a target; the leakage anchor**; `free_text`.

### 3.2 Why structured beats flat
- **Behaviour attached to the concept** drives real checks: `monetary_stock` → *never sum across time*;
  `protected_attribute` → *block for credit*; `outcome_label` → *this is a target, watch leakage*.
- **is-a edges** let it generalise: *"recency signals predict churn"* applies to every
  `duration_tenure`; two `monetary_stock` columns are recognised as the same kind.
- **Entity links** power joins, grain, and cross-catalog matching.

### 3.3 How it stays solid
Seeded from this list; **enriched by curation + learning** (§7) — new concepts and is-a edges are added
as the catalog grows. The classifier maps a column to the *nearest* concept and flags `unclassified`
for human curation (never silently mislabels).

---

## 4. Foundation 2 — the banking case catalog (make it STRONG)

The seed's 15 name-only cases are day-one bootstrap. Strength comes from **per-case depth** + **breadth
that grows**, not a fixed list.

### 4.1 What a STRONG case entry contains
```
use_case, domain, entity, status
target:            { name, definition, source_columns_pattern }   ← precise, feeds §5-leakage
feature_templates: [ <parametric template refs> ]                 ← REAL templates (§5), not names
allowed_data_classes / blocked_data_classes                        ← regulatory data rules
regulatory:        { fair_lending, adverse_action, aml, mrm_tier }
risk_tier, primary_metric, explainability_mode (symbolic|template)
owner, compliance_confirmed, version
```
Every field is a *proposed default to be ratified* by the bank's domain owner + Compliance (§8) — the
vendor never ships authority.

### 4.2 Breadth: seeded + generalized + grown
- **Seeded:** the 15 known cases (churn, credit_origination, aml_txn_monitoring, card_fraud, …) get the
  fast path.
- **Generalized (§6):** a *new banking* request outside the 15 still works — the LLM + ontology +
  parametric templates handle it; it's then **onboarded** into the catalog (owner ratifies), never
  rejected.
- **Grown (§7):** the flywheel + curation continuously deepen existing cases (better templates, learned
  target refinements) and add new ones. The living catalog quickly exceeds the seed.

### 4.3 Depth per domain (the real work)
Each of the five domains (marketing, retail_credit, pricing, financial_crime, fraud) needs its cases
filled to real depth — precise targets, complete parametric template sets, ratified regulatory rules.
This is **expert-curated content**, seeded by the vendor and completed per-bank. It is the single
biggest content investment and the main determinant of out-of-box strength.

---

## 5. Foundation 3 — parametric feature templates (the real power)

A template must be a **groundable, safe-by-construction definition**, never a name.

```
template rolling_balance_trend:
  intent:        "recent trajectory of a balance"
  computes:      slope of {stock_col} over the trailing {window} days
  requires:      a monetary_stock column + an as_of_date + an {entity} grain
  parameters:    window ∈ {30, 60, 90}
  produces:      one value per {entity} per as_of
  point_in_time: uses only rows dated BEFORE as_of        ← leakage-safety BAKED IN
  additivity:    n/a (a trend)
  explainability: high (monotone, inspectable)             ← for symbolic_synthesis use-cases
```

**Why this is the power:** the system can **ground** `{stock_col}`/`{entity}` to real catalog columns,
run the gauntlet, and generate confidently — and the template *cannot* produce a leaky feature because
the point-in-time constraint is part of its definition. Template generation for interpretable use-cases
(credit/pricing) is the *symbolic/parametric synthesis* mode; template-only for the rest.

### 5.1 Templates are a SCAFFOLD, not a CAGE (non-negotiable)

The failure mode to avoid: templates become the *only* path → the system degrades to a rigid lookup
table that's stuck on any un-templated request. That would make it **less** intelligent, not more. So:

- **Seed, don't restrict.** Templates give the LLM a head-start ("known churn patterns"); the LLM may
  always **propose beyond them**.
- **Parametric = flexible.** A template is a *family* (blanks flex to the catalog), never one frozen
  feature.
- **The LLM adapts / combines / invents** — tweak a template, merge two, or use one as inspiration for a
  novel variant.
- **Generalization catches the rest.** A hypothesis matching no template still works via the from-scratch
  LLM path (which exists today); only a **non-banking** request is refused.
- **The library grows** — good novel patterns become templates (curation + flywheel); the cookbook is
  never frozen.

**Why this makes it smarter, not dumber:** intelligence = **priors + adaptation**, like an expert who
knows the patterns *and* adapts them — not a junior improvising from nothing. Templates are the priors;
the LLM is the adaptation. Both, always. **Templates must never be the only path.**

### 5.2 Selection pipeline — TWO candidate sources → one safety gate → LLM ranks → human finalises

Candidates come from **two first-class sources**; **both** pass the *same* deterministic safety gate
before a human sees them. **Safety is deterministic; coverage + relevance are the LLM's; the choice is
the human's.**

- **Source 1 — Templates (priors):** the use-case's `feature_templates` + cross-cutting families,
  grounded to *this* catalog by concept-matching (§3).
- **Source 2 — LLM novel proposals (creativity/adaptation, first-class — NOT a footnote):** the LLM
  proposes features **beyond any template**, from the hypothesis + the catalog's actual columns + its
  banking knowledge. This is how the **long tail** works (un-templated use-cases, clever
  situation-specific combinations, features tailored to *this bank's* columns) and what keeps the system
  from being a rigid cookbook. Templates cover the *known well*; the LLM covers *everything else + adapts*.

The pipeline:
1. **Generate from BOTH sources** — grounded templates ∪ LLM novel proposals.
2. **Groundability (deterministic).** Every candidate must bind to real, concept-matched columns; drop
   the ungroundable. (Templates bind `{params}`; a novel proposal must reference columns that exist.)
3. **Safety + eligibility (deterministic).** The four checks + `blocked_data_classes` on **every**
   candidate → the **SAFE set** (templates AND novel, uniformly filtered — a novel feature is *never*
   trusted to be safe just because the LLM proposed it).
4. **LLM shortlist (proposes).** From the safe set, pick the **2–3 most relevant** to *this* hypothesis,
   **each with a rationale**; flywheel-informed.
5. **Human finalises (disposes).** Picks from the shortlist; can **expand to the full safe set**
   (scaffold-not-cage — never hide valid options).

**The boundary (non-negotiable): the LLM PROPOSES + RANKS, but never GATES.** Its novel proposals *and*
its shortlist pass the *same* deterministic groundability + safety gate as templates — so the LLM affects
**coverage and relevance**, never **safety**. A novel LLM feature that leaks or binds a protected
attribute is dropped exactly like a template would be. Rationale recorded in the contract provenance;
**fail-safe** to the deterministic ranking if the LLM call fails (the flow never blocks on the LLM).

---

## 6. Reasoning — wired into the contract flow

The five stages plug into the governed flow at defined points:

| Contract-flow step | Domain-intelligence reasoning |
|---|---|
| **Gate 1 — brief** | **recognise** the use-case; **propose the known target** (grounded); banking-scope guard |
| **Generation** | **seed from parametric templates** (§5); **regulatory filter** (block/flag protected-attribute etc.); **ontology** reasoning (dedup, generalise) |
| **Gate 2 — approve** | **reuse/memory** (don't rebuild an existing feature); **capture** the approve/reject signal |

Un-seeded requests fall through to **generalization**: the LLM proposes features grounded on the
ontology + generic parametric patterns; a **non-banking** request (no banking entity/concept) is
rejected.

> **The use-case is a SAFETY input — human-confirmed + fail-to-strict (arch fix #3).** Recognition is an
> LLM *proposal*, and a wrong one applies the wrong regulatory rules (churn's laxity to a **credit**
> model → protected attributes not blocked). So the recognised use-case is **shown for human
> confirmation** at Gate 1, and when recognition is **uncertain/ambiguous the system defaults to the
> STRICTEST applicable rules** (treat as regulated) until confirmed — never the laxest. The LLM never
> sets a regulatory posture unchecked.

## 7. Learning + curation — the flywheel (gets stronger with use)

- **Capture every human decision** at Gate 2 (approve/reject + why). This is recorded today and ignored;
  persist it as a training signal per use-case + per team.
- **Steer future generation** toward what this org actually approves; **demote** repeatedly-rejected
  patterns.
- **Grow the foundations:** learned target refinements, new concepts/is-a edges, new use-cases — proposed
  by the system, **ratified by a curator** (LLM proposes, human disposes, even for the knowledge base).
- **Curation surface:** a governed admin view to edit the vocabulary + cases + templates (versioned,
  audited). The knowledge base is a *living product artifact*, owned by the bank's domain team.

## 8. Production lifecycle of the knowledge base

**Not** a static file shipped in the wheel (like migrations). Governed reference data:

1. **Ship the seed** as a starting template.
2. **Load into a per-deployment, versioned, DB-backed store** on setup.
3. **Ratify locally — and fail CLOSED until then (arch fix #4).** The bank's domain/risk owner confirms
   domain facts (target, templates, allowed data); **Compliance confirms policy facts** (blocked data,
   regulatory flags); `compliance_confirmed` flips true only then (a vendor default is never authority).
   **But un-ratified must NOT mean un-enforced:** until ratified, the system treats the use-case
   **conservatively** — it **blocks protected/sensitive data classes by default** and marks any feature
   `NEEDS_REVIEW` — so the (possibly months-long) pre-ratification window fails **closed / safe**, not
   open. *(v1 said "not enforced until ratified," which left protected attributes usable during that
   window — the wrong default.)*
4. **Grow at runtime** — onboard new banking use-cases with sign-off.
5. **Version + audit** every change (it drives what data is blocked — a governance event).

## 9. Non-goals

- No data plane / compute / predictiveness (unchanged) — templates are *definitions*, not runtime jobs.
- **The vendor does not ship a complete or authoritative banking brain** — impossible to enumerate and a
  cross-jurisdiction liability. Strength = living knowledge + generalization, not a perfect seed.
- No auto-enforced regulatory rules before local ratification (§8.3).

## 10. Build phases (dependency order)

1. **Solid vocabulary** (§3) — the structured concept ontology + behaviour + entity links; migrate the
   classifier to it; curation surface for it.
2. **Parametric templates** (§5) — the template model + a first real set for the seeded domains, safe-by-
   construction, groundable.
3. **Strong case catalog** (§4) + **lifecycle** (§8) — DB-backed governed store, per-case depth,
   ratification, onboarding.
4. **Reasoning wired into the flow** (§6) — use-case recognition, known-target proposal, template-seeded
   generation, regulatory filter.
5. **Learning + curation flywheel** (§7).

## 11. Open decisions

- **A — vocabulary is a fixed schema vs. curator-extensible at runtime.** *Rec: seeded schema, curator-
  extensible with ratification* (so a bank can add concepts without a code release).
- **B — templates authored as data (DB/JSON) vs. code.** *Rec: data (versioned, curatable), executed by
  a small deterministic template engine — so the domain team edits templates without a deploy.*
- **C — how much per-case depth ships in the seed vs. left to per-bank curation.** *Rec: ship strong
  defaults for the 15 seeded cases; everything is ratifiable/editable; breadth grows via onboarding.*
