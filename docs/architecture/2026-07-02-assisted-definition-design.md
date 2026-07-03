# Assisted Definition — Design Spec (SP-2 addendum)

**Status:** Designed / agreed in dialogue (2026-07-02); **review rounds 1–4 incorporated** (38 findings → §14 resolutions R1–R38). Not yet planned or built.
**Relationship:** An addendum to SP-2 (`2026-07-01-sp2-intake-clarification-design.md`). Builds on SP-2's seams; the *quality* half depends on SP-12 (the real Feature Generation engine). Ships after SP-2 is merged — it must NOT destabilise the in-flight SP-2 branch.

---

## 1. Summary

Every feature request must carry a **hypothesis** (the "why"). The platform builds the requester's **definition** (the "what", if given) as the anchor, and — for discovery + a governance cross-check, **when a definition is present and the alternatives policy is on (see §6)** — **generates scored alternative definitions from the hypothesis** and runs an **advisory AI critique**. At **Human Gate #1** the requester sees their definition (pre-selected) alongside the scored alternatives (when generated) and any advisory notes, and confirms exactly one. The confirmed contract records the full considered set + the choice (`chosen_source`/`chosen_option_id`) + who + (conditionally) why.

The definition, when provided, is the pre-selected **anchor**. `intake_mode` is fixed at submit (definition present → `definition`, else `hypothesis`) and **never mutates** thereafter (§6).

### Authority model (unchanged from SP-2)
LLM **suggests/critiques** → platform **validates/enforces** → human **confirms** → registry **governs**. The AI never decides, never auto-approves, never silently swaps the anchor. No PII/raw data reaches the LLM (SP-2's redactor + egress guard, extended to the hypothesis in §5.3).

---

## 2. The flow (target end-state; pre-SP-12 bridge in §3)

```
                 DATA SCIENTIST
        ┌──────────────────────────────────┐
        │ Hypothesis H   ← MANDATORY        │
        │ Definition D   ← optional         │
        └────────────────┬─────────────────┘
                         │ submit_intent
                         │  (no hypothesis → COMMAND-VALIDATION DENIAL:
                         │   no run created, resubmit — NOT a terminal reject)
                         ▼
        ┌──────────────────────────────────┐
        │ redact + classify D and H         │  (§5.3: two screens — prohibited from
        │ (per-text + combined)             │   EITHER text; scope from COMBINED)
        │ intake_mode fixed here (immutable)│
        │ build anchor(D) if given          │
        │ generate alternatives from H *    │  (* only when — truth table §6)
        │ advisory critique *  (disposition=│  (* only when D present AND policy on;
        │   advisory; cannot block)         │   NOT on policy-off / hypothesis-only)
        │                                   │  → advisory_notes (blocks_progress=false)
        └────────────────┬─────────────────┘
                         ▼
        ┌───────────────────────────────────────────┐
        │              GATE #1                        │
        │   • D (anchor) ◀ pre-selected if given      │
        │   • A1, A2, …   (scored + why)              │
        │   • advisory_notes (never block)            │
        │        human confirms ONE option            │
        │   rationale_for_choice required IFF §7.2    │
        └───────────────────┬────────────────────────┘
                            ▼
                  CONFIRMED CONTRACT (v2)
     (chosen_option_id, chosen_source, considered set,
      advisory_notes, alternatives_provenance, confirmer,
      rationale_for_choice?; derived_from = chosen_option_id)
                            ▼
                    SP-3 → SP-4 → SP-5
```

**Definition present** → pre-selected anchor; alternatives around it when policy generates them (§6).
**Definition absent** → generation **always runs** (only source of an option — §3/§6); no anchor default.

---

## 3. Scope & staging (two levers, two schedules)

The §2 flow is the **target (alternatives policy ON)**. Levers:

| Lever | Ship when | Why |
|---|---|---|
| **Hypothesis mandatory** (command-validation denial on absence; new submits only) | **Now** (post-SP-2) | Governance/MRM win. Requires the schema-v2 change (§4.1). |
| **Alternatives-alongside-a-definition, policy ON** (no requester opt-out) | **After SP-12** | Always-on only helps when alternatives are good; SP-2's stub gives near-clones. |

**What the policy gates (resolves P1-3, round 1):** `alternatives_policy` controls whether alternatives are **added to a request that already has a definition**. It does **NOT** gate generation when there is **no definition** — with no anchor, generation is the only confirmable option, so it **always runs**. No empty-gate state. Full matrix in §6.

- **Pre-SP-12 (policy OFF):** definition present → pure definition path (no alternatives); definition absent → generate + pick. The unified "alternatives even for a definition" path is the flag-ON end state — stated honestly, not pretended.
- **SP-12 (policy ON):** definition-present requests also get alternatives → single §2 flow. No re-wiring — swap the stub behind the `CandidateGenerator` seam, flip the policy.

### 3.1 Hard prerequisite — candidate-body retrieval (resolves P1-1 / R21)
Confirming *any* generated candidate requires loading its body (§7.3). SP-2's candidate docs currently store only a `blob_index` hash (the 6.4/7.5 deferred item). Therefore **candidate-body retrieval (event-inline or object-store) is a first-build PREREQUISITE, not deferred** — because a **definition-absent** submission reaches Gate #1 with *only* generated options and no anchor, so with no candidate-body path it would be **unconfirmable**. Until candidate-body retrieval exists, **definition-absent submits are command-denied** ("provide a definition, or wait for hypothesis-only support"). Definition-present requests are unaffected (the anchor is confirmable regardless).

### 3.2 Generation failure / zero candidates (resolves P1-2 / R22)
`alternatives_provenance.generation_status="failed"` (or zero candidates) resolves by case — never a silent pass:
- **Definition present** → **degrade to anchor-only**: drop the alternatives, record `generation_status=failed`, confirm the anchor normally. Fail-soft to the pure definition path.
- **Definition absent** → **retryable park / clarification** (no confirmable option exists). **Concrete lifecycle (resolves P2-1 / R35):** append `CLARIFICATION_REQUESTED` (with a distinct reason, e.g. `field="candidate_generation"`, `kind="generation_failed"`, `blocks_progress=true`) → open a **visible CLARIFICATION gate task** ("we couldn't generate options — retry, or add a definition") → the run holds on that open field/task (the user can see it). **Retry** is the `answer_clarification` command (re-drives generation) or `request_edit` to add a definition. NOT a terminal reject (transient LLM failure must not kill the request) and NOT a command denial (the run already exists). Reuses SP-2's LLM-fail-closed→clarification machinery so there is always a visible, actionable task.

---

## 4. Data model

### 4.1 Schema versioning — Draft, Confirmed, AND Candidate (resolves P1-1 R1, P1-3 R13)
Making `hypothesis` **required** is a **content-schema v2 for ALL THREE doc kinds**: `DRAFT_CONTRACT`, `CONFIRMED_CONTRACT`, **and the candidate docs** (currently `DRAFT_CONTRACT_SCHEMA_VERSION = 1`). Candidate docs must carry the hypothesis + option-identity + provenance, or confirming *from* a candidate (§7.3) loses required audit context.

- Bump the three schema versions to v2. **Fields are partitioned by document role/stage (resolves P1-1 / R31)** — a Draft or unchosen Candidate must NOT carry confirmation-stage fields:
  - **Draft (v2):** `hypothesis_*` fields, `anchor_doc_id`?, `candidate_doc_ids`? (refs only). NO `chosen_*`/`considered_alternatives`/`rationale_for_choice`.
  - **Candidate (v2):** its own option identity (doc id) + body + `provenance` + `hypothesis_*`. NO `chosen_*` (it is not chosen yet).
  - **Confirmed (v2):** `chosen_option_id`, `chosen_source`, `considered_alternatives`, `rationale_for_choice`?, `alternatives_provenance`, `derived_from`, `hypothesis_*`.
- Register a **reader upcaster** (SP-2 Task 2.1 seam) v1 → v2 that **synthesizes role-appropriate defaults for ALL new v2 fields, not only the hypothesis (resolves P2-4 / R38):**
  - all roles: `hypothesis_status="unspecified_pre_assisted_definition"`, `hypothesis_text=null`, `hypothesis_raw_ref=null`, `hypothesis_classification=null`; `definition_raw_ref = raw_input_ref` (the legacy envelope ref, §4.2).
  - Confirmed v1→v2 also: `chosen_source="anchor"`, `chosen_option_id = <the confirmed draft doc id>`, `considered_alternatives=[]`, `rationale_for_choice=null`, `alternatives_provenance={generation_status:"legacy", policy_version:null}`, `derived_from` preserved as-is (already a list in SP-2).
  - Candidate v1→v2: option identity = its own doc id; `provenance` preserved.
- **Schema-validity vs new-write-validity (resolves P1-4 / R24):** the v2 JSON schema must accept BOTH valid shapes via a conditional (`if hypothesis_status == "provided" then hypothesis_text` non-null required; `if == "unspecified_pre_assisted_definition" then hypothesis_text` null allowed) — so the upcaster never emits schema-invalid v2 docs. The stricter rule "a NEW submission must have `hypothesis_status="provided"`" is enforced at the **command/write path** (§5.1), NOT the schema. Two levels: schema = "is this a structurally valid v2 doc"; command = "may this new request proceed".
- **In-flight v1 runs (resolves P2-4 / R19):** runs already submitted / sitting before Gate #1 when v2 ships are **grandfathered** — read-time upcast with the sentinel, confirm as-is (no retroactive hypothesis requirement). Mandatory-hypothesis applies to **new** submissions only. A v1 run *may* add a hypothesis via `request_edit`. No rewrite of docs already downstream in SP-3+.

### 4.2 Input envelope (resolves P1-2 / R12)
SP-2's single `intent_text` becomes a two-text envelope:
- `definition` (optional text) → `definition_raw_ref`, `definition_classification`, `definition_redaction_version`.
- `hypothesis` (**required** text) → `hypothesis_raw_ref`, `hypothesis_classification`, `hypothesis_redaction_version`.
- **Legacy `raw_input_ref` reconciliation (resolves P1-2 / R32):** SP-2's required `raw_input_ref`/`raw_input_classification` are **kept** — `raw_input_ref` becomes the **envelope-level** ref (the whole request), with `definition_raw_ref`/`hypothesis_raw_ref` added as the per-text components; `raw_input_classification` = the **derived effective classification** (below). So `validate_draft`/`assemble_confirmed`/existing read paths keep their required fields intact; the two per-text refs are additive.
- **`request_classification` — a structured object (resolves P2-1 / R25), not a single mapping:**
  `{ definition_screen, hypothesis_screen, combined_scope_screen, prohibited_screen }`, each `{outcome, catalog_version, matched_class}`. `prohibited_screen` = most-restrictive over both texts; `combined_scope_screen` = scope classified over the definition+hypothesis together (§5.3).
- **Derived *effective* classification → fold/MCV/terminal-reject (resolves P2-2 / R36):** the fold's existing single `classification`/`matched_class`/`catalog_version` (which MCV's in-scope check, the terminal-reject path, and the confirm backstop all read) = the **most-restrictive screen** (`prohibited_screen` if any prohibited, else `combined_scope_screen`), i.e. SP-2's existing most-restrictive-wins rule applied over the four screens. Mapping: **MCV in-scope** ← `combined_scope_screen`; **terminal reject** (OUT_OF_SCOPE/PROHIBITED) ← the deciding screen; **state fold `classification`/`matched_class`** ← the deciding screen; **confirm backstop** (§5.3) ← re-run all four. The four-screen object is retained on the contract for audit; the fold reads the one derived value, so the SP-2 state model does not change shape.

### 4.3 Contract content-schema (v2)
- **Hypothesis fields, split (resolves P1-1 / R11):** `hypothesis_text` (redacted LLM-safe string, nullable) + `hypothesis_status` (`"provided" | "unspecified_pre_assisted_definition"`) + `hypothesis_raw_ref` + `hypothesis_classification`. (No sometimes-object.) v2-new requires `hypothesis_status="provided"` + non-null `hypothesis_text`.
- **Option-identity model:** `anchor_doc_id` (definition/Draft doc, else null), `candidate_doc_ids[]`, `chosen_option_id`, `chosen_source ∈ {"anchor","candidate"}`.
- **`derived_from` (resolves P2-4 / R28):** a **list** (matching SP-2's list-shaped provenance), `derived_from: [chosen_option_id]`. The frozen confirmed **document DAG** derives from the same `chosen_option_id`. Anchor/candidate **lineage stays discoverable** through the chain: confirmed → chosen option → (candidate doc `derived_from` [draft] → draft `derived_from` [intake]). So a candidate-sourced confirmation still traces back to the original Draft/intake.
- **`advisory_notes[]`** — critique findings `{note, refers_to_field?, severity, blocks_progress: false}`. Distinct from `open_fields`/`open_questions`.
- **`alternatives_provenance` (resolves P2-2 / R17):** `{policy_version, alternatives_policy, request_alternatives_override, generation_status ∈ {"generated","skipped_policy_off","failed"}, skipped_reason?}` — so auditors can distinguish "no alternatives because policy off" from "generation failed".
- **`considered_alternatives[]` — immutable Gate-#1 evidence (resolves P3-2 / R30):** each entry `{option_id, display_order, score, score_source, score_version, why, doc_content_hash, was_pre_selected}` — the exact order shown, the score's source+version, an immutable content hash of the option doc, and whether it was the pre-selected anchor. This proves what the human actually saw at the gate.
- `rationale_for_choice` — conditional (§7.2).

### 4.4 Server policy vs requester preference (resolves P2-3 round 1 + P2-3 / R27)
Two cleanly separated things:
- **`alternatives_policy`** — **server-side, authoritative** platform config (`{off, on}`, with `policy_version`). Owns the decision. NOT a `submit_intent` input.
- **`request_alternatives`** — a requester **opt-in *preference*** on `submit_intent`. It is **advisory**: honored ONLY when (a) the server policy permits overrides AND (b) the surface accepts it (pre-SP-12 bridge). It can only turn alternatives **ON** (never off — no governance opt-out). It is always **recorded** in `alternatives_provenance.request_alternatives_override` whether honored or not. When SP-12 flips the policy to `on`, `request_alternatives` is a no-op.

---

## 5. Component wiring (reuse SP-2 seams)

| Step | Seam reused | New/changed |
|---|---|---|
| §5.1 Mandatory-hypothesis validation | `submit_intent` command-validation | **denial before run creation** (not a run/reject event) |
| §5.2 Hypothesis on the Draft/Candidate | `_produce_draft` / `generate_candidate_docs` | redacted `hypothesis_text` on the draft so the generator reads it; carried onto candidate docs |
| §5.3 Two-screen classify + prohibited | redactor+egress (P3), `classify_intent` (P2), §8.4 screen (P7) | see below |
| Generate alternatives from H | `CandidateGenerator(draft, catalog, domain)` (P6) | generator reads `draft.hypothesis_text` — seam signature unchanged |
| §5.4 Advisory critique | `contract_review` / `CONTRACT_REVIEW` (P5) | run with **`disposition=advisory`** — findings CANNOT become must-ask blockers |
| Score/route | scoring + Doubt Router (P5) | only Doubt-Router must-ask blocks; critique advisory is separate |
| Gate #1 | `open_gate1_task` (P7) | enriched payload |
| Confirm anchor / adopt candidate | `confirm_contract` / `select_candidate_doc` (P6–7) | §7.3 candidate-adoption path |

**§5.1 (missing hypothesis):** command-boundary validation failure — no `feature_contract`/run created, request id resubmittable. NOT `INTENT_REJECTED`/`RUN_REJECTED`. This is where the **new-write rule** (§4.1 R24) lives: a new submission must carry `hypothesis_status="provided"`.

**§5.1a (minimum-usable redacted hypothesis — resolves P3-1 / R29):** after redaction, if the redacted `hypothesis_text` is empty or below a usefulness threshold (i.e. it was mostly PII/sensitive text that got scrubbed away), the "hypothesis required" rule is not *really* satisfied — generation/critique would be garbage. Treat this as a **retryable command denial / clarification**: "your hypothesis was mostly redacted; please rephrase it without sensitive data." Prevents a technically-present-but-meaningless hypothesis from passing.

**§5.3 (two screens — resolves P1-5 R5 + P3 R20):**
- **Prohibited data class:** block if **EITHER** the definition OR the hypothesis contains a prohibited class — a prohibited data class *anywhere* is fatal (most-restrictive). Example: benign definition "avg transaction value per customer" + hypothesis "…because customers of a certain ethnicity spend more" → **blocked** (prohibited class in H).
- **Banking scope (out-of-scope):** classify the **COMBINED** request (definition + hypothesis together), NOT the hypothesis alone — a valid definition with a loosely-worded business hypothesis must not false-reject. Example: definition "90-day declined-auth count" + hypothesis "these customers churn more" → in-scope (the combined request is a banking feature), even if "churn" read alone looked marginal.
- **Classification order + trust boundary (resolves P1-3 / R23) — load-bearing for privacy AND blocking:** prohibited/scope classification runs **FIRST, in a trusted NON-EGRESS local path over the RAW (by-reference) text.** SP-2's `classify_intent` is **deterministic/catalog-based, not an LLM**, so classifying raw text never violates the no-PII rule (nothing leaves the platform). Redaction runs **after** classification. Only the **redacted** text is ever handed to an LLM (generator/critique). Order is: **classify-on-raw (trusted, local, no LLM) → redact → redacted-only to LLM.** Classifying *after* redaction would be wrong — redaction can scrub the very evidence (e.g. a protected-attribute term) the prohibited screen needs to block on.
- **Confirm-time backstop re-screens BOTH raw refs (resolves P1-3 / R33):** SP-2's §8.4 confirm backstop currently re-screens `_screen_text(draft_body)` (the draft feature-semantics summary). That is insufficient here — a prohibited *hypothesis* with clean-looking draft semantics would slip through. At confirm, re-run the **structured classifier (§4.2) over both `definition_raw_ref` and `hypothesis_raw_ref` with the CURRENT catalog** (same trusted non-egress path). A prohibited class in *either* raw text, or an out-of-scope *combined* request, blocks the confirm — even if the draft body looks clean.

**§5.4 (advisory disposition — resolves P1-5 R15):** the assisted-definition critique invokes `contract_review` with an explicit `disposition=advisory` so findings are emitted as `advisory_notes` (blocks_progress=false) and can **never** be routed into must-ask clarification. This is a distinct disposition at the *source*, not downstream filtering. SP-2's normal (blocking-capable) `CONTRACT_REVIEW` is unaffected.

---

## 6. `intake_mode` truth table — mode is FIXED at submit (resolves P1-3 R3 + P2-4 R9 + P1-4 R14)

`intake_mode` is set at submit (definition present → `definition`, else `hypothesis`) and is **IMMUTABLE** — it never changes at Gate #1, even if a candidate is adopted. The choice is captured only via `chosen_source`/`chosen_option_id`. MCV's calculation-method availability keys off `chosen_source` **at confirm** (anchor → from the Draft; candidate → from the chosen candidate body), NOT a mutated mode.

| Definition | Alt. policy | Generation? | Critique runs? | Gate shows | intake_mode (fixed) | Confirm calc-method source |
|---|---|---|---|---|---|---|
| present | off | no | **no** (pure definition path) | anchor only | definition | Draft (anchor) |
| present | on | yes | **yes** (does D serve H) | anchor (default) + alts + notes | **definition** (unchanged even if a candidate is adopted) | `chosen_source==anchor` → Draft; `==candidate` → that candidate body |
| absent | off | **yes (mandatory)** | **no** (no D to critique) | alts only, none pre-selected | hypothesis | selected candidate |
| absent | on | yes | **no** (no D to critique) | alts only, none pre-selected | hypothesis | selected candidate |

**Critique-runs (resolves P2-2 / R26):** the advisory "does-the-definition-serve-the-hypothesis" critique runs ONLY when a definition is present AND the alternatives policy is on. Policy-off definition requests take the **pure definition path with no critique** (preserves product value + LLM cost as §3 states). Definition-absent requests have no `D` to critique. (A general candidate critique is SP-8/SP-12 scope, out of this addendum.)

**Two competing truths avoided:** the fold/validation continues to key off the fixed `intake_mode`; `chosen_source` records what the human picked without rewriting the mode.

---

## 7. Gate #1 UX + confirm paths

Gate #1 presents the anchor (pre-selected if given), each alternative (score + "why fits H"), and `advisory_notes`. The human confirms ONE option (author-self-confirm, unchanged).

### 7.1 Canonical score (resolves P2-1 / R16)
"Score" = the candidate's **`heuristic_rank`** (0–1, from `candidate_signals`). The **anchor is scored with the same cheap signals** so it is comparable. Definitions:
- **"higher-scoring alternative"** = an alternative whose `heuristic_rank` exceeds the anchor's by **> `tie_threshold`** (default 0.10).
- **Missing/absent score** (either side) → treated as "not higher" → no rationale trigger (fail-open on a governance nicety, never on safety).
- Note: until SP-12 the scores are weak (near-clone candidates), so the higher-scoring trigger is best-effort; it becomes meaningful with the real generator.

### 7.2 Conditional `rationale_for_choice` (resolves P3, round 1)
- One-click confirm stands when the requester confirms the anchor AND there are no `advisory_notes` AND no higher-scoring alternative (per §7.1).
- `rationale_for_choice` is **required** when EITHER `advisory_notes` is non-empty (overriding a flagged concern) OR the human confirms an option while a **higher-scoring alternative** exists (choosing against the scores).

### 7.3 Candidate-adoption confirm path (resolves P2-3 / R18)
When `chosen_source == "candidate"`, `confirm_contract`:
1. **Loads the chosen candidate's body** (requires candidate-body retrieval — the SP-2 6.4/7.5 deferred item; **this path is gated on that being built**),
2. **validates it** (`validate_semantics` + MCV) as the selected contract,
3. **assembles the confirmed contract with `derived_from = chosen_option_id`** (the candidate doc) and calc-method from that candidate body.
When `chosen_source == "anchor"`, confirmation derives from the Draft exactly as SP-2 does today.

### 7.4 Edit invalidation cascade (resolves P1-4 / R34)
Editing the **definition or hypothesis** (via `request_edit`, incl. a v1 run adding a hypothesis) invalidates everything derived from the old text. The edit MUST rebuild, in order:
1. **Reclassify** both texts (§4.2 structured screens) with the current catalog; re-derive the effective classification. A now-prohibited/out-of-scope edit blocks (§5.3).
2. **Re-redact** both texts (new `*_redaction_version`).
3. **Regenerate** the applicable candidates from the new hypothesis (if policy/mode calls for them, §6); **mark the previous candidate set superseded** (a new `candidate_doc_ids` generation; old docs retained immutably but flagged superseded via provenance).
4. **Recompute** the advisory critique against the new (D, H).
5. **Cancel and re-open Gate #1** (mark the prior `considered_alternatives` evidence superseded), and **re-run MCV** on the revised body (per SP-2 Task 7.6's re-validate-before-reopen rule).
No stale candidate/score/advisory-note/gate-evidence may survive an edit. This reuses SP-2's `CONTRACT_REFINED`/refinement + `request_edit` machinery (Tasks 5.5/7.6) extended to regenerate the option set.

---

## 8. Guardrails (as code — non-negotiable)

1. **Anchor-default** — never auto-swap the definition; adopting an alternative is an explicit audited `select_candidate_doc`.
2. **Advisory ≠ blocking** — critique via `disposition=advisory` → `advisory_notes` (blocks_progress=false); only Doubt-Router must-ask blocks.
3. **Missing hypothesis → command-validation denial** (no run), resubmittable; not a terminal reject.
4. **Prohibited data class blocks from either text; scope classifies the combined request** (§5.3).
5. **Server-side alternatives policy** — opt-in only, never opt-out.
6. **`intake_mode` immutable** after submit; choice recorded via `chosen_source`.
7. **Recorded choice + policy provenance** — option-identity + `alternatives_provenance` + considered set + confirmer + conditional rationale.
8. **No PII to the LLM** — both texts redacted, raw by-reference.
9. **Author-self-confirm** — unchanged.

---

## 9. Non-goals

Not a new *declared* mode (fixed at submit, §6); no second signer (Gate #2/SP-5); no execution/grounding (SP-3+); not the real generator (SP-12); no auto-adoption of a "better" alternative; no requester opt-OUT of governance-required behaviour.

---

## 10. Testing strategy (deterministic, FakeLLM)

- Missing hypothesis → command-validation denial, no `feature_contract` created, resubmittable.
- Prohibited data class in the **hypothesis** + benign definition → blocked; scope screen on the **combined** request does not false-reject a valid-definition + loose-hypothesis.
- Both texts redacted before the (fake) LLM; PII in either never reaches it; raw only by-reference.
- Policy ON, D + H → anchor + N scripted alternatives + critique in `advisory_notes` (NOT `open_fields`; MCV/Gate not blocked).
- `intake_mode` fixed: a definition request that adopts a candidate keeps `intake_mode="definition"`; `chosen_source="candidate"`, `derived_from=candidate_doc_id`, calc-method from the candidate body.
- Candidate-adoption loads + validates the candidate body (gated on candidate-body retrieval).
- Anchor pre-selected; one-click when no notes & no higher-scoring alt; `rationale_for_choice` required iff §7.2; "higher-scoring" per §7.1 (tie_threshold, missing-score → no trigger).
- `alternatives_provenance` distinguishes generated / skipped_policy_off / failed.
- Policy OFF + definition present → no generation; policy OFF + definition absent → generation still runs (§6).
- v1 doc (incl. an in-flight pre-Gate run) upcasts to v2 with the sentinel and stays readable/confirmable.
- Candidate docs are v2 and carry hypothesis + option-identity + provenance.
- All hermetic via the existing intake FakeLLM harness.

---

## 11. Decisions register

- **D1 — Hypothesis mandatory (new submits):** command-validation denial on absence; schema v2 (§4.1); in-flight v1 grandfathered.
- **D2 — Alternatives always-on for definition requests:** server-side policy, default off/opt-in until SP-12; generation-when-no-definition always runs.
- **D3 — `intake_mode` fixed at submit, immutable;** choice via `chosen_source`/`chosen_option_id`.
- **D4 — AI advisory only** via `disposition=advisory` + `advisory_notes`; never blocks/auto-approves; anchor never auto-swapped.
- **D5 — Everything recorded** (option-identity + `alternatives_provenance` + considered set + confirmer + conditional rationale); `derived_from = chosen_option_id`.
- **D6 — Hypothesis PII/prohibited-governed:** redacted + by-reference + classified; prohibited-from-either-text, scope-from-combined.
- **D7 — Candidate adoption** loads + validates the candidate body and derives from it (gated on candidate-body retrieval).

---

## 12. Path to build

1. Finish SP-2 (in flight), review, merge.
2. Take this doc through: brainstorm-confirm (done, incl. review rounds 1+2) → `writing-plans` → subagent-driven build.
3. Land D1 (mandatory hypothesis + schema v2 + upcaster + input-envelope) + the policy-gated flow first; the candidate-adoption path (D7) depends on candidate-body retrieval; flip D2 when SP-12 is ready.

---

## 13. Open items for the plan phase

- `advisory_notes` schema + gate rendering of severity; the `disposition=advisory` parameter on `contract_review`.
- `alternatives_policy` config surface (global vs per-tenant/per-domain) + `policy_version` source.
- Candidate-body retrieval mechanism (event-inline vs object-store) — prerequisite for D7 (see SP-2 6.4/7.5 note).
- `tie_threshold` default + whether the anchor's cheap-signal scoring is computed at gate time.
- Whether `request_alternatives` opt-in is exposed on all three surfaces (UI/API/CLI) or UI-only during the bridge.

---

## 14. Review resolutions (findings → fixes)

**Round 1 (R1–R10):**
- R1 (not additive): §4.1 schema v2 + upcaster; v1 grandfathered.
- R2 (not terminal reject): §5.1 command-validation denial, resubmittable.
- R3 (bridge vs single-path): §3/§6 policy gates alts-alongside-definition; no-definition always generates; truth table.
- R4 (generate from H): §5.2 redacted hypothesis on the Draft; seam unchanged.
- R5 (hypothesis PII/prohibited): §5.3 redacted+by-ref+classified+screened.
- R6 (advisory vs open_questions): §4.3/§5.4 `advisory_notes` channel.
- R7 (option identity): §4.3 anchor/candidate/chosen_option/chosen_source/derived_from.
- R8 (flag bypass): §4.4 server-side policy; opt-in only.
- R9 (mode truth table): §6.
- R10 (record-why vs one-click): §7.2 conditional rationale.

**Round 2 (R11–R20):**
- R11 (P1-1 v2 hypothesis shape): §4.3 split fields + `hypothesis_status` enum (no sometimes-object).
- R12 (P1-2 input envelope): §4.2 `definition_raw_ref`/`hypothesis_raw_ref` + per-text classification/redaction + combined `request_classification`.
- R13 (P1-3 candidate docs v2): §4.1 candidate docs also v2 (hypothesis + option-identity + provenance).
- R14 (P1-4 mode immutable): §6 `intake_mode` fixed at submit; choice via `chosen_source`; MCV keys off `chosen_source` at confirm.
- R15 (P1-5 advisory mode): §5.4 explicit `disposition=advisory` at the source (cannot block), not downstream routing.
- R16 (P2-1 score): §7.1 canonical score = `heuristic_rank`; anchor scored comparably; `tie_threshold`; missing → no trigger; weak-until-SP-12.
- R17 (P2-2 policy audit): §4.3 `alternatives_provenance` (policy_version, override, generation_status, skipped_reason).
- R18 (P2-3 candidate adoption): §7.3 load + validate candidate body, derive from candidate doc; gated on candidate-body retrieval.
- R19 (P2-4 in-flight v1): §4.1 in-flight pre-Gate runs grandfathered (sentinel), confirm as-is; new submits require hypothesis.
- R20 (P3 scope false-reject): §5.3 prohibited-from-EITHER-text (fatal anywhere); scope from the COMBINED request; examples.

**Round 3 (R21–R30):**
- R21 (P1-1 unconfirmable without body retrieval): §3.1 candidate-body retrieval is a first-build prerequisite; definition-absent submits command-denied until it exists.
- R22 (P1-2 generation failure): §3.2 present→degrade to anchor-only; absent→retryable park/clarification; never silent pass.
- R23 (P1-3 classify/redact order): §5.3 classify-on-raw in a trusted non-egress local path (deterministic, no LLM) → redact → redacted-only to LLM.
- R24 (P1-4 schema vs write validity): §4.1 v2 schema accepts both shapes (if/then on hypothesis_status); command path enforces provided-for-new.
- R25 (P2-1 request_classification shape): §4.2 structured object {definition_screen, hypothesis_screen, combined_scope_screen, prohibited_screen}.
- R26 (P2-2 critique-runs): §6 column — critique only when definition present AND policy on.
- R27 (P2-3 request_alternatives separation): §4.4 server policy authoritative; requester `request_alternatives` = advisory on-only preference, recorded in provenance.
- R28 (P2-4 derived_from/DAG): §4.3 `derived_from: [chosen_option_id]` (list); doc DAG derives from same; lineage discoverable via candidate→draft→intake.
- R29 (P3-1 unusable redacted hypothesis): §5.1a minimum-usable check → retryable denial/clarification.
- R30 (P3-2 considered_alternatives evidence): §4.3 {option_id, display_order, score, score_source, score_version, why, doc_content_hash, was_pre_selected}.

**Round 4 (R31–R38):**
- R31 (P1-1 stage-specific schema): §4.1 v2 fields partitioned by doc role — Draft (hypothesis + refs), Candidate (option identity + body + provenance), Confirmed (chosen/considered/rationale).
- R32 (P1-2 raw_input_ref reconciliation): §4.2 legacy `raw_input_ref` kept as envelope ref; `definition_raw_ref`/`hypothesis_raw_ref` additive; `raw_input_classification` = derived effective classification.
- R33 (P1-3 confirm-time two-text classify): §5.3 confirm backstop re-screens BOTH raw refs (not draft_body) with the current catalog.
- R34 (P1-4 edit invalidation): §7.4 edit D/H → reclassify + re-redact + regenerate candidates + recompute critique + cancel/reopen Gate #1 + supersede old evidence.
- R35 (P2-1 generation-failure lifecycle): §3.2 append CLARIFICATION_REQUESTED + open visible gate task + blocked field; retry via answer_clarification/request_edit.
- R36 (P2-2 classification→fold/MCV mapping): §4.2 derived effective classification (most-restrictive screen) feeds fold/MCV/terminal-reject; four-screen object retained for audit; state shape unchanged.
- R37 (P2-3 diagram vs truth table): §1/§2 critique marked conditional (D present AND policy on) to match §6.
- R38 (P2-4 upcaster all fields): §4.1 v1→v2 upcaster synthesizes role-appropriate defaults for ALL new fields (option identity, provenance, considered, raw-ref shape), not only hypothesis.
