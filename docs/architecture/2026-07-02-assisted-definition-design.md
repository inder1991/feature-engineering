# Assisted Definition — Design Spec (SP-2 addendum)

**Status:** Designed / agreed in dialogue (2026-07-02). Not yet planned or built.
**Relationship:** An addendum to SP-2 (`2026-07-01-sp2-intake-clarification-design.md`). Builds on SP-2's seams; the *quality* half depends on SP-12 (the real Feature Generation engine). Ships as a follow-up after SP-2 is merged — it must NOT destabilise the in-flight SP-2 branch.

---

## 1. Summary

Every feature request must carry a **hypothesis** (the "why"). The platform builds the requester's **definition** (the "what", if given) as the anchor, and — for discovery + a governance cross-check — **generates scored alternative definitions from the hypothesis** and runs an **advisory AI critique** ("does the definition actually serve the hypothesis?"). At **Human Gate #1** the requester sees their definition (pre-selected) alongside the scored alternatives and the critique notes, and confirms exactly one. The confirmed contract records the **full set considered** + the choice + who + why.

This collapses the definition-vs-hypothesis "mode" question into a **single flow**: the *definition*, when provided, is simply the pre-selected anchor among the options.

### Authority model (unchanged from SP-2)
LLM **suggests/critiques** → platform **validates/enforces** → human **confirms** → registry **governs**. The AI never decides, never auto-approves, never silently swaps the anchor. No PII/raw data ever reaches the LLM (SP-2's redactor + egress guard already enforce this).

---

## 2. The agreed flow (single path — no "opted-in NO" branch)

```
                 DATA SCIENTIST
        ┌──────────────────────────────────┐
        │ Hypothesis H   ← MANDATORY        │
        │ Definition D   ← optional         │
        └────────────────┬─────────────────┘
                         │ submit_intent
                         │  (no hypothesis → REJECTED, fail-closed: "state why first")
                         ▼
        ┌──────────────────────────────────┐
        │ ALWAYS:                           │
        │  • anchor on D (if given)         │
        │  • generate alternatives A1..An   │  (from H, via the candidate seam)
        │  • AI critique (advisory only)    │  (CONTRACT_REVIEW: does D serve H?)
        └────────────────┬─────────────────┘
                         ▼
        ┌───────────────────────────────────────────┐
        │              GATE #1   (always)            │
        │   • D   ◀ pre-selected default (if given)  │
        │   • A1, A2, …   (scored + why)             │
        │   • AI notes ("D counts declines but H is  │
        │      about spending shifts — intended?")   │
        │            the human confirms ONE          │
        └───────────────────┬────────────────────────┘
                            ▼
                  CONFIRMED CONTRACT
            (records: chosen one + all considered + why + confirmer)
                            ▼
                    SP-3 → SP-4 → SP-5
```

**Definition present** → it is the pre-selected anchor; alternatives sit around it.
**Definition absent** → no anchor default; the human picks from the generated alternatives (this is today's hypothesis-mode behaviour).

---

## 3. Scope & staging (the recommendation, with the flow above as the target)

The flow above is the **target shape**. Its two policy levers ship on **different schedules**, because one is cheap governance and the other depends on generation *quality*:

| Lever | Ship when | Why |
|---|---|---|
| **Hypothesis mandatory** (fail-closed on missing) | **Now** (post-SP-2, cheap) | Pure governance/MRM win — every feature gets a documented rationale. No generation cost. |
| **Alternatives always generated** (no opt-in) | **After SP-12** | Always-on alternatives only help when they're *good*. With SP-2's stub generator the alternatives are near-clones; forcing weak alternatives onto every request trains users to ignore them. |

**Bridge until SP-12:** the always-generate behaviour lives behind a **feature flag** (`always_offer_alternatives`) that defaults **off / opt-in** while the generator is a stub, and is **flipped on** once SP-12 lands. The *flow, schema, gate UX, audit, and guardrails are all built to the target (always-on) shape from day one* — only the flag's default changes. No re-wiring when SP-12 arrives; swap the generator behind the existing seam and flip the flag.

> Net: mandatory hypothesis is a governance win to take immediately; always-on alternatives is a win to take once they're worth showing. The single-path flow is designed once, for the end state.

---

## 4. Data-model deltas (small, additive)

- **`submit_intent` inputs:**
  - `hypothesis: str` — **required**; empty/absent → fail-closed reject with a clear "state the rationale" message (this is the mandatory-hypothesis lever).
  - `definition` — optional (unchanged); when present it becomes the anchor.
  - `always_offer_alternatives: bool` — feature flag (see §3); default per SP-12 readiness.
- **Contract content-schema:**
  - `rationale` / `hypothesis` field — captured on **every** contract (even when alternatives aren't generated), as first-class MRM documentation.
  - `considered_alternatives` block in the confirmation record — the anchor + each generated alternative (id, score, why) + the chosen one + rationale-for-choice. (Extends SP-2's existing selected/rejected confirmation record.)
- **No new events required** — reuse SP-2's candidate docs (`generate_candidate_docs`) and the `CONTRACT_CONFIRMED` confirmation record. `derived_from` provenance links the confirmed contract to the option it came from.

---

## 5. Component wiring (reuse SP-2 seams — do NOT build new governance)

| Step | Existing seam reused |
|---|---|
| Mandatory-hypothesis validation | `submit_intent` input validation (fail-closed) |
| Build the anchor (definition) | SP-2 definition path (`_produce_draft`) |
| Generate alternatives from H | `generate_candidate_docs` / `CandidateGenerator` (Phase 6) — stub now, SP-12 later |
| Advisory AI critique ("does D serve H?") | `contract_review` / Critique `CONTRACT_REVIEW` (Phase 5) → findings as `open_questions`/notes |
| Score + route the options | scoring + Doubt Router (Phase 5) |
| Gate #1 with anchor + options + notes | `open_gate1_task` (Phase 7) — enriched payload |
| Confirm anchor OR adopt an alternative | `confirm_contract` / `select_candidate_doc` (Phases 6–7) |
| No PII to LLM | redactor + egress guard (Phase 3) — inherited free |
| Full audit | event-sourced throughout — inherited free |

The only genuinely new code is: the mandatory-hypothesis validation, the `always_offer_alternatives` gating, the enriched Gate #1 payload, and the `considered_alternatives` record. Everything else is composition.

---

## 6. Guardrails (as code — non-negotiable)

1. **Anchor-default.** The requester's definition is the pre-selected default. Adopting an alternative is an explicit `select_candidate_doc` command (an audited switch), never a silent swap.
2. **Advisory, not authoritative.** Critique findings + candidate scores are `open_questions`/notes routed through the Doubt Router's advisory channel — they can **never** auto-block or auto-approve. The AI has no ground truth for "is this a good feature"; the human decides.
3. **Fail-closed on missing hypothesis.** No hypothesis → reject at submit; never proceed without a rationale.
4. **Cost-gate (until always-on).** While the flag is off/opt-in, generation fires only when requested; with no hypothesis there is nothing to generate from anyway.
5. **Recorded choice.** The confirmed contract records the full considered set + the chosen option + why + confirmer identity.
6. **No PII to the LLM.** Alternatives + critique run through SP-2's redacted, egress-guarded envelope.
7. **Author-self-confirm.** Gate #1 confirmer is the authenticated human requester (SP-2 Decision 2) — unchanged.

---

## 7. Gate #1 UX (enriched, not re-architected)

Gate #1 presents:
- the **anchor definition** (pre-selected if provided),
- each **generated alternative** with its score and a one-line "why this might capture H",
- the **critique notes** (e.g. "your definition counts declined authorisations, but your hypothesis is about spending-category shifts — is that intended?").

The human **confirms one**. Confirming the anchor is a one-click path for a confident author (no forced choosing). The gate remains an *audited intent lock*, not a compliance approval.

---

## 8. SP-12 boundary

- **Now (on SP-2):** the whole flow, schema, gate UX, audit, and guardrails are buildable — using the **stub generator** (weak, near-clone alternatives). Honest limitation, flag-gated off/opt-in.
- **SP-12:** replace the stub behind the `CandidateGenerator` seam with the real engine (router + specialists + memory + symbolic synthesis + few-shot). Alternatives become genuinely good; flip `always_offer_alternatives` on. **No re-wiring** — same seam.

---

## 9. Non-goals

- Not a new intake *mode* — it unifies the existing two; `intake_mode` becomes "did you also give a definition to anchor on?".
- No second signer here — independent validation stays at Gate #2 / SP-5.
- No execution / grounding / materialisation — the output is still a *specification* (SP-3+ owns the rest).
- Not the real generator — quality is SP-12.
- No auto-adoption of a "better" alternative — always an explicit human choice.

---

## 10. Testing strategy (deterministic, FakeLLM)

- Missing hypothesis → submit rejected (fail-closed).
- With the flag on: a definition + hypothesis → anchor built + N scripted alternatives generated + critique findings present as advisory `open_questions`.
- Anchor is the pre-selected default; confirming it is definition-style; adopting an alternative is an explicit, recorded `select_candidate_doc` switch.
- Advisory findings never change status / never block / never auto-approve.
- `considered_alternatives` provenance recorded on the confirmed contract.
- With the flag off: no generation runs (cost-gate); plain definition path unchanged.
- All hermetic via the existing intake FakeLLM harness; no PII reaches the (fake) LLM.

---

## 11. Decisions register

- **D1 — Hypothesis mandatory:** yes, immediately (cheap governance). Fail-closed on absence.
- **D2 — Alternatives always-on:** target yes; gated behind `always_offer_alternatives`, default off/opt-in until SP-12 quality lands, then on. Flow designed for the always-on end state.
- **D3 — Definition optional; when present = pre-selected anchor.** Mode question dissolves into "anchor present?".
- **D4 — AI is advisory only** (suggest/critique), never authoritative. Anchor never auto-swapped.
- **D5 — Everything recorded** (considered set + choice + rationale + confirmer); provenance via `derived_from`.

---

## 12. Path to build

1. Finish SP-2 (in flight), review, merge.
2. Take this doc through the normal discipline: brainstorm-confirm (largely done here) → **implementation plan** (`writing-plans`) → **subagent-driven build**.
3. Land D1 (mandatory hypothesis) + the flag-gated flow first; flip D2 on when SP-12 is ready.
