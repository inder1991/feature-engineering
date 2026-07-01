# SP-2 — Intake + Clarification + Human Gate #1: Design Spec

**Status:** Design (sub-project spec)
**Date:** 2026-07-01
**Sub-project:** SP-2 (Phase B — Vertical slice / MVP, pipeline Layers 1–2)
**Parent:** [Reference architecture §3 Layers 1–2](./2026-06-27-feature-engineering-platform-design.md) · [Roadmap §4 SP-2](./2026-06-27-feature-engineering-platform-roadmap.md) · builds on [SP-0](./2026-06-27-sp0-foundations-design.md) · reads [SP-1](./2026-06-29-sp1-metadata-overlay-design.md)
**Type:** Vendor-neutral design + a clearly-marked sample-stack appendix

> Implements **Layer 1 (Intake and normalization)** and **Layer 2 (Contract control and human
> clarification, Human Gate #1)** of the platform (design §3:88–106). SP-2 turns a data scientist's
> plain-English intent into a **Confirmed Feature Contract** — the first executable-eligible artifact —
> through an **auditable LLM** that *structures and suggests*, deterministic checks that *validate and
> enforce*, a human who *confirms business meaning*, and the registry that *governs*. It is the **first
> sub-project that invokes an LLM**, and it establishes the platform's permanent **auditable-LLM surface**.
> SP-2 **builds on SP-0 only** (event store, staged-document DAG, durable runtime, human-gate tasks,
> identity/authz + structural SoD, audit) and *reads* SP-1's merged-view metadata for normalization.
>
> **The hard invariant this whole sub-project exists to protect:** *no Confirmed Feature Contract → no
> mapping, no compilation, no execution* (design §3:106). A contract is never executable until CONFIRMED.

---

## 1. Purpose and scope

### 1.1 In scope

- The **Feature Contract content-schema** — SP-2 owns the *semantics* of the Draft and Confirmed contract
  (entity/grain, observation intent, calculation method, windows, filters, target definition), the
  **Assumption Ledger**, and per-field **ambiguity + confidence scores**; SP-0 owns the *envelope* and the
  generic Draft schema (§2.1, §4).
- **Layer 1 — Intake & Normalization:** the `submit_intent` command → **LLM Intake & Normalization Agent** →
  a **Draft Feature Contract** (`status: NEEDS_CLARIFICATION`, never executable) + **Assumption Ledger**
  (§5). Classifies each intent against the read-only **`BankingDomainCatalog`** (§4.5, §5.4) —
  rejecting/parking out-of-scope (**`OUT_OF_SCOPE`**) and prohibited-class (**`PROHIBITED_DATA_CLASS`**)
  intents (each stamping the catalog version) and routing sensitive-proxy / ambiguous ones to clarification.
- The **two intake modes** — *definition-driven translation* (built end-to-end) and *hypothesis-driven
  generation* (real flow; deliberately dumb single-call generator stub) (§3).
- **Layer 2 — Contract control & clarification:** per-field **ambiguity + confidence scoring**, the
  **Doubt Router** (auto-resolve vs must-ask-human), the **Critique Service `CONTRACT_REVIEW` mode**, the
  **Human Clarification Gate**, the **Contract Refinement Loop**, and **Minimum Contract Validation** (§6).
- The **`CandidateGenerator` seam** — the interface, candidate schema, and Gate #1 scored-candidate-selection
  UX, with a **stub single-call generator** for SP-2 (§7).
- **Human Gate #1** — author-self-confirm as an *audited intent lock*, with `requires_independent_validation`
  risk-flagging and **prohibited-intent blocking** (§8).
- The **`LLMClient` interface + `FakeLLM` + a config-gated real Claude adapter + the auditable-LLM
  envelope** — event-sourced call records, structured-output contract with bounded repair → fail-into-
  clarification, no-silent-fallback, no-PII-to-LLM boundary enforcement (§9).

### 1.2 Out of scope (deferred — with the receiving sub-project)

- **The real hypothesis-generation engine** — the Strategy Router, feature-strategy specialists, attempt +
  conceptual memory, symbolic/scorecard synthesis, few-shot proposer, diversity/islands (design §14.6–14.9)
  → **SP-12**. SP-2 ships only the seam + a single-call stub and **must not import SP-12 scope** (§7).
- **All data grounding** — mapping business concepts to concrete allowed columns, point-in-time / SCD
  binding, the Catalog Quality Gate → **SP-3**. SP-2 *reads* catalog/overlay metadata for normalization and
  ambiguity only; it never does policy-aware mapping (§4.4, §10).
- **Independent validation / registration approval (Human Gate #2)**, four-eyes signer, MRM validation →
  **SP-5**. SP-2 only *sets* `requires_independent_validation` on the contract; it does not run the gate (§8.4).
- **The reusable multi-mode Critique Service** (all five modes, one service) → **SP-8**. SP-2 owns the single
  `CONTRACT_REVIEW` mode only (§6.4).
- **Candidate *scoring* by predictive power** (IV/WoE, overfitting guard) → **SP-5/SP-7**. SP-2's stub attaches
  only cheap, model-free *plausibility/quality* signals to candidates; there is no ground-truth score yet (§7.3).
- **The full Domain / Use-Case Catalog** (generation priming, per-use-case templates, governance defaults) →
  it is a Layer-0 foundation artifact (design §15). SP-2 reads it *read-only* for the closed **banking
  boundary** and **blocked-data-class** screen only, via the read-only **`BankingDomainCatalog`**
  (§4.5, §5.4, §1.3 decision D8).
- **Any UI** — the confirmation/clarification console → the frontend sub-project. SP-2 is **API/command-first**,
  consistent with SP-0/SP-1 (§1.3 decision D6).

### 1.3 Design decisions (ledger)

| # | Decision | Choice |
|---|---|---|
| D1 | Scope shape | **Definition mode end-to-end + all shared Layer-1/2 machinery**; hypothesis mode is a real flow with a **stub single-call generator** (the real engine is SP-12). |
| D2 | Foundation | **Build on SP-0 only.** Reuse the run aggregate, staged-document DAG (incl. candidate-role docs + the document **`PRIMARY_SELECTED`** promotion for hypothesis-mode candidate selection — *not* request-level `select_candidate`, §7.1), `CLARIFICATION` gate, durable runtime, identity/SoD, audit — **no new aggregate and no event-store aggregate-CHECK** (unlike SP-1). Additive registrations: event-types, document-schemas, and **one backward-compatible human-gate/park-reason migration** (`USE_CASE_ONBOARDING` gate + `NEEDS_USE_CASE_ONBOARDING` park hold-state, mirroring SP-1's `0505`, §2.1). |
| D3 | Contract ownership | **SP-2 owns contract *semantics*; SP-0 owns the *envelope* + generic Draft schema** (design SP-0 §3.5, §12). Confirmed-contract content-schema is registered with SP-0's document registry. |
| D4 | Gate #1 confirmer | **Author self-confirms** — an *audited intent lock*, not a governance approval. Confirmer MUST be the authenticated human requester (never a service or the LLM). Independent bank-grade signer is Gate #2 (SP-5) (§8). |
| D5 | LLM reality | **`LLMClient` interface is mandatory; `FakeLLM` is the deterministic default; a real Claude adapter is shipped but config-gated, never required in CI.** Every call event-sourced; **no silent prod fallback**; structured-output → **bounded repair → fail into clarification**; **no PII to the LLM** (§9). |
| D6 | Surface | **API/command-first, no UI** in SP-2. |
| D7 | Catalog use | Catalog/overlay metadata used **only for normalization/ambiguity**, never as authoritative grounding (that is SP-3). SP-2 may *read* SP-1's merged-view API for names/types/grain (§4.4). |
| D8 | Banking scope | SP-2 reads the **`BankingDomainCatalog`** (§4.5) — **SP-0-governed, read-only reference data** — as the seed for the intake banking-boundary + prohibited-class screens (§5.4, §8.4). Deterministic outcomes: out-of-scope → **`OUT_OF_SCOPE`** and prohibited class → **`PROHIBITED_DATA_CLASS`** (both fail-closed, each stamping the reason/matched-class + catalog `version`); sensitive-proxy/ambiguous → clarification / compliance review; a new banking use-case routes to onboarding, not rejection (design §15.5–15.6). *(**RATIFIED** — the user explicitly approved the catalog as SP-0-governed read-only reference data; this resolves the former §16.8 open question, now §16 register entry 8 — not a deviation.)* |
| D9 | LLM call store | LLM call records are an **SP-2-owned immutable append-only `llm_call` record store** (mirrors SP-1's evidence store), referenced by `llm_call_ref`, classified **sensitive**, plus an `LLM_CALL_RECORDED` domain event on the run. *(SP-0's artifact enum has no LLM-call type — see §16.)* |
| D10 | Content schema | **Minimum-viable** contract content-schema, not maximal — only the fields Gate #1 and SP-3 need. |

---

## 2. Foundation reuse (build on SP-0)

SP-2 is a **thin domain layer over SP-0**, exactly as SP-1 is. Crucially — and unlike SP-1 — SP-2 needs **no
new aggregate and no event-store aggregate-CHECK migration**: the whole Layer-1/2 flow rides on SP-0's
**existing `run` aggregate**, its **DRAFT → CONFIRMED_CONTRACT** run states, its **staged-document DAG**, its
`CLARIFICATION` human-gate, and its **document `PRIMARY_SELECTED`** candidate promotion (hypothesis-mode
candidate selection is document-level, *not* request-level `select_candidate`, §7.1). SP-2's additions are
**additive registrations + handlers** — **including one small backward-compatible schema migration** that adds
a `USE_CASE_ONBOARDING` human-gate value + a `NEEDS_USE_CASE_ONBOARDING` park hold-state (§2.1, §5.4),
mirroring SP-1's additive `0505_overlay_gates.sql`. This is honest additive surface, not zero surface: SP-0's
base gate enum (`0070_identity_authz_gates.sql`) has no onboarding gate and base `RUN_PARKED` carries only
`owner`/`waiting_on_fact` (`run_lifecycle.py`), so SP-2 registers both additively. What SP-2 *avoids* (unlike
SP-1) is a **new aggregate** or an **event-store aggregate-CHECK** migration — the human-gate widening is
neither.

SP-2 reuses, verbatim from SP-0:

- **The event store + envelope + `global_seq`** (SP-0 §3.1) — every SP-2 action is an event on the `run`/`request`
  aggregate with the standard identity + provenance envelope.
- **The staged-document DAG** (SP-0 §3.4) — the Draft Contract, Assumption Ledger, and Confirmed Contract are
  **frozen, content-hashed, DAG-linked documents** (`derived_from`/`supersedes`), with candidate/primary
  branch roles and `PRIMARY_SELECTED` events (used for multi-candidate hypothesis mode, §7).
- **The normative Draft schema** (SP-0 §3.5) — SP-0 owns the envelope fields (`request_id`, `intake_mode`,
  `raw_input_ref`, `raw_input_classification`, `open_fields`, `assumption_ledger_ref`, `status`); **SP-2 fills
  in and validates the *semantics*** (§4). SP-0 validates envelope + required-field presence; **semantic
  validation is SP-2's** (SP-0 §3.5:184).
- **The document/artifact schema registry** (SP-0 §3.7) — SP-2 registers versioned content-schemas for
  `DRAFT_CONTRACT`, `ASSUMPTION_LEDGER`, and `CONFIRMED_CONTRACT` (already in SP-0's published stage enum) with
  total/chained reader-upcasters. The stage enum is **not** extended.
- **The human-gate task model** (SP-0 §7) — the `CLARIFICATION` gate already exists with
  `allowed_responses: [confirm, edit, reject]`, `required_inputs`, `task_version`, quorum, and the
  `open | answered | conflict | expired | cancelled | superseded` lifecycle. SP-2 uses it directly for the
  Human Clarification Gate (§6.5) and Human Gate #1 (§8).
- **Identity + command authz + structural SoD** (SP-0 §6) — `create_request`, `create_run`, and
  `submit_human_signal(gate=CLARIFICATION)` all already carry authz rows admitting the
  **`data_scientist` role for *any* human** (role-scoped, **not** keyed to the specific request owner — SP-0
  §6.2, `authz_policy`), plus a `service:intake-agent` service principal for system-initiated steps
  (SP-0 §6.2). (Hypothesis-mode candidate selection is a **document `PRIMARY_SELECTED`** promotion, *not* the
  request-level `select_candidate` command — §7.1.) Because SP-0's role-authz and `submit_human_signal`
  eligibility check the *role/scope/quorum*,
  **not** that the acting `subject` is the task's requester, **SP-2 builds an explicit request-owner guard** on
  top (§8.2, §6.5, §2.1) — additive, in SP-2's own handlers, changing no SP-0 row. **On gate/park vocabulary
  SP-2 is *additive*, not zero-surface:** it registers **one new human-gate value + one park hold-state**
  (`USE_CASE_ONBOARDING` gate / the `NEEDS_USE_CASE_ONBOARDING` park-reason, §5.4, §11) via a small
  backward-compatible migration — exactly as SP-1 added its overlay gates (`0505_overlay_gates.sql`), §2.1 —
  while adding **no new run aggregate and no event-store aggregate-CHECK** (§2).
- **The durable runtime** (SP-0 §5) — outbox, idempotent handlers, durable timers (clarification-SLA →
  reminder → escalation → auto-park), bounded retries with hard loop limits (used to bound the Refinement
  Loop, §6.6), and the atomic one-transaction-per-step boundary.
- **The security-audit stream** (SP-0 §6.2) — denied/unauthorized attempts (e.g. a service trying to confirm
  Gate #1, §8.2) are recorded here, not in the domain stream.

### 2.1 Additive SP-0 registrations SP-2 ships (all backward-compatible)

1. **Event types** registered in SP-0's event-type registry (SP-0 §3.3), schema-owned by SP-2, emitted on the
   `run` aggregate: `INTENT_SUBMITTED`, `DRAFT_CONTRACT_PRODUCED`, `CONTRACT_CRITIQUED`,
   `FIELD_AUTO_RESOLVED`, `CLARIFICATION_REQUESTED`, `CLARIFICATION_ANSWERED` (a thin domain shadow of the
   SP-0 gate answer, carrying the re-normalization trigger), `CONTRACT_REFINED`, `MINIMUM_CONTRACT_VALIDATED`,
   `CONTRACT_CONFIRMED`, `USE_CASE_ONBOARDING_REQUESTED` (a new banking use-case parked for governance
   onboarding, §5.4, §11), `INTENT_REJECTED` (carrying the deterministic banking-boundary classification
   reason — `OUT_OF_SCOPE` or `PROHIBITED_DATA_CLASS` — plus the `BankingDomainCatalog` version, §5.4, §8.4),
   and `LLM_CALL_RECORDED` (§9.3).
2. **Document content-schemas** for `DRAFT_CONTRACT`, `ASSUMPTION_LEDGER`, `CONFIRMED_CONTRACT` registered in
   SP-0's document registry (SP-0 §3.7), versioned, with reader-upcasters (§4).
3. **The `llm_call` immutable record store** — a new SP-2-owned append-only table (an SP-0-style write-once
   artifact, like SP-1's `overlay_evidence`), referenced by `llm_call_ref`, **classified sensitive /
   governance-retained / read-controlled** (§9.3). This is *not* an event aggregate and needs no CHECK change.
4. **Handlers + a lifecycle guard set** wired into SP-0's durable runtime for the Layer-1/2 flow (§11), and
   the SP-2 lifecycle guards registered in SP-0's predicate registry (SP-0 §4.1) so the DRAFT →
   CONFIRMED_CONTRACT transition is machine-checkable — **including the SP-2-built request-owner guard** that
   SP-0 authz does **not** provide. SP-0 admits the `data_scientist` role generally (`authz_policy`, SP-0 §6.2)
   and `submit_human_signal` never checks the acting `subject` against `eligible_assignees`; SP-2 therefore
   pins the acting `subject` to the request owner via `actor_is_request_owner` (at `answer_clarification`,
   §6.5) and `confirmer_is_requester_human` = `actor_is_request_owner ∧ actor_kind==human` (at
   `confirm_contract` / Gate #1, §8.2). A mismatch is **denied + written to the security-audit stream**.
   **Plus one additive `authz_policy` row (rejection authority):** because SP-0's `reject` action is
   **validator-only** (`authz/policy.py:42`), SP-2 registers **one additive `authz_policy` row** admitting the
   **platform/service principal** (`service:intake-agent`) to issue the deterministic intake-rejection terminal
   outcome **`reject_intent`** (→ SP-0 `RUN_REJECTED`; §5.4, §11). This **adds** a row and **changes no existing
   SP-0 row**, so SoD holds — the validator-only `reject` and the data-scientist-owned `withdraw`
   (`authz/policy.py:41`) are untouched, and **requester-initiated abandonment reuses that existing `withdraw`,
   not `reject`.**
5. **One additive human-gate + park-reason registration** — a small backward-compatible migration (mirroring
   SP-1's `0505_overlay_gates.sql`) that **widens SP-0's `human_tasks` gate CHECK** with a new
   `USE_CASE_ONBOARDING` gate value and registers the **`NEEDS_USE_CASE_ONBOARDING` park hold-state** as an
   additive `RUN_PARKED` park-reason (§5.4, §11). This is *required* because SP-0's base gate enum
   (`0070_identity_authz_gates.sql`: `CLARIFICATION`/`DATA_STEWARD`/`COMPLIANCE`/`INDEPENDENT_VALIDATION`/
   `FINAL_APPROVAL`) has **no** onboarding gate and base `RUN_PARKED` carries **only** `owner`/`waiting_on_fact`
   (`run_lifecycle.py`). It only widens a CHECK / adds allowed values — it changes no existing row and adds no
   new *aggregate*, so it is **not** an event-store aggregate-CHECK migration.

All five are additive and backward-compatible; no existing SP-0 row, document, or event is rewritten.

---

## 3. The two intake modes

Intent arrives in exactly one of two modes (design §14.1). Both converge on the **same Confirmed Feature
Contract** (§4.2) and the **same safety floor** — the mode only changes *how much the platform generates*.

### 3.1 Definition-driven (precise spec → platform translates) — **built end-to-end**

The scientist states the exact feature; there is little to invent, so the platform **translates faithfully**
rather than generating alternatives. This is the mode SP-2 builds and tests fully, because it is
**deterministically checkable** — there is one correct normalization of a given definition.

> **Running definition example:** *"90-day rolling count of declined card authorizations per customer."*
>
> The Intake Agent structures this into: entity = `customer`; grain = `customer × as_of_date`; calculation
> method = `rolling_count`; window = `90d`; filter = `authorization.status = 'declined'` on
> `card_authorizations`; observation intent = point-in-time as-of `as_of_date`. Every field is either
> directly stated (window, method, entity) or a **recorded assumption** (grain's `as_of_date` companion; the
> exact declined-status encoding) — no field is silently invented (§5.3).

### 3.2 Hypothesis-driven (loose belief → platform generates) — **real flow, stub generator**

A belief, not a formula; the platform proposes **1–3 candidate feature definitions/calculations** for the
scientist to pick from at Gate #1. In SP-2 the **flow is real and fully tested** — the `CandidateGenerator`
seam, the candidate schema, multi-candidate staged documents, and the Gate #1 scored-selection UX — but the
**generator itself is a deliberately dumb single-call stub** (§7). The full engine is SP-12.

> **Running hypothesis example:** *"Customers who abruptly shift spending category are higher credit risk."*
>
> The stub generator makes **one** LLM structuring call and emits 1–3 plain-English candidate definitions,
> each with a one-line rationale (FeatLLM-style, design §14.2), e.g.: (a) *"count of distinct
> merchant-category codes in the last 30d minus the prior 30d"*; (b) *"share of spend in the top-1 category
> this month vs. the 3-month average"*; (c) *"Jensen-Shannon divergence between this month's category-spend
> distribution and the trailing-6-month distribution."* Each is a candidate document; the scientist confirms
> one at Gate #1 (§8). The *quality* of this set is intentionally unguaranteed in SP-2 (§7.3 rationale).

### 3.3 Why definition mode is built and generation is deferred (rationale, for the record)

- **Definition mode is deterministically testable** — one correct normalization — so `FakeLLM` fixtures can
  assert exact Draft/Confirmed contracts. Generation has **no ground truth**: there is no single right set of
  candidates, so it cannot be unit-asserted the same way.
- **Generation quality has no floor until SP-3/4/5 exist.** The real engine (SP-12) *learns from validation
  outcomes* (IV/WoE scores, overfitting-guard results) that simply do not exist yet — building it now would
  be building a learner with no teacher.
- **It keeps the first auditable-LLM surface minimal.** SP-2's job is to make the *LLM-in-a-bank* contract
  airtight (audit, structured output, no-PII, fail-closed). A single-call stub exercises that surface without
  the combinatorial search of the real engine.

---

## 4. Data model — the Feature Contract content-schema

The Feature Contract is the single artifact that flows through the platform, gaining structure at each stage
(design §4). SP-2 produces its **first two stages**: the **Draft** (Layer 1) and the **Confirmed** (Layer 2).
Both are **frozen staged documents** on SP-0's DAG (§2). The **semantic ↔ envelope split** is the load-bearing
boundary with SP-0.

### 4.0 The semantic ↔ envelope split (SP-2 owns semantics, SP-0 owns the envelope)

| Concern | Owner | Where it lives |
|---|---|---|
| Document identity, `content_hash`, `derived_from`/`supersedes`, `branch_role`, frozen storage, DAG acyclicity | **SP-0** | Staged-document DAG (SP-0 §3.4) |
| `request_id`, `intake_mode`, `raw_input_ref` + `raw_input_classification`, `open_fields`, `assumption_ledger_ref`, top-level `status` | **SP-0** | Normative Draft schema (SP-0 §3.5) |
| **Feature semantics** (entity/grain, observation intent, calculation method, windows, filters, target) | **SP-2** | Draft/Confirmed content-schema (below) |
| **Assumption Ledger** entries, per-field **ambiguity + confidence** scores, **open questions** | **SP-2** | This section |
| Content-schema **versioning + upcasters** for `DRAFT_CONTRACT`/`ASSUMPTION_LEDGER`/`CONFIRMED_CONTRACT` | **SP-2**, registered in **SP-0's** registry | SP-0 §3.7 |

SP-0 validates the envelope and *required-field presence*; **SP-2 runs all semantic validation** (grain
resolved, method chosen, no unresolved high-ambiguity field, in-banking-scope — the Minimum Contract
Validation of §6.7). A Draft with a non-empty `open_fields` cannot pass Gate #1 (SP-0 §3.5:182).

### 4.1 Draft Feature Contract (Layer 1 output — never executable)

Captures the LLM's structured reading of free text, with everything uncertain marked `UNKNOWN` and listed in
`open_fields`. `status: NEEDS_CLARIFICATION`. Content body (the SP-2-owned semantic block; the SP-0 envelope
fields wrap it):

```json
{
  "raw_input_ref": "blob_01H...",               // SP-0 envelope field; encrypted, access-restricted; never inline (SP-0 §9)
  "raw_input_classification": "clean",          // SP-0 envelope field: clean | contains_pii | unscanned (SP-0 §3.5)
  "intake_mode": "definition",
  "proposed_feature_name": "declined_card_auth_count_90d",   // LLM-proposed; human-editable at Gate #1 → Confirmed feature_name (§4.2, §8.3)
  "feature_semantics": {
    "entity": "customer",
    "entity_grain": ["customer_id", "as_of_date"],   // as_of_date is an ASSUMPTION (see ledger)
    "observation_intent": {
      "kind": "point_in_time",
      "as_of_field": "as_of_date",
      "rule": "use only data available strictly before as_of_date"
    },
    "calculation_method": "rolling_count",
    "windows": [{ "name": "lookback", "value": "90d" }],
    "filters": [{ "concept": "declined card authorization", "predicate": "UNKNOWN" }],
    "target_definition": "N/A (definition-mode feature, no target)"
  },
  "field_scores": {                              // per-field ambiguity + confidence (§6.1)
    "entity":              { "ambiguity": 0.05, "confidence": 0.97, "source": "llm" },
    "entity_grain":        { "ambiguity": 0.30, "confidence": 0.72, "source": "default" },
    "calculation_method":  { "ambiguity": 0.10, "confidence": 0.90, "source": "llm" },
    "windows":             { "ambiguity": 0.05, "confidence": 0.98, "source": "llm" },
    "filters":             { "ambiguity": 0.80, "confidence": 0.40, "source": "llm" }
  },
  "open_fields": ["filters.declined_status_encoding"],   // mirrored into the SP-0 envelope open_fields
  "open_questions": [
    {
      "field": "filters.declined_status_encoding",
      "question": "Which column/value marks a declined authorization — status='DECLINED', response_code!='00', or auth_result='D'?",
      "ambiguity": 0.80, "confidence": 0.40,
      "blocks_progress": true, "routed_to": "human"   // Doubt Router decision (§6.2)
    }
  ],
  "provenance": { "llm_call_refs": ["llmc_01H..."], "schema_version": 1 },
  "status": "NEEDS_CLARIFICATION"
}
```

### 4.2 Confirmed Feature Contract (Layer 2 output — the first executable-eligible artifact)

Every P0 field resolved, either by human confirmation or a **recorded, human-acknowledged default**. The
`open_fields` list is empty; Minimum Contract Validation (§6.7) has passed; Gate #1 (§8) has locked it.

```json
{
  "feature_name": "declined_card_auth_count_90d",   // from the Draft's proposed_feature_name (§4.1), human-editable at Gate #1 (§8.3)
  "intake_mode": "definition",
  "entity": "customer",
  "entity_key": "customer_id",                   // split from the Draft's entity_grain (Draft→Confirmed rename, below)
  "feature_grain": ["customer_id", "as_of_date"],  // the Draft's entity_grain (§4.1), persisted under this confirmed-stage name
  "observation_intent": {
    "kind": "point_in_time",
    "as_of_field": "as_of_date",
    "rule": "use only data available strictly before as_of_date"
  },
  "calculation_method": {
    "chosen": "rolling_count",
    "considered": ["rolling_count"],             // definition mode: one faithful translation
    "window": "90d",
    "filter": {
      "concept": "declined card authorization",
      "predicate": "card_authorizations.auth_result = 'D'"   // confirmed at the clarification gate
    }
  },
  "target": null,                                // definition-mode feature; hypothesis mode carries a target
  "assumption_ledger_ref": "doc_01H...",
  "requires_independent_validation": false,      // set true when a risk flag fires (§8.4)
  "confirmation": {
    "confirmed_by": "user:raj (data_scientist)", // the authenticated requester — never a service/LLM (§8)
    "confirmed_at": "2026-07-01T10:22:41Z",
    "selected_candidate": null,                  // hypothesis mode records the chosen candidate doc_id
    "rejected_candidates": [],                   // + the rejected sibling doc_ids (§8.3)
    "human_edits": [ { "field": "calculation_method.filter.predicate",
                       "from": "UNKNOWN", "to": "card_authorizations.auth_result = 'D'" } ],
    "ambiguity_notes": "declined encoding confirmed against catalog value set by requester"
  },
  "provenance": { "derived_from": ["doc_01H...(draft)"], "llm_call_refs": ["llmc_01H..."], "schema_version": 1 },
  "status": "CONFIRMED"
}
```

The **hypothesis example** produces the same shape with `intake_mode: "hypothesis"`, a non-null `target`
(e.g. the confirmed credit-risk label definition), a `calculation_method.chosen` picked from
`considered` (the candidates of §3.2), and `confirmation.selected_candidate`/`rejected_candidates` populated.

> **Draft→Confirmed field renames (deliberate).** Two Draft fields take their confirmed-stage names here:
> the Draft's **`entity_grain`** (§4.1) is persisted as **`feature_grain`**, with the entity key split out
> into **`entity_key`** (here `customer_id`); and the Draft's LLM-proposed **`proposed_feature_name`** (§4.1)
> is persisted as the human-editable **`feature_name`**. Everything else keeps its Draft name (including the
> SP-0 envelope fields `raw_input_ref` / `raw_input_classification`). Minimum Contract Validation (§6.7)
> validates the fields the **Draft** actually carries (`entity` + `entity_grain`); the renamed forms are the
> confirmed-stage persistence.

### 4.3 Assumption Ledger (its own frozen document)

Every **inferred choice** the platform made instead of asking is recorded here — *never silently inlined*
(SP-0 §3.5:183). Each entry is `{ field, chosen_value, source, rationale, ambiguity, confidence,
auto_resolved_at }`, where `source ∈ {default, catalog, llm}`:

```json
{ "entries": [
  { "field": "entity_grain",
    "chosen_value": ["customer_id", "as_of_date"],
    "source": "default",
    "rationale": "point-in-time features are grained by entity × as_of_date by platform convention",
    "ambiguity": 0.30, "confidence": 0.72, "auto_resolved_at": "2026-07-01T10:19:03Z" },
  { "field": "calculation_method.window",
    "chosen_value": "90d",
    "source": "llm",
    "rationale": "window stated verbatim in the intent ('90-day rolling')",
    "ambiguity": 0.05, "confidence": 0.98, "auto_resolved_at": "2026-07-01T10:19:03Z" }
]}
```

The ledger is surfaced **in full** at Gate #1 so the confirmer reviews every assumption the platform made
(§8.1). It is persisted as part of the confirmation record (Decision D4). Bodies are `governance-retained`
(SP-0 §9) — needed for MRM reproduction and adverse-action explainability.

### 4.4 Catalog metadata is *input to normalization only* (not grounding)

During normalization and scoring, SP-2 may **read** SP-1's merged-view read API (`resolve_fact` /
`list_objects`, SP-1 §7, §10) for **names, types, and asserted grain** — e.g. to check that `customer_id`
exists and to score the ambiguity of "declined authorization" against the actual value set of a status column.
This is **advisory context only**: it is used to *score* and *frame clarifying questions*, never to *bind* a
concept to a column. **All policy-aware, VERIFIED-fact grounding is SP-3** (design §3:108–116). Concretely,
SP-2 **does not** call the write side of SP-1, does not open overlay confirmation tasks, and does not treat a
missing overlay fact as a blocker — a Draft can be confirmed with unresolved *grounding* because grounding
happens downstream; it only needs unresolved *meaning* eliminated.

### 4.5 The `BankingDomainCatalog` — SP-0-governed, read-only intake-classification reference data

The banking-boundary / blocked-class reference data that SP-2's intake screens read (§5.4, §8.4) is the
design's `banking-domain-catalog`, and it is **ratified as SP-0-governed, read-only reference data** used
**only for intake classification** — **never for grounding or execution** (all VERIFIED-fact grounding is
SP-3, §4.4, §10). SP-2 **reads** it; it never writes it. Because it is a *reference artifact* — not a
buildable SP-2 dependency — reading it does **not** violate the "SP-0 only" foundation rule (Decision D8).
This is the **ratified** resolution of the former §16.8 open question (the user explicitly approved it), **not
a deviation**. *(Distinct from the SP-1 merged-view catalog metadata of §4.4, which frames normalization, and
from the generation-priming `DomainCatalogEntry` slice the `CandidateGenerator` reads for allowed concepts,
§7.1–§7.2; the richer generation catalog is deferred to SP-12, §14.)*

**Contents — `BankingDomainCatalog`:**

| Field | Meaning |
|---|---|
| `allowed_domains` / `allowed_use_cases` | in-scope banking domains and use cases — the **closed banking boundary** |
| `out_of_scope_examples` | out-of-scope example intents / categories (→ **`OUT_OF_SCOPE`**, §5.4) |
| `blocked_data_classes` | **explicitly prohibited / blocked** data classes (→ **`PROHIBITED_DATA_CLASS`**, §5.4, §8.4) |
| `sensitive_proxy_hints` | sensitive-proxy hints carried **only** as *"requires clarification / compliance review"* — **never** an automatic block or standalone proof of prohibition (§6.2, §6.7, §8.4) |
| `jurisdiction_scope` / `use_case_scope` | the scope in which each rule applies — **where rules differ by product or region** |
| `version`, `owner`, `effective_date`, `source` / `provenance` | catalog **version**, governance **owner**, **effective date**, and **source/provenance** — the `version` is recorded on every non-clear classification outcome as audit / MRM provenance |

**Deterministic intake-classification outcomes (the behaviour §5.4 and §8.4 encode).** The intake screen is a
**deterministic classifier** over this seed (never the LLM's call, §5.4), producing exactly one of:

1. **Out of banking scope** → **reject or park as `OUT_OF_SCOPE`**, recording the **reason** and the **catalog
   `version`**. *(Terminal / park; fail-closed.)*
2. **Explicit prohibited data class** → **block / reject as `PROHIBITED_DATA_CLASS`**, recording the **matched
   class** and the **catalog `version`**. *(Terminal; fail-closed; re-checked as the authoritative backstop at
   confirmation, §8.4.)*
3. **Sensitive-proxy hint matched** → open **clarification / compliance review** (§6.2, §6.5) — **NOT** an
   automatic block. *(Non-terminal; routes into the existing clarification path.)*
4. **Ambiguous intent** → open **clarification** (§6.2) — **do NOT auto-reject**. *(Non-terminal.)*

Outcomes 1–2 are **deterministic, fail-closed, and never fake a compliance approval** (§8.4), and each stamps
the catalog `version` (and, for 2, the matched class) for audit/MRM; outcomes 3–4 are **not** terminal and
route into the clarification path (§6.2, §6.5). *(An in-scope banking request that matches no known use-case is
neither of these — it parks into `NEEDS_USE_CASE_ONBOARDING`, §5.4, §11.)*

---

## 5. Layer 1 — Intake & Normalization

**Purpose:** turn intent into a structured *draft* — never executable — and eliminate the possibility of the
LLM silently committing to a hidden reading. **Inputs → outputs:** free-text hypothesis *or* definition →
Draft Feature Contract + Assumption Ledger.

### 5.1 The authority model, made concrete

> **LLM structures/suggests · platform validates/enforces · human confirms · registry governs.**

| Actor | In Layer 1 it may… | It may NOT… |
|---|---|---|
| **LLM Intake Agent** (`service:intake-agent`) | propose the structured Draft, field scores, candidate calculations, and clarifying questions | decide anything is final; write a Confirmed contract; see raw data or PII |
| **Platform (deterministic)** | validate the content-schema, run the in-banking-scope + prohibited-intent screens, run Minimum Contract Validation, record every assumption | invent semantics; approve compliance |
| **Human requester** | later, at Gate #1, confirm meaning / pick a candidate / edit fields | (in Layer 1 the human has not yet acted) |
| **Registry** | — (governs from Gate #2 onward) | — |

### 5.2 Flow

```
submit_intent(request)                                   authz: data scientist (request owner) or service:intake-agent
  └─ create_request (SP-0) + create_run (SP-0) → run in DRAFT
  └─ PII-scan + classify raw intent (SP-0-owned envelope classification → raw_input_classification, §9.4) → encrypted blob (SP-0 §9); emit INTENT_SUBMITTED
  └─ banking-boundary classification (§5.4, over BankingDomainCatalog §4.5)
        ├─ out of banking scope   ──▶ OUT_OF_SCOPE → reject_intent → INTENT_REJECTED / park  (platform/service-issued; reason + catalog version)
        ├─ prohibited data class  ──▶ PROHIBITED_DATA_CLASS → reject_intent → INTENT_REJECTED  (platform/service-issued; matched class + catalog version)
        ├─ sensitive-proxy/ambiguous ──▶ clarification / compliance review (§6.2)  (NOT terminal)
        └─ in-banking, unknown use-case ──▶ NEEDS_USE_CASE_ONBOARDING (park, §5.4)
  └─ LLMClient.structure_intent(redacted_intent, catalog_metadata)   → event-sourced call record (§9)
        │  structured-output contract + bounded repair (§9.2)
        ▼
  └─ Draft Feature Contract (frozen doc) + Assumption Ledger (frozen doc)
        status = NEEDS_CLARIFICATION, open_fields populated
  └─ if intake_mode == hypothesis:  CandidateGenerator.generate(draft, catalog_metadata, domain_context)  (§7)
        → 1–3 scored candidate-role staged docs under the Draft stage  [each generate = event-sourced LLM call (§9)]
        this is the confirmable candidate set MCV #2 (§6.7) requires to exist pre-gate; the human picks one at Gate #1
        (definition mode has NO generation step — there is one faithful translation to confirm)
     emit DRAFT_CONTRACT_PRODUCED   ──▶  hand to Layer 2 (§6)
```

The run sits in SP-0's **DRAFT** run-state until Gate #1; **no downstream command may advance it** to
`CONFIRMED_CONTRACT` while `open_fields` is non-empty (guard `open_fields_empty`, §11).

### 5.3 No-silent-assumption rule

The Intake Agent is required to emit, for **every** field it did not take verbatim from the intent, either
(a) an **open question** (routed by the Doubt Router, §6.2) or (b) an **Assumption Ledger entry** with a
`source` and `rationale`. There is no third option — a field is never both absent from the ledger and taken as
settled. This is enforced deterministically: Minimum Contract Validation (§6.7) rejects any resolved field
that lacks either a human confirmation or a ledger entry.

### 5.4 The banking boundary — deterministic classification over the `BankingDomainCatalog`

The intake **banking-boundary screen** is a **deterministic classifier** over the read-only
`BankingDomainCatalog` seed (§4.5) — the closed banking boundary + entity/concept taxonomy (`allowed_domains`
/ `allowed_use_cases`, `out_of_scope_examples`), the `blocked_data_classes`, and the `sensitive_proxy_hints`.
It is **not** the LLM's call — the LLM may *suggest* a use-case label, but the deterministic screen decides the
outcome. Every non-clear outcome **records the matched reason and the catalog `version`** as audit/MRM
provenance. The screen produces exactly one of these **deterministic classification outcomes** (design §3:90,
§15.5–15.6):

1. **Out of banking scope** — no banking entity, data, or concept, or a match against `out_of_scope_examples`
   → **reject or park as `OUT_OF_SCOPE`**, recording the **reason** and the **catalog `version`**. Surfaced as
   `INTENT_REJECTED` (classification `OUT_OF_SCOPE`) or, where the request is held for review, an SP-0 `park`.
   **Fail-closed:** an out-of-scope intent never reaches normalization.
2. **Explicit prohibited data class** — the intent targets/filters on a `blocked_data_classes` member →
   **block / reject as `PROHIBITED_DATA_CLASS`**, recording the **matched class** and the **catalog
   `version`**. This is the **fail-closed** prohibited-class block; it is **re-run as the authoritative backstop
   at confirmation** by the §8.4 prohibited-intent screen and **never fakes a compliance approval**.
3. **Sensitive-proxy hint matched** — a `sensitive_proxy_hints` member → **open clarification / compliance
   review** (§6.2, §6.5), carried **only** as *"requires clarification / compliance review,"* **NOT** an
   automatic block or standalone proof. **Non-terminal.**
4. **Ambiguous intent** — banking-plausible but under-specified scope → **open clarification** (§6.2). **Do NOT
   auto-reject. Non-terminal.**

**Rejection / withdrawal authority.** The two deterministic **terminal** rejections (`OUT_OF_SCOPE`,
`PROHIBITED_DATA_CLASS`) are **platform/service-issued terminal outcomes** — the platform's *deterministic
classifier* decided (running as the `service:intake-agent` principal), **not** a validator. They therefore do
**not** reuse SP-0's `reject` command, whose authz is **validator-only** (`authz/policy.py:42`); reusing it
would misattribute a platform decision to a human validator. SP-2 issues them via its own platform/service
action **`reject_intent`** (→ SP-0 `RUN_REJECTED`) under an **additive service authz row** (§2.1 #4). By
contrast, **requester-initiated abandonment** — the *author* choosing to walk away from their own intent (e.g.
rather than edit a blocked or looping one) — reuses SP-0 **`withdraw`** (→ `RUN_WITHDRAWN`), which is
**data-scientist-owned** (`authz/policy.py:41`), never the validator-only `reject`.

A **new banking use-case** — a request that is *in-scope* banking but matches no known catalog use-case — is
**neither rejected nor blocked**: the run is **parked** (SP-0 `park`) into a hold state
**`NEEDS_USE_CASE_ONBOARDING`** and emits **`USE_CASE_ONBOARDING_REQUESTED`**, which opens a **governance
use-case-onboarding human-gate task** (SP-0's human-gate task model, owned by governance). Both the
`NEEDS_USE_CASE_ONBOARDING` park hold-state and the `USE_CASE_ONBOARDING` gate value are **additive
registrations SP-2 ships** (§2.1 #5) — SP-0's base `RUN_PARKED` payload carries only `owner`/`waiting_on_fact`
(`run_lifecycle.py`) and its base gate enum has no onboarding gate (`0070`), so SP-2 registers both via a small
backward-compatible migration (mirroring SP-1's `0505_overlay_gates.sql`). **SP-2 only routes/parks: the
onboarding *workflow* itself is out of SP-2 build scope** (§14) — SP-2 defines the park state + the routing
event + these additive registrations, not the onboarding gate's semantics. (The richer domain-priming of *generation* is SP-12;
SP-2 uses the catalog only as a boundary + blocked-class + proxy-hint reference, Decision D8, §4.5.)

---

## 6. Layer 2 — Contract control and human clarification (Human Gate #1)

**Purpose:** eliminate hidden LLM assumptions before any data work begins. **Inputs → outputs:** Draft
Feature Contract → Confirmed Feature Contract. **Gate (hard):** *No Confirmed Feature Contract → no mapping,
no compilation, no execution* (design §3:106).

### 6.1 Ambiguity + confidence scoring (per field)

Every semantic field carries two independent scores on a **0.0–1.0** scale:

- **ambiguity** — how many plausible readings the field has (0.0 = exactly one reading; 1.0 = many
  incompatible readings). Driven by the intent text + catalog metadata (e.g. a status concept that maps to
  several candidate columns/values scores high).
- **confidence** — how sure the platform is of the *chosen* reading (0.0 = a guess; 1.0 = stated verbatim or
  catalog-unique).

Scores come from **two sources, combined deterministically**: (i) the LLM's self-reported per-field
uncertainty (structured output, §9.1), and (ii) a **deterministic catalog-cardinality check** (how many
catalog objects/values a concept could bind to). Where they disagree, the **platform takes the more
cautious** (higher ambiguity / lower confidence) — the LLM can never *lower* a doubt the deterministic check
raised. *(The 0.0–1.0 scale and this combine rule were not fixed by the decision record; see §16.)*

### 6.2 The Doubt Router (per field: auto-resolve vs must-ask-human)

For each field the Doubt Router makes one deterministic decision:

```
auto-resolve  iff  ambiguity ≤ 0.30  AND  confidence ≥ 0.70
                   AND  a safe source exists (default or catalog value)
                   AND  the field is NOT policy-sensitive
                   AND  the field is NOT a calculation-method choice
otherwise → must-ask-human
```

- **auto-resolve** → record an **Assumption Ledger entry** (§4.3), emit `FIELD_AUTO_RESOLVED`, and continue.
- **must-ask-human** → raise a **Human Clarification task** (§6.5). The **calculation-method choice is always
  must-ask** in hypothesis mode (the whole point of Gate #1 is picking it), and any **policy-sensitive**
  field (e.g. a filter touching a protected attribute) is always must-ask regardless of score — it may never
  be auto-resolved. A **`sensitive_proxy_hints` match** (§4.5) is a **distinct routing outcome — "requires
  clarification / compliance review"** — that always opens a clarification task and may never be
  auto-resolved. This routing is **not** the deterministic prohibited-class block: a proxy hint is a *doubt to
  be reviewed*, never a standalone block, whereas the deterministic `PROHIBITED_DATA_CLASS` outcome (§5.4,
  §8.4) *rejects*.

The thresholds are **config-gated constants**, deliberately conservative (fail toward asking). *(The exact
threshold values were a reasonable call, §16.)*

### 6.3 Worked routing — the two running examples

- **Definition (declined-auth count):** `windows=90d` (amb 0.05, conf 0.98) and `calculation_method=rolling_count`
  (amb 0.10, conf 0.90) → **auto-resolve** (ledger). `entity_grain` gets the `as_of_date` companion by default
  (amb 0.30, conf 0.72) → **auto-resolve** (ledger, `source: default`). `filters.declined_status_encoding`
  (amb 0.80, conf 0.40) → **must-ask** — several columns/values could mark "declined." One clarification, then
  converge.
- **Hypothesis (abrupt category shift → credit risk):** the **calculation method** is always must-ask →
  presented as the 1–3 scored candidates (§3.2, §7). The **target** ("higher credit risk") is policy-sensitive
  (credit-decisioning use-case) → must-ask to pin its exact definition and confirm it is a permitted target.

### 6.4 Critique Service — `CONTRACT_REVIEW` mode (SP-2 owns this one mode)

A single LLM critique pass over the Draft, in the `CONTRACT_REVIEW` mode (design §8.1). It is a **challenger,
never a gate** (design §8.2): it detects contradictions, ambiguity, and scope problems and **feeds the Doubt
Router** — it can *raise* doubts and *add* open questions, but it **cannot** confirm, lower a doubt below the
deterministic floor, or silently rewrite the contract. Output is the structured `CONTRACT_REVIEW` shape
(design §8.1):

```json
{ "review_type": "CONTRACT_REVIEW", "status": "NEEDS_REVIEW",
  "findings": [
    { "severity": "HIGH", "category": "AMBIGUOUS_DEFINITION",
      "evidence": "'declined' could mean issuer-declined, expired, or fraud-blocked authorizations.",
      "recommendation": "Ask the requester to confirm the declined-status encoding.",
      "blocks_progress": true } ] }
```

Each `blocks_progress: true` finding forces its field to **must-ask** (an OR with the §6.2 decision). SP-2
implements exactly **one** mode; the reusable five-mode Critique Service is SP-8 (design §8.1, roadmap SP-8).

### 6.5 The Human Clarification Gate

Each must-ask field opens an **SP-0 `CLARIFICATION` human-gate task** (SP-0 §7) — the same infrastructure
SP-1 uses for confirmations. The task carries the field, the open question, the candidate readings (with their
scores), and `required_inputs` = the draft doc ref (so a re-normalized draft correctly *stales* a pending
answer, SP-0 §7:429). `allowed_responses` = `[confirm, edit, reject]`, and — because clarification is an
**author-owned intent lock** — **`delegation_allowed = False`** (SP-2 sets this explicitly: SP-0's
`GateTaskSpec.delegation_allowed` **defaults to `True`**, `contracts/envelopes.py:209`, so without it a
delegate of the author could still answer even though the request-owner *subject* guard holds — the guard pins
the acting subject, `delegation_allowed=False` forbids a stand-in). The **eligible assignee is the request
owner** (the data scientist) — clarification is answered by the author, consistent with Gate #1 being an
author-owned intent lock (§8). **But SP-0's `submit_human_signal` verifies the responder's *role/scope/quorum*,
not that the acting `subject` is in `eligible_assignees`** (SP-0 §7 / `gates/tasks.py`), so role-authz alone
would let *any* `data_scientist` answer another author's clarification. **SP-2's `answer_clarification` wrapper
therefore adds the explicit request-owner guard** (`actor_is_request_owner`, §8.2, §2.1): a *different* data
scientist is **denied + security-audited**, never counted. Answers are idempotent by `(task_id, subject)` and
quorum-1 (SP-0 §7).

### 6.6 The Contract Refinement Loop

```
human answers a clarification (submit_human_signal, gate=CLARIFICATION)
  └─ emit CLARIFICATION_ANSWERED (domain shadow of the SP-0 gate answer)
  └─ LLMClient.renormalize(prior_draft, answers)  → new frozen Draft doc (supersedes prior)  [event-sourced]
  └─ re-score all fields (§6.1) + re-run CONTRACT_REVIEW (§6.4)
  └─ re-run the Doubt Router (§6.2)
        ├─ still-open must-ask fields → open/refresh clarification tasks; loop
        └─ no open fields → Minimum Contract Validation (§6.7) → eligible for Gate #1
```

The loop **converges** because each answer removes at least one open field and answered fields are pinned
(not re-opened unless a *new* answer changes an input they depend on). It is **bounded** by SP-0's durable-
runtime hard loop limit (SP-0 §5): after a configured maximum number of refinement rounds it **auto-parks**
the run (SP-0 `park`) with a named owner for human follow-up, rather than looping forever. *(The specific
round cap is a reasonable call, §16.)* Each new Draft is a fresh frozen document that `supersedes` the prior
one on the DAG — full history is retained for audit.

### 6.7 Minimum Contract Validation (the deterministic checklist that gates Gate #1)

Before Gate #1 can open, a **deterministic checklist** must pass (design §3:104). It is pure and
machine-checkable (registered as SP-0 lifecycle guards, §11). All must hold:

1. **Grain resolved** — `entity` and the grain the **Draft carries** (`entity_grain`, §4.1) are present and
   non-`UNKNOWN`. (At confirmation this is persisted as `feature_grain` + the derived `entity_key`, §4.2 —
   MCV validates the Draft-stage field, not the renamed confirmed form.)
2. **Calculation method available for selection** — a *confirmable* method exists **pre-gate** (this does
   **not** assert `calculation_method.chosen` is already set — the CHOICE is recorded **at** Gate #1, §8):
   in **definition mode**, the single faithfully-translated `calculation_method` is present and non-`UNKNOWN`;
   in **hypothesis mode**, a **non-empty scored candidate set** (the 1–3 candidate-role docs produced by
   `CandidateGenerator.generate()` during intake, §5.2, §7) exists under the Draft, exactly one of which the
   confirmer selects at Gate #1.
3. **No unresolved high-ambiguity field** — `open_fields` is empty and no field remains `ambiguity > 0.30`
   without a ledger entry or human confirmation.
4. **Observation intent present** — point-in-time/observation rule is stated (so SP-3 can bind it).
5. **In banking scope** — the §5.4 deterministic classification over the `BankingDomainCatalog` (§4.5)
   returned neither `OUT_OF_SCOPE` nor `PROHIBITED_DATA_CLASS`, and (for a policy-sensitive use-case) the
   target is a permitted, non-blocked concept. A **prohibited-data-class** match routes to the §8.4 block
   (stamping the matched class + catalog `version`); a **sensitive-proxy-hint** match routes back to
   **must-ask clarification / compliance review** (§6.2), *not* to the deterministic block. This is the
   **deterministic pre-gate scope / blocked-class check** that lets Gate #1 *open*; it is deliberately re-run
   as the **fail-closed compliance backstop at the moment of confirmation** by the prohibited-intent screen of
   **§8.4 #2** (which is authoritative for the block). Both exist by design (§8.4).
6. **Every resolved field is accountable** — each has either a human confirmation or an Assumption Ledger
   entry (the §5.3 rule).

A failure of any check keeps the run in DRAFT and re-enters the Refinement Loop (or blocks, §8.4); it can
*never* open Gate #1 on an under-specified contract. On success: emit `MINIMUM_CONTRACT_VALIDATED`.

---

## 7. The `CandidateGenerator` seam (hypothesis mode)

### 7.1 The interface (stable across SP-2 → SP-12)

```python
class CandidateGenerator(Protocol):
    def generate(self, draft: DraftContract, catalog_metadata: CatalogView,
                 domain_context: DomainCatalogEntry | None) -> list[Candidate]: ...
```

```python
@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    definition_text: str          # plain-English, FeatLLM-style (design §14.2)
    rationale: str                # one-line causal reasoning, surfaced at Gate #1
    calculation_method: dict      # the structured method the definition compiles to
    signals: dict                 # cheap, model-free plausibility/quality signals ONLY (§7.3)
    provenance: dict              # llm_call_refs, generator_version
```

Each returned candidate is written as a **candidate-role staged document** under the run's Draft stage (SP-0
§3.4 multi-candidate support); the scientist's Gate #1 selection is a **document-candidate selection** — an
SP-0 **`PRIMARY_SELECTED`** promotion of the chosen candidate doc on the **run** aggregate (SP-0 §3.4,
`new_primary_selected` → payload `{doc_id, stage}`), which records **only the chosen** doc. **Documents are
write-once**, so the losing candidate docs are **left untouched** — there is **no per-doc "reject" event** (the
DAG has no such write); the rejected sibling `doc_id`s (with the selection reason) are captured **only in the
confirmation record** (§8.3, Decision D4's persisted set), never on the DAG. It is **not** the request-level
`select_candidate` command, which selects among *run* candidates
on a *request* stream (SP-0 §4.4, `request_aggregate.py`) — the wrong granularity here: SP-2's candidates are
**documents under a single run**, not runs under a request. **This document/selection machinery is identical
for the stub and for SP-12** — only the `generate` body changes.

### 7.2 The SP-2 stub generator (deliberately dumb, single call)

`StubCandidateGenerator.generate` makes **one** `LLMClient` structuring call → 1–3 candidate definitions with
rationales, each compiled to a `calculation_method`, each a candidate document. It has **no** router, **no**
specialists, **no** attempt/conceptual memory, **no** symbolic synthesis, **no** diversity/islands, and **no**
few-shot — those are SP-12 (design §14.6–14.9). It is domain-*aware* only to the extent of reading the
read-only per-use-case `DomainCatalogEntry` (the allowed-concepts slice of the `BankingDomainCatalog`, §4.5),
never the full generation prior.

### 7.3 What the stub does **not** do (the SP-12 boundary + rationale)

- **No predictive scoring.** `signals` carries only cheap, model-free plausibility/quality hints (does the
  definition reference existing catalog concepts? is the window sane? is it a duplicate of an in-flight
  candidate on this run?). It contains **no IV/WoE, no AUC, no overfitting-guard result** — those need a
  point-in-time labelled sample and live in SP-5/SP-7. The stub therefore presents candidates as **ranked by
  cheap heuristic + surfaced rationale**, honestly *not* by measured predictive power.
- **No learning loop.** Because the real engine improves from validation outcomes that don't exist until
  SP-3/4/5, the stub is stateless across runs.
- **No gate bypass.** Candidates flow through the normal pipeline and gates; the generator *proposes only*
  (design §14.2, roadmap SP-12: "proposes only — never bypasses gates").

**SP-2 must not import SP-12 scope.** The seam is the contract; SP-12 replaces the stub behind it without
touching Layer 1/2, the candidate schema, or the Gate #1 UX.

---

## 8. Human Gate #1 — the audited intent lock

Gate #1 is where the human **confirms business meaning** (design §3b). Per Decision D4 it is an **audited
intent lock, not a governance approval** — a light author-owned gate now; the independent bank-grade signer
is Gate #2 (SP-5).

### 8.1 What the confirmer sees and does

The now-unambiguous Draft is presented to the **request owner** with: the full **Assumption Ledger** (§4.3),
the resolved fields, any residual **ambiguity notes**, and — in hypothesis mode — the **1–3 scored candidates
with their plain-English definitions + rationales** (design §14.2: the scientist audits the *logic* before any
code exists). The confirmer then either:

- **Definition mode:** *confirms the faithful translation* (optionally editing a field), or
- **Hypothesis mode:** *picks the calculation method from the scored candidates* (a document-candidate
  **`PRIMARY_SELECTED`** promotion of the chosen candidate doc, §7.1 — not request-level `select_candidate`),

producing the **Confirmed Feature Contract** (§4.2) and moving the run DRAFT → **CONFIRMED_CONTRACT**.

### 8.2 Confirmer guardrails (verbatim intent of Decision D4)

- The confirmer **MUST be the authenticated human requester** — **never a service principal, never a
  *different* data scientist, and never the LLM**. **SP-2 builds this guard; SP-0 does not enforce it.** SP-0
  authz only admits the **`data_scientist` role for any human** (SP-0 §6.2, `authz_policy`) — for both the
  `submit_human_signal(CLARIFICATION)` path and the base run rows covering the Gate #1 `PRIMARY_SELECTED`
  promotion — and SP-0's `submit_human_signal` checks role/scope/quorum but
  **not** that the acting `subject` is in the task's `eligible_assignees` (`gates/tasks.py`) — so role-authz
  alone would let *any* data scientist confirm another's contract. SP-2 therefore adds an **explicit
  request-owner guard** in `confirm_contract` (and `answer_clarification`, §6.5): the acting human's
  **`subject` MUST equal the contract requester's `subject`**, combined with an explicit **`actor_kind ==
  human`** check (guard `confirmer_is_requester_human` = `actor_is_request_owner ∧ actor_kind==human`, §11). A
  service, the LLM, or a **different** data scientist is **denied and recorded in the security-audit stream**
  (SP-0 §6.2), never applied.
- **Delegation is off — the author-owned intent lock.** The Gate #1 confirmation task **and** the §6.5
  per-field clarification tasks are opened with **`delegation_allowed = False`**. The request-owner *subject*
  guard is **necessary but not sufficient**: SP-0's `GateTaskSpec.delegation_allowed` **defaults to `True`**
  (`contracts/envelopes.py:209`), which would otherwise let a delegate answer/confirm on the author's behalf
  even with the subject guard in place. Setting it `False` seals the lock — no delegate, deputy, or role-peer
  may stand in for the requester. (SP-2 sets this on the tasks it opens; it changes no SP-0 default.)
- **This is not four-eyes.** Gate #1 deliberately lets the author confirm their *own* intent (that is its
  purpose). It does **not** invoke SP-0's `FINAL_APPROVAL` requester≠approver SoD — that is Gate #2 (SP-5).

### 8.3 What is persisted at confirmation (full audit of the decision)

`CONTRACT_CONFIRMED` and the Confirmed-Contract document persist, immutably (Decision D4):

- the final **`feature_name`** — LLM-proposed in the Draft as `proposed_feature_name` (§4.1) and **editable
  by the confirmer at Gate #1** (any edit is also captured in the human-edits list below),
- **selected candidate** + **rejected candidates** — the chosen candidate `doc_id` and the losing sibling
  `doc_id`s with the selection reason, recorded **here in the confirmation record only** (documents are
  write-once: there is **no per-doc rejection event** — the losers simply remain unpromoted candidate-role
  docs on the DAG),
- the **Assumption Ledger** as-confirmed,
- the **human edits** (field-level before/after),
- the **ambiguity notes**,
- the **confirmer identity** (subject, role claims at time of action, `source_of_authority`, SP-0 §6.1).

Bodies are `governance-retained` (SP-0 §9) — required for MRM reproduction, adverse-action, and dedup.

### 8.4 Risk flags, `requires_independent_validation`, and prohibited-intent blocking

Two deterministic screens run before / at Gate #1:

1. **Risk-flag screen → `requires_independent_validation`.** If the intent carries risk flags — e.g. the
   `BankingDomainCatalog` (§4.5) marks the use-case high-risk (credit-decisioning, adverse-action,
   fair-lending, MRM-high),
   or the target/filters touch a sensitive concept — the contract is confirmed **with
   `requires_independent_validation = true`**. This is a **flag only**: SP-2 does **not** require a second
   signer, and does **not** block. The independent validation / registration approval is **Gate #2 (SP-5)**;
   SP-2 just records that it will be needed (Decision D4). The credit-risk hypothesis example sets this true.

2. **Prohibited-intent screen → deterministic block, or route sensitive proxies to clarification.** This is
   the **fail-closed compliance backstop**, re-running the §5.4 deterministic classification over the
   `BankingDomainCatalog` (§4.5) at the moment of confirmation. It resolves into **two distinct routings**:
   - **Explicit prohibited data class → `PROHIBITED_DATA_CLASS` block.** If the intent targets or filters on a
     `blocked_data_classes` member (e.g. a protected attribute used as a credit-decisioning input), Gate #1
     **blocks / rejects as `PROHIBITED_DATA_CLASS`**, recording the **matched class** and the **catalog
     `version`**. It **must not pretend to approve compliance** (Decision D4): the contract cannot be CONFIRMED
     while a prohibited-data-class finding stands. The requester either **edits** the intent (back through the
     Refinement Loop) or **withdraws** it — a *requester-initiated abandonment* that reuses **SP-0 `withdraw`**
     (data-scientist-owned, `authz/policy.py:41`, → `RUN_WITHDRAWN`); independently, the **platform** records
     the deterministic block as its own **platform/service-issued terminal outcome** (`reject_intent` →
     `RUN_REJECTED`, additive service authz §2.1 #4), with the matched class + catalog `version`. **Neither path
     reuses SP-0's validator-only `reject`** (`authz/policy.py:42`): the classifier is not a validator, and the
     requester owns `withdraw`, not `reject`. This is a **deterministic** ruleset over the catalog's blocked
     classes, **not** an LLM judgement. *(The concrete ruleset mechanism was a reasonable call, §16.)*
   - **Sensitive-proxy hint → clarification / compliance review (not a block).** If the intent matches a
     `sensitive_proxy_hints` member, it is routed to **clarification / compliance review** (§6.2, §6.5), **NOT**
     auto-blocked. A proxy hint is a *doubt requiring review*, never standalone proof of prohibition; it is
     resolved (or escalated to compliance) before the contract can be CONFIRMED. This is the routing outcome
     distinct from the deterministic prohibited-class block (§6.2, §6.7).

   Compliance *approval* is never Gate #1's to give; it belongs to Compliance at the overlay/policy layer
   (SP-1) and the governance gates (SP-5/SP-9).
   **Relationship to MCV #5 (§6.7) — why both exist.** MCV #5 is the **deterministic pre-gate**
   scope/blocked-class check whose job is to decide whether Gate #1 may *open*. This §8.4 screen re-runs the
   same blocked-class/prohibited-intent classification (§5.4) as the **fail-closed backstop at the moment of
   confirmation**, so a contract can **never** be CONFIRMED if a `PROHIBITED_DATA_CLASS` finding stands — even
   one that appeared (or was missed) after the gate opened. They are the same deterministic ruleset applied at
   two checkpoints; **this confirmation-time screen is authoritative for the block.**

### 8.5 Output

On success: the **Confirmed Feature Contract** (§4.2), a frozen document `derived_from` the final Draft, is
the primary artifact of the CONFIRMED_CONTRACT stage — **the input to SP-3 grounding** (§10). The run is now
in SP-0's `CONFIRMED_CONTRACT` run-state; only now may it be handed downstream.

### 8.6 The Gate #1 task lifecycle (open / confirm / edit / OCC)

**Gate #1 is a SEPARATE, dedicated confirmation task — not the terminal per-field clarification task.** The
per-field clarification tasks of §6.5 exist to *remove doubts*; Gate #1 is a single, distinct task where the
author confirms (or edits/rejects) the **assembled** contract. It rides the same SP-0 `CLARIFICATION` gate
infrastructure (§2, no new gate value), so it carries `allowed_responses = [confirm, edit, reject]`,
`required_inputs`, `task_version`, and the `open | answered | conflict | expired | cancelled | superseded`
lifecycle (SP-0 §7).

- **`open_gate1_task`** — opens the dedicated confirmation task, and **only after Minimum Contract Validation
  passes** (§6.7, guard `minimum_contract_validated`). Its `required_inputs = [the final Draft doc ref]` (so a
  later re-normalization *stales* it by SP-0's task-staleness rule, §12); `eligible_assignees` = the request
  owner; it is opened with **`delegation_allowed = False`** and is **request-owner + `actor_kind==human`
  guarded** (§8.2). **Opening the gate cancels any
  still-pending per-field clarification tasks** for the run — they are moved to `cancelled` (SP-0 §7). (After a
  passing MCV there should be none; the cancel is the defensive close so no stale field task can be answered
  behind an open gate.)
- **`confirm_contract`** — the **`confirm`** response on the Gate #1 task. It writes `CONTRACT_CONFIRMED` + the
  frozen Confirmed-Contract document (§4.2, §8.3) — and in hypothesis mode the **document `PRIMARY_SELECTED`**
  candidate promotion (§7.1) — and drives the DRAFT → CONFIRMED_CONTRACT transition (guards of §11). The Gate
  #1 task moves to `answered`.
- **`request_edit`** — the **`edit`** response on the Gate #1 task: a human field edit *at the gate*. It
  produces a **REVISED Draft version** — a new frozen Draft document that `supersedes` the prior on the DAG —
  captures the change in the confirmation `human_edits` list (§8.3), and **re-runs Minimum Contract Validation**
  (§6.7) on the revised draft. Because the revised draft changes the Gate #1 task's `required_inputs`, the open
  Gate #1 task is **staled/superseded**; if MCV still passes, `open_gate1_task` re-opens a fresh confirmation
  task against the revised draft, and if the edit re-introduces an `open_field` the run drops back into the
  **Refinement Loop** (§6.6). An edit therefore never confirms silently — it always re-validates.
- **`reject`** — the **`reject`** response is a **requester-initiated abandonment** of the author's own intent →
  SP-0 **`withdraw`** (data-scientist-owned, `authz/policy.py:41`, → `RUN_WITHDRAWN`), **not** SP-0's
  validator-only `reject` and **not** the platform's `reject_intent`. (A prohibited-class finding is instead the
  §8.4 **platform/service-issued** block.) Both are non-confirming exits of §11.
- **Task-version optimistic concurrency.** Every `confirm`/`edit`/`reject` carries the `task_version` it read;
  SP-0's `submit_human_signal` **rejects a signal whose `expected_task_version != task_version`** (SP-0 §7,
  `gates/tasks.py`) — so a confirm or edit against a **stale** Gate #1 task (one already superseded by a
  concurrent re-normalization or a prior edit) is **not counted**, and the client must re-fetch the current
  task. This is the OCC guard that prevents a confirm from racing an in-flight re-normalization: the winner
  serializes on run-stream OCC (§12), the loser is rejected on `task_version`, never double-applied.

---

## 9. The auditable-LLM surface (`LLMClient` + `FakeLLM` + real Claude adapter)

This is SP-2's most consequential contribution: the platform's **first and permanent auditable-LLM boundary**.
Everything above depends on it. All agent code (Intake Agent, Critique `CONTRACT_REVIEW`, stub generator,
re-normalizer) depends on the **`LLMClient` interface — never on a provider** (Decision D5).

### 9.1 The `LLMClient` interface

```python
class LLMClient(Protocol):
    def call(self, request: LLMRequest) -> LLMResult: ...

@dataclass(frozen=True)
class LLMRequest:
    task: str                       # "structure_intent" | "contract_review" | "generate_candidates" | "renormalize"
    prompt_id: str                  # versioned, registered prompt template id
    prompt_version: int
    inputs: dict                    # redacted intent text + catalog METADATA only — NO data values (§9.4)
    output_schema_id: str           # the JSON schema the result MUST satisfy
    output_schema_version: int

@dataclass(frozen=True)
class LLMResult:
    output: dict                    # parsed, schema-valid structured output (or the call FAILED, §9.2)
    self_reported_scores: dict      # per-field ambiguity/confidence the model reports (input to §6.1)
    call_ref: str                   # llm_call_ref → the event-sourced record (§9.3)
    status: str                     # "ok" | "repaired" | "failed_into_clarification"
```

Every LLM interaction in SP-2 is **structured input → schema-validated structured output**. There is no
free-text path into the contract: the model fills a schema, the platform validates it, and only validated
structure enters a document.

### 9.2 Structured-output contract → bounded repair → fail into clarification (no silent bad structure)

```
call model with output_schema (structured output; §9.5 real-adapter mechanics)
  ├─ output parses AND validates against output_schema  → status = ok
  ├─ invalid / unparseable / refusal                    → REPAIR (bounded)
  │      re-prompt with the validation error, up to N attempts (default N = 2, config-gated)
  │      ├─ a repair validates  → status = repaired
  │      └─ repairs exhausted    → FAIL INTO CLARIFICATION / MANUAL PATH
  │             emit CLARIFICATION_REQUESTED with a "the assistant could not produce a
  │             usable structure; please specify <field> directly" question → human handles it
  └─ NEVER: silently accept a malformed structure, guess the field, or execute on it
```

This is the fail-closed core: **an invalid structure is a doubt, not a value.** On exhaustion the flow does
not error out or fabricate — it degrades to the **human clarification path** the platform already has (§6.5),
which is safe by construction. The default repair budget is **2** attempts; *(the number was a reasonable
call, §16)*. A **refusal** (the model declining) is treated exactly like an invalid structure — routed to
repair, then to clarification — never silently accepted (§9.5).

### 9.3 Every call is event-sourced (the `llm_call` record + `LLM_CALL_RECORDED` event)

Each `LLMClient.call` writes **one immutable record** to the SP-2-owned append-only `llm_call` store and emits
an `LLM_CALL_RECORDED` event on the run referencing it by `call_ref`. The record captures **the full,
reproducible provenance of the call** (Decision D5):

```
{ llm_call_ref, run_id, task, provider, model, prompt_id, prompt_version,
  output_schema_id, output_schema_version,
  input_hash,               // sha256 of the exact (redacted) input — OR the redacted input itself
  input_redaction,          // what was scrubbed, so a reviewer knows the boundary held (§9.4)
  raw_output,               // the model's structured output as returned
  validation_result,        // ok | invalid(reasons) | refusal
  repair_attempts,          // each attempt's error + re-prompt
  latency_ms, cost_metadata,
  created_at, created_by }  // service:intake-agent (attested, SP-0 §6.1)
```

- **Classification: sensitive / governance-retained / read-controlled** (like SP-1's evidence store) — the
  input hash + catalog metadata can be revealing, so the store is service-internal and access-controlled; no
  raw data values are ever present (§9.4). *(Modelling LLM calls as an SP-2-owned record store rather than an
  SP-0 aggregate/artifact was a reasonable call, Decision D9, §16.)*
- **Reproducibility:** because prompt+schema versions, model, and input hash are all pinned, a regulator can
  reproduce (or at least fully audit) exactly what was asked and what came back. Runs pin the registry
  snapshot (SP-0 §3.3) so the prompt/schema versions replay deterministically.

### 9.4 No raw data or PII to the LLM (boundary enforced, not merely intended)

The LLM sees **only**: (a) the scientist's **intent text, PII-scanned and redacted**, and (b) **catalog
metadata** — object/column **names, types, and asserted grain** (from SP-1's merged view). It **never** sees
data rows, column *values*, samples, extrema, or overlay evidence metrics (Decision D5). This is enforced at
**two points**:

1. **Ingest (owned by SP-0):** **SP-0 owns envelope PII-scanning + classification.** On submission SP-0 scans
   the raw intent and stamps `raw_input_classification ∈ {clean, contains_pii, unscanned}` (SP-0 §3.5); PII
   spans are redacted before the text is placed in any `LLMRequest.inputs`. If the intent cannot be safely
   redacted (classification `contains_pii` with un-redactable spans), the call **fails into the
   clarification/manual path** rather than sending it — no unsafe payload is ever dispatched.
2. **Egress guard (owned by SP-2 — the hard backstop):** SP-2 does **not trust** the envelope blindly. A
   deterministic pre-send check on every `LLMRequest` **refuses to dispatch** any payload whose
   `raw_input_classification` is **`unscanned`** (never send un-classified content to the LLM), or that
   carries data *values* (as opposed to metadata) or un-redacted PII; a violation is a **hard failure**
   recorded in the security-audit stream, not a warning. The `input_redaction` field on the call record
   documents what the guard scrubbed, so the boundary is auditable after the fact. **Two-point boundary,
   unambiguous:** SP-0 classifies at ingest; SP-2's egress guard is the fail-closed gate that lets only
   `clean`/safely-redacted, metadata-only payloads reach the model. *(This two-point scan-and-egress-guard
   construction extends the decision record's "only intent text + catalog metadata" rule into a concrete
   enforced boundary, §16.)*

### 9.5 `FakeLLM` (default) and the real Claude adapter (config-gated)

- **`FakeLLM`** — the deterministic default for all unit/integration tests, mirroring SP-1's `FixtureCatalog`
  pattern. It maps `(task, prompt_id, input_hash)` → a fixture structured output (and can be scripted to
  return invalid output, a refusal, or an ambiguous reading to exercise the repair loop, the fail-into-
  clarification path, and the Doubt Router). It is **hermetic and reproducible** — **no network, required in
  CI** (§15). Because definition mode has one correct normalization, `FakeLLM` fixtures assert exact Draft and
  Confirmed contracts.
- **Real Claude adapter** — shipped but **optional / config-gated, never required in CI** (Decision D5). It
  implements `LLMClient` over the Anthropic SDK: default model **`claude-opus-4-8`**, **adaptive thinking**
  (`thinking={"type": "adaptive"}`), and **structured outputs** via
  `output_config={"format": {"type": "json_schema", "schema": <output_schema>}}` (equivalently
  `messages.parse()` against the schema) so the model's response is schema-constrained at the source. It
  **must handle `stop_reason == "refusal"`** by routing to the repair/clarification path (§9.2) — never
  accepting empty/partial content as a value. The adapter carries **no production fallback to `FakeLLM`**: if
  it is enabled and unavailable, the platform **fails closed into the clarification/manual path**, it does not
  silently swap in the fake (Decision D5, "no silent production fallback"). No PII/data ever leaves via the
  adapter (§9.4).

The provider is **isolated to the adapter**; swapping providers, or SP-12 replacing the generator, touches no
agent, no document, and no gate.

---

## 10. Seams — what SP-2 consumes, exposes, and defers

### 10.1 Consumes (SP-0)

Event store + envelope; staged-document DAG + document registry (incl. the document **`PRIMARY_SELECTED`**
candidate-promotion primitive SP-2 uses for Gate #1 selection, §7.1); the run aggregate +
DRAFT/CONFIRMED_CONTRACT states + the lifecycle command catalog (`create_request`, `create_run`,
`submit_human_signal`, `park`/`unpark`, and `withdraw` for requester abandonment → `RUN_WITHDRAWN` — but
**not** SP-0's **validator-only** `reject`, which SP-2 never invokes from the requester/service path; SP-2's
deterministic intake rejection is its own platform/service `reject_intent`, §2.1 #4, §5.4); the `CLARIFICATION`
human-gate; identity/authz + structural
SoD + the security-audit stream; the durable runtime (outbox, timers, bounded retries); privacy/retention
(encrypted raw-intent blob, body classification). (SP-0 §§3–9.)

### 10.2 Reads (SP-1) — documented, **not** built here

SP-2 **reads** SP-1's **merged-view read API** (`resolve_fact`, `list_objects`; SP-1 §7, §10) for names /
types / grain during normalization and scoring **only** (§4.4). SP-2 does **not** build or invoke SP-1's
write side, does not open overlay confirmation tasks, and does not treat an unconfirmed overlay fact as a
blocker. SP-1's **`waiting_on_fact` park + `FACT_CONFIRMED_RESUME`** hook and its merged-view resolver are
documented as the seam a **later grounding flow (SP-3)** uses — SP-2 does **not** implement that saga (this
mirrors SP-1's own note that the resolver is built "for SP-2+ consumers," SP-1 §10). Grounding is SP-3.

### 10.3 Produces (→ SP-3)

The **Confirmed Feature Contract** (§4.2), a frozen CONFIRMED_CONTRACT-stage document with its Assumption
Ledger and confirmation record, is SP-2's sole downstream output — the **input to SP-3 grounding**. The
`requires_independent_validation` flag rides the contract to **Gate #2 (SP-5)**.

---

## 11. State machine / contract lifecycle

SP-2 spans SP-0's run-state **DRAFT → CONFIRMED_CONTRACT** (SP-0 §4.3). Internally, the contract advances
through SP-2 sub-states (all while the SP-0 run-state is `DRAFT`, until Gate #1):

```
                              submit_intent
                                   │
              banking-boundary classification (§5.4, over BankingDomainCatalog §4.5)
                    ├── out of banking scope ────────► OUT_OF_SCOPE → reject_intent → INTENT_REJECTED
                    │        (run REJECTED, platform/service-issued) | park; reason + catalog version (fail-closed)
                    ├── prohibited data class ───────► PROHIBITED_DATA_CLASS → reject_intent → INTENT_REJECTED
                    │        (run REJECTED, platform/service-issued); matched class + catalog version
                    │        (fail-closed; re-checked at §8.4)
                    ├── sensitive-proxy / ambiguous ─► CLARIFYING (clarification / compliance review, §6.2)
                    │        NOT terminal — routes into the clarification path
                    ├── in-banking, unknown use-case ─► NEEDS_USE_CASE_ONBOARDING  (SP-0 park + hold)
                    │        [additive park-reason + USE_CASE_ONBOARDING gate value SP-2 registers, §2.1 #5]
                    │        emit USE_CASE_ONBOARDING_REQUESTED → opens a governance onboarding
                    │        human-gate task  (the onboarding workflow itself is out of SP-2 scope, §14)
                    └── in-banking, known use-case
                                   ▼
                          NEEDS_CLARIFICATION  (Draft produced, open_fields populated)
                                   │  hypothesis mode: CandidateGenerator.generate() → 1–3 scored candidate docs (§7)
                                   │  (definition mode: no generation step — one faithful translation)
                                   ▼
                Doubt Router: all fields auto-resolvable?
                    ├── yes ─────────────────────────────────────────┐
                    └── no ─► CLARIFYING ◄── refinement loop ─────────┤
                               (human answers → renormalize → rescore)│
                                   │  (bounded; exhausted → PARK)     │
                                   ▼                                  ▼
                         MINIMUM_CONTRACT_VALIDATED  ◄────────────────┘
                                   │
                    prohibited-intent screen (§8.4, re-runs §5.4 over BankingDomainCatalog)
                    ├── prohibited data class ─► PROHIBITED_DATA_CLASS: BLOCKED → (edit → loop)
                    │        | withdraw (requester) | reject_intent (platform/service-issued)
                    │        records matched class + catalog version (authoritative block)
                    ├── sensitive-proxy hint ──► CLARIFYING (clarification / compliance review, §6.2)
                    └── clear ─►  READY_FOR_GATE_1
                                   │  open_gate1_task (dedicated CLARIFICATION-gate confirm task; cancels pending
                                   │                   clarification tasks; required_inputs = final Draft; §8.6)
                                   ▼
                    Human Gate #1  (requester + actor_kind=human; task_version OCC, §8.6)
                     ├── edit  ─► request_edit → REVISED Draft (supersedes) → re-run MCV ──► (loop / re-open gate)
                     ├── reject ─► withdraw (requester abandonment, run WITHDRAWN) | §8.4 platform block
                     └── confirm ─► confirm_contract (picks candidate via document PRIMARY_SELECTED in hypothesis mode)
                                   ▼
                            CONFIRMED  →  run advances to CONFIRMED_CONTRACT  →  SP-3
```

**Lifecycle wiring.** The DRAFT → CONFIRMED_CONTRACT transition is declared in SP-0's transition engine with
guards registered in SP-0's predicate registry (SP-0 §4.1) — `open_fields_empty`,
`minimum_contract_validated`, `not_prohibited_intent`, `confirmer_is_requester_human`,
`calculation_method_chosen`. Every SP-2 command runs the transition engine (guards evaluated on frozen
documents/version-attributes, pure/deterministic) **before** appending, so an illegal advance is rejected
before it is written. In hypothesis mode, `calculation_method_chosen` is satisfied by a **document
`PRIMARY_SELECTED`** promotion of the chosen candidate doc on the **run** aggregate (§7.1) — the document-level
primitive, *not* the request-level `select_candidate` command. The promotion payload records **only the chosen**
doc (`{doc_id, stage}`); the rejected sibling `doc_id`s are persisted **only in the confirmation record**
(§8.3), never via a per-doc event — documents are write-once. **Gate #1 task lifecycle (§8.6):** once
`minimum_contract_validated` holds, `open_gate1_task` opens a **dedicated** `CLARIFICATION`-gate confirmation
task (distinct from the per-field clarification tasks, which it **cancels**), keyed to the final Draft doc via
`required_inputs`. The confirmer's `confirm` → `confirm_contract` (DRAFT → CONFIRMED_CONTRACT); `edit` →
`request_edit`, which supersedes the Draft with a **REVISED** version, re-runs MCV, and re-opens a fresh gate
task (or re-enters the Refinement Loop); `reject` → the non-confirming exit. A `confirm`/`edit`/`reject`
carrying a **stale `task_version`** is rejected (SP-0 `submit_human_signal` OCC, §8.6, §12) so a confirm can
never race a re-normalization. **The two banking-boundary rejection outcomes are distinct and each carries its
provenance:** **`OUT_OF_SCOPE`** (reject-or-park; records the reason + catalog `version`) and
**`PROHIBITED_DATA_CLASS`** (block/reject; records the matched class + catalog `version`) — both are the
terminal/park refinements of the earlier generic `INTENT_REJECTED`, surfaced via the **platform/service-issued**
`reject_intent` → `INTENT_REJECTED` / `RUN_REJECTED` (additive service authz §2.1 #4 — **not** SP-0's
validator-only `reject`, `authz/policy.py:42`) or an SP-0 `park`, carrying that classification reason, and both
are **fail-closed**. The **sensitive-proxy** and
**ambiguous** cases are **not** terminal — they route into the existing clarification path (§6.2, §6.5).
the **platform/service-issued** `reject_intent` → `INTENT_REJECTED` terminal outcome (classification
`OUT_OF_SCOPE` or `PROHIBITED_DATA_CLASS`; own additive service authz, §2.1 #4), a **requester-initiated
`withdraw`** (SP-0, data-scientist-owned, `authz/policy.py:41`, → `RUN_WITHDRAWN` — the author abandoning their
own intent, e.g. a Gate #1 `reject` response), an auto-parked exhausted loop, and `NEEDS_USE_CASE_ONBOARDING`
(a new banking use-case parked for governance onboarding, §5.4 — an **additive park hold-state +
`USE_CASE_ONBOARDING` gate value** SP-2 registers, §2.1 #5) are the non-confirming exits. Terminal for SP-2's
span: `CONFIRMED_CONTRACT` (hands off), `REJECTED` (platform/service-issued intake rejection), or `WITHDRAWN`
(requester abandonment); the `NEEDS_USE_CASE_ONBOARDING` park exits SP-2 into a governance onboarding flow that
SP-2 does not build (§14).

---

## 12. Error handling & concurrency

- **Fail-closed everywhere.** No Confirmed contract → nothing downstream (the hard floor, §6). A malformed LLM
  structure is a doubt, not a value (§9.2). An un-redactable-PII intent fails into the manual path, never to
  the LLM (§9.4). A prohibited intent blocks (§8.4). An exhausted refinement loop parks (§6.6).
- **OCC on the run stream** (SP-0) — concurrent writers to one run serialize; each SP-2 step is one atomic
  SP-0 transaction (append event(s) + insert frozen doc(s) + upsert timers + outbox, SP-0 §5.1).
- **Clarification/Gate-#1 staleness is keyed to the task, not the run** (SP-0 §7:429): a pending clarification
  answer — or a Gate #1 `confirm`/`edit` (§8.6) — is rejected only if its `required_inputs` (the draft doc ref)
  changed since it opened — so a re-normalization or a gate `edit` correctly stales an in-flight answer, while
  unrelated run activity (reminders, sibling-candidate writes) does not. The timer/answer race, **and any
  signal against a stale Gate #1 task,** are rejected by CAS on `task_version` (SP-0 §5.5, §8.6) — a confirm can
  never race a re-normalization.
- **Idempotency** — `submit_intent` is idempotent per request; clarification answers idempotent by
  `(task_id, subject)` (SP-0 §7); `LLMClient.call` records are keyed by `(run_id, task, input_hash,
  prompt_version)` so a retried identical call reuses its record rather than double-charging.
- **Multi-candidate races** (hypothesis mode) — candidate documents are independent DAG writes; the Gate #1
  choice is a single **`PRIMARY_SELECTED`** promotion on the **run** aggregate (§7.1), so the run-stream **OCC**
  (above) serializes two concurrent promotions — only one wins; the losers stay **untouched** candidate-role
  docs (write-once — no per-doc reject event), their `doc_id`s recorded as rejected **only in the confirmation
  record** (§8.3). This is the document-primary primitive (SP-0 §3.4), *not* request-level `select_candidate`.
- **Degraded projections fail closed** (SP-0 §3.6) — a work-queue/lifecycle projection that cannot apply an
  event blocks the affected run's commands until `resolve_degraded`, never proceeding on a false view.

---

## 13. Interfaces SP-2 exposes (for SP-3+ consumers)

- **Commands:** `submit_intent(request, intent_text, intake_mode)`; `answer_clarification(task_id, actor,
  response)` (a thin wrapper over SP-0 `submit_human_signal(gate=CLARIFICATION)` that **enforces the SP-2
  request-owner guard** — acting `subject` == request owner, else DENY + security-audit; the underlying
  clarification tasks are opened **`delegation_allowed = False`**, §6.5, §8.2);
  `open_gate1_task(run_id, actor)` (opens the dedicated Gate #1 confirmation task once MCV passes, **with
  `delegation_allowed = False`**, cancelling pending clarification tasks, §8.6); `confirm_contract(run_id, actor, task_version, candidate_doc_id?)` (Gate
  #1 `confirm` — in hypothesis mode selects the calculation method by promoting the chosen candidate
  **document** via SP-0 **`PRIMARY_SELECTED`**, §7.1; **not** the request-level `select_candidate` command);
  `request_edit(run_id, actor, task_version, field_edit)` (Gate #1 `edit` → **REVISED** Draft version that
  supersedes the prior + re-runs MCV, §8.6). All three are **request-owner + `actor_kind==human` guarded**
  (§8.2) and reject a **stale `task_version`** (OCC, §8.6). `reject_intent(run_id, actor, reason)` — the
  **platform/service-issued** deterministic-classifier terminal outcome (`INTENT_REJECTED` → SP-0
  `RUN_REJECTED`) for `OUT_OF_SCOPE`/`PROHIBITED_DATA_CLASS`, carrying its **own additive service authz**
  (§2.1 #4, §5.4) because SP-0's `reject` is **validator-only** (`authz/policy.py:42`). **Requester-initiated
  abandonment** — the author walking away (e.g. a Gate #1 `reject` response) — instead reuses **SP-0 `withdraw`**
  (data-scientist-owned, `authz/policy.py:41`, → `RUN_WITHDRAWN`), never the validator-only `reject`.
- **Read model:** `get_contract(run_id) -> {stage, status, draft|confirmed body, assumption_ledger,
  field_scores, open_questions}` — service-internal, for SP-3 to fetch the Confirmed Contract.
- **The Confirmed Feature Contract document** (CONFIRMED_CONTRACT stage) — the governed hand-off artifact.
- **`CandidateGenerator` protocol** (§7.1) — the seam SP-12 binds its real engine to.
- **`LLMClient` protocol + the auditable-LLM envelope** (§9) — reusable by every later LLM-using sub-project
  (SP-3 grounding assistant, SP-6 candidate-SQL, SP-8 Critique, SP-12 generation).

---

## 14. What SP-2 deliberately does NOT do

The real hypothesis-generation engine (router, specialists, memory, symbolic, few-shot, diversity) — **SP-12**
· any data grounding / policy-aware mapping / point-in-time binding / Catalog Quality Gate — **SP-3** ·
independent validation, four-eyes signer, MRM, Human Gate #2, registration — **SP-5** · the reusable
five-mode Critique Service — **SP-8** · predictive candidate scoring (IV/WoE), overfitting guard — **SP-5/SP-7**
· the full Domain/Use-Case Catalog + generation priming — Layer-0 catalog work · any UI/console — frontend ·
the **use-case onboarding workflow** for a new banking use-case (SP-2 only parks the run into
`NEEDS_USE_CASE_ONBOARDING` and routes to it — the onboarding gate/workflow itself is governance-owned, §5.4)
· building SP-1's overlay write side or the `FACT_CONFIRMED_RESUME` grounding saga — **SP-1/SP-3**.

---

## 15. Testing (FakeLLM-driven, deterministic)

All tests run on **`FakeLLM`** — hermetic, reproducible, no network, required in CI (§9.5). The real Claude
adapter is exercised only in an **opt-in, config-gated smoke test** never gated in CI.

- **Intake / normalization:** `submit_intent` produces a Draft with `status=NEEDS_CLARIFICATION` and populated
  `open_fields`; the **definition example** normalizes to the exact expected Draft (deterministic);
  out-of-scope intent → **`OUT_OF_SCOPE`** (`INTENT_REJECTED`/park, stamping reason + catalog `version`); a
  prohibited-data-class intent → **`PROHIBITED_DATA_CLASS`** (block/reject, stamping matched class + catalog
  `version`); a sensitive-proxy-hint match → **clarification / compliance review** (not an auto-block); an
  ambiguous intent → clarification (not auto-reject); a new banking use-case routes to onboarding, **not**
  rejection; the no-silent-assumption rule holds (every resolved field has a ledger entry or a human
  confirmation).
- **Scoring + Doubt Router:** the deterministic catalog-cardinality check raises ambiguity the LLM under-
  reported (platform takes the cautious value); auto-resolve fires only inside the threshold **and** with a
  safe source **and** non-policy-sensitive **and** non-calculation-method field; a policy-sensitive field is
  **always** must-ask regardless of score; the calculation-method choice is **always** must-ask in hypothesis
  mode.
- **Critique `CONTRACT_REVIEW`:** a `blocks_progress` finding forces its field to must-ask; the critique can
  **raise** a doubt but can **never** lower one below the deterministic floor or confirm the contract.
- **Clarification + Refinement Loop:** a must-ask field opens an SP-0 `CLARIFICATION` task (eligible assignee =
  request owner); **only the request owner may answer — a *different* `data_scientist` (same role, different
  `subject`) is denied + security-audited by SP-2's request-owner guard, since SP-0 role-authz alone would
  admit them**; answering re-normalizes, re-scores, and closes the field; the **declined-encoding** example
  converges in one round; a re-normalization **stales** a pending unrelated answer (task `required_inputs`
  changed) while unrelated run activity does **not**; the loop is **bounded** — exhausting the round cap
  **auto-parks** the run.
- **Minimum Contract Validation:** each check independently blocks Gate #1 when it fails
  (grain / method / high-ambiguity-open / observation-intent / in-scope / accountable-field); success emits
  `MINIMUM_CONTRACT_VALIDATED`; an under-specified contract can **never** open Gate #1.
- **CandidateGenerator seam:** the **stub** emits 1–3 candidate documents with rationales; each is a candidate-
  role staged doc; a document **`PRIMARY_SELECTED`** promotion picks one; the losing siblings are **untouched**
  (write-once) and their `doc_id`s recorded as rejected **only in the confirmation record** (§8.3), never via a
  per-doc event (§7.1 — not request-level `select_candidate`); `signals` carries **no**
  predictive score (no IV/WoE/AUC); the seam is generator-agnostic (a fake alternate generator plugs in
  unchanged) — proving the SP-12 boundary.
- **Human Gate #1:** author-self-confirm produces the Confirmed Contract; **a service principal, the LLM, or a
  *different* data scientist (same `data_scientist` role, different `subject`) is denied → security-audit**
  (never applied) — proving SP-2's request-owner guard, since SP-0 role-authz alone would admit the other
  scientist; confirmer must be the authenticated requester with `actor_kind=human`; **selected + rejected
  candidates + assumptions + human edits + ambiguity notes + confirmer identity are all persisted**; definition
  mode confirms the faithful translation; hypothesis mode records the picked candidate.
- **Gate #1 task lifecycle (§8.6):** `open_gate1_task` fires **only after MCV passes** and **cancels** any
  still-pending clarification tasks; a `confirm`/`edit`/`reject` carrying a **stale `task_version`** is
  **rejected** (OCC) and never applied; `request_edit` produces a **REVISED** Draft that supersedes the prior,
  **re-runs Minimum Contract Validation**, and (if a field re-opens) re-enters the Refinement Loop or re-opens a
  fresh Gate #1 task; a confirm racing a concurrent re-normalization loses on `task_version` (never
  double-applied).
- **Risk flags + prohibited intent:** a high-risk-tier use-case sets `requires_independent_validation=true`
  **without** requiring a second signer or blocking; an explicit prohibited data class (blocked data class /
  protected attribute as credit input) → **`PROHIBITED_DATA_CLASS`** block (matched class + catalog `version`
  recorded) and can **never** be CONFIRMED; a **`sensitive_proxy_hints` match** routes to clarification /
  compliance review, **not** an auto-block; Gate #1 never "approves compliance."
- **Auditable-LLM surface:** every call writes an `llm_call` record + `LLM_CALL_RECORDED` event with provider /
  model / prompt+schema version / input-hash / output / validation-result / repair-attempts / latency-cost;
  **invalid output → bounded repair → (repaired) or fail into clarification** (never silent-accept, never
  execute); a **refusal** is treated as invalid (repair → clarification), never accepted; **repair budget
  exhausted → clarification task** raised; the record store is **immutable, sensitive, read-controlled**.
- **No-PII boundary:** a payload carrying data values or un-redacted PII is **rejected by the egress guard →
  security-audit** (hard failure, not a warning); an un-redactable-PII intent **fails into the manual path**
  and no payload is dispatched; `input_redaction` documents what was scrubbed; `FakeLLM` asserts only
  metadata (names/types/grain) — never values — reaches the model.
- **No silent fallback:** with the real adapter enabled-but-unavailable, the flow **fails closed into
  clarification**, and does **not** swap in `FakeLLM` (asserted via a fault-injected adapter).
- **Lifecycle / guards:** the DRAFT → CONFIRMED_CONTRACT transition is **rejected before append** when any
  guard fails (`open_fields_empty`, `minimum_contract_validated`, `not_prohibited_intent`,
  `confirmer_is_requester_human`); events rebuild the read model purely from the stream (self-describing).
- **Concurrency:** OCC serializes concurrent run writers; two concurrent candidate `PRIMARY_SELECTED`
  promotions on one run can't both win (run-stream OCC);
  a retried identical `LLMClient.call` reuses its record (no double-charge).

---

## 16. Decisions / deviations register

| # | Point | Decision-record source | What SP-2 did / where a call was made |
|---|---|---|---|
| 1 | Scope shape | Decision 1 | Definition mode end-to-end + all shared machinery; hypothesis is a real flow with a single-call **stub** generator; SP-12 boundary held (§3, §7). |
| 2 | No new aggregate | Decision (Seams) | **Reasonable call:** SP-2 rides SP-0's existing `run` aggregate + DRAFT/CONFIRMED_CONTRACT states + `CLARIFICATION` gate + the document **`PRIMARY_SELECTED`** candidate-promotion primitive (candidate selection is document-level, *not* request-level `select_candidate`, §7.1) — **no new aggregate and no event-store aggregate-CHECK migration** (unlike SP-1). Additive registrations only: event-types, document-schemas, and **one backward-compatible human-gate/park-reason migration** (`USE_CASE_ONBOARDING` gate + `NEEDS_USE_CASE_ONBOARDING` park hold-state, mirroring SP-1's `0505`, §2.1) — the base gate enum + `RUN_PARKED` payload carry neither (SP-0 `0070`, `run_lifecycle.py`). *The decision record listed the SP-0 dependencies but did not specify whether a new aggregate was needed; using the existing run spine is the minimal faithful encoding.* |
| 3 | Ambiguity/confidence scale + combine rule | Decision (Components) said "each field scored for ambiguity + confidence" | **Reasonable call:** fixed a **0.0–1.0** scale for both, sourced from LLM self-report **+** a deterministic catalog-cardinality check, with the platform taking the **more cautious** value on disagreement (§6.1). Scale and combine rule were not specified. |
| 4 | Doubt Router thresholds | Decision (Components): auto-resolve vs must-ask | **Reasonable call:** `auto-resolve iff ambiguity ≤ 0.30 AND confidence ≥ 0.70 AND safe source AND not policy-sensitive AND not calc-method`; config-gated, biased toward asking (§6.2). Exact thresholds were not specified. |
| 5 | Bounded repair budget | Decision 3: "bounded repair loop → on exhaustion fail into clarification" | **Reasonable call:** default **N = 2** structured-output repair attempts, config-gated, then fail into clarification; refusal treated as invalid (§9.2). The count was not specified. |
| 6 | Refinement-loop bound | Decision (Components): "converge until minimum-contract passes" | **Reasonable call:** loop bounded by SP-0's durable-runtime hard loop limit; on exhaustion **auto-park** the run for human follow-up (§6.6). The specific round cap was not specified. |
| 7 | LLM-call record store | Decision 3: "every LLM call is event-sourced" (fields enumerated) | **Reasonable call:** modelled as an **SP-2-owned immutable append-only `llm_call` store** (mirroring SP-1's evidence store) referenced by `llm_call_ref`, plus an `LLM_CALL_RECORDED` event — because SP-0's stage/artifact enum has no LLM-call type (§9.3, Decision D9). All enumerated fields captured. |
| 8 | Banking-scope / `BankingDomainCatalog` dependency | Decision 1, 4 & 8: "rejects only out-of-banking"; "depends on SP-0 only" *(record was previously silent on catalog availability)* | **RATIFIED (user-approved).** The banking-boundary / blocked-class reference data is accepted as **SP-0-governed, read-only reference data** — the **`BankingDomainCatalog`** (§4.5): `allowed_domains`/`allowed_use_cases`, `out_of_scope_examples`, `blocked_data_classes`, `sensitive_proxy_hints` (carried **only** as "requires clarification / compliance review," never an auto-block), `jurisdiction_scope`/`use_case_scope`, and `version`/`owner`/`effective_date`/`provenance`. It is **read-only intake-classification reference data — never grounding/execution** — so it is *not* an SP-2 build dependency and does **not** violate "SP-0 only." Deterministic intake outcomes: out-of-scope → **`OUT_OF_SCOPE`** and prohibited class → **`PROHIBITED_DATA_CLASS`** (both fail-closed, each stamping the reason/matched-class + catalog `version`); sensitive-proxy/ambiguous → clarification / compliance review; a new banking use-case → onboarding park (§5.4, §8.4, §11). **This resolves the former open question (was silent, §16.8) — now ratified, not a deviation; the user explicitly approved it.** |
| 9 | No-PII enforcement construction | Decision 3: "no raw data or PII to the LLM — enforce/validate this boundary" | **Reasonable call:** enforced at **two points** — ingest PII-scan/redact-or-fail, and a pre-send **egress guard** that hard-fails a payload carrying data values or un-redacted PII (→ security-audit), with `input_redaction` recorded for audit (§9.4). The decision record required the boundary; the two-point mechanism is the concrete encoding. |
| 10 | Prohibited-intent mechanism | Decision 2: "obviously prohibited/compliance-sensitive → blocks or forces clarification; must NOT pretend to approve compliance" | **Reasonable call (mechanism); RATIFIED (contract, see entry 8).** A **deterministic** screen over the `BankingDomainCatalog` `blocked_data_classes` (§4.5): an explicit prohibited data class → **`PROHIBITED_DATA_CLASS`** block (matched class + catalog `version`) → edit-and-loop, requester **`withdraw`** (SP-0, data-scientist-owned), or the **platform/service-issued** `reject_intent` terminal outcome (not SP-0's validator-only `reject` — see entry 13); a `sensitive_proxy_hints` match is the **distinct** routing → clarification / compliance review, **not** an auto-block; never an LLM judgement, never a compliance approval (§8.4). The screen mechanism was not specified; the proxy-vs-block distinction is the user-ratified contract. |
| 11 | Gate #1 is not four-eyes | Decision 2 | Encoded: author confirms own intent (audited intent lock); `requires_independent_validation` is a **flag only**, no second signer; independent validation is Gate #2 / SP-5 (§8.2, §8.4). |
| 12 | Real adapter details | Decision 3: "real Claude adapter shipped, config-gated, never required in CI; no silent fallback" | Encoded with concrete Claude API: model `claude-opus-4-8`, adaptive thinking, structured outputs via `output_config.format`, `stop_reason=="refusal"` → repair/clarification, fail-closed (no fallback to FakeLLM) (§9.5). *Model/API specifics grounded in the current Claude API; not a deviation.* |
| 13 | Rejection / withdrawal authority | SP-0: `reject` is **validator-only** (`authz/policy.py:42`); `withdraw` is **data-scientist-owned** (`authz/policy.py:41`) | **Corrected authority.** SP-2's deterministic intake rejections (`OUT_OF_SCOPE`/`PROHIBITED_DATA_CLASS`) are **platform/service-issued terminal outcomes** — the deterministic classifier decided, **not** a validator — issued via SP-2's own **`reject_intent`** action (→ SP-0 `RUN_REJECTED`) under **one additive service `authz_policy` row** (§2.1 #4); they do **not** reuse SP-0's validator-only `reject`. **Requester-initiated abandonment** (the author walking away — e.g. a Gate #1 `reject` response, or giving up on a blocked/looping intent) reuses **SP-0 `withdraw`** (→ `RUN_WITHDRAWN`), never `reject`. SP-0's validator-only `reject` stays reserved for independent validation (Gate #2 / SP-5). The added row **changes no existing SP-0 row**, so SoD holds. (§5.4, §8.4, §11, §13, §2.1.) |

---

## Appendix A — Sample stack (non-binding)

Python 3.11 · pytest. The Draft/Assumption-Ledger/Confirmed documents reuse **SP-0's staged-document DAG +
document registry** (SP-2 registers the three content-schemas + upcasters); the run flow reuses **SP-0's event
store, run aggregate, `CLARIFICATION` gate, durable runtime, identity/SoD, and security-audit**. The
`llm_call` record store is an SP-0-style append-only table (write-once, classified sensitive). `FakeLLM` is an
in-memory fixture client (`(task, prompt_id, input_hash) → structured output`, scriptable to invalid /
refusal / ambiguous) — the CI default. The real adapter is the **Anthropic Python SDK** (`anthropic`), default
model **`claude-opus-4-8`**, `thinking={"type":"adaptive"}`, structured output via
`output_config={"format":{"type":"json_schema","schema":…}}` (or `client.messages.parse()`), `stop_reason`
handled — config-gated, never in CI. Catalog reads go through **SP-1's merged-view API** (`resolve_fact` /
`list_objects`). The **`BankingDomainCatalog`** (§4.5) is loaded read-only from the design's
`banking-domain-catalog` seed for the boundary + blocked-class + sensitive-proxy screens, and every non-clear
classification stamps the catalog `version` for audit.

## Appendix B — The two running examples, end to end

**Definition — `declined_card_auth_count_90d`.**
`submit_intent("90-day rolling count of declined card authorizations per customer", mode=definition)` → Draft
(entity `customer`; grain `customer × as_of_date` [ledger: default]; method `rolling_count` [ledger]; window
`90d` [ledger]; filter `declined authorization` → **open, must-ask**, amb 0.80). One `CLARIFICATION` task to
the requester → *"auth_result = 'D'"*. Re-normalize → all fields resolved → Minimum Contract Validation passes
→ prohibited-intent screen clear (not a decisioning target) → Gate #1: requester confirms the faithful
translation → **Confirmed Contract** (§4.2), `requires_independent_validation=false` → SP-3.

**Hypothesis — abrupt spending-category shift → credit risk.**
`submit_intent("customers who abruptly shift spending category are higher credit risk", mode=hypothesis)` →
Draft with `target = "higher credit risk"` (**policy-sensitive**, must-ask) and calculation method
**must-ask**. `StubCandidateGenerator` makes **one** LLM call → 3 candidate documents (distinct-MCC delta;
top-category-share drift; distribution divergence), each with a plain-English rationale and cheap `signals`
(**no** IV/WoE). Risk-flag screen fires (credit-decisioning, MRM-high) → `requires_independent_validation`
will be set true. Prohibited-intent screen (§8.4, over `BankingDomainCatalog`): target must be pinned to a
permitted, non-blocked credit-risk label — a `sensitive_proxy_hints` match here routes to **clarification /
compliance review** with the requester; had it named a `blocked_data_classes` protected attribute it would
**block as `PROHIBITED_DATA_CLASS`** (matched class + catalog `version` recorded). At **Gate #1** the
requester reviews the three rationales + the Assumption Ledger, **picks one candidate** (a document
**`PRIMARY_SELECTED`** promotion, §7.1; the losing siblings' `doc_id`s recorded in the confirmation record
only — write-once docs, no per-doc reject event), confirms the pinned
target → **Confirmed Contract** with
`intake_mode=hypothesis`, `selected_candidate`/`rejected_candidates` recorded, `requires_independent_validation
=true` → SP-3 (and, later, the flag drives Gate #2 at SP-5).
