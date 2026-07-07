# Governed Feature-Contract Flow — Design

**Status:** shape + decisions approved (2026-07-07) — ready for the implementation plan · **Date:** 2026-07-07

## 1. Problem

The platform has a hardened, tested **feature-contract governance engine** in the backend
(`considered-set → draft → confirm → INSERT INTO contract`), but it is **orphaned**:

- **No UI** drives it — zero frontend calls to `/contract/*`. The Workbench registers features via
  `POST /features` (`registerFeature`), which writes a `feature` row with **no contract at all**.
- The **contract model is thin** — it stores `feature_name, definition (narrative), version, join_path,
  intent_id, verification`, missing the governing spec fields (grain, point-in-time rule, lookback,
  calculation method, target, approver).
- There is **no human-approval flow** — the "brief" (`submit_intent`) is recorded and immediately used,
  never approved; and `target_ref` (the leakage-safety anchor) is client-supplied, optional, and
  un-assisted, so in practice it is never set.
- `POST /features` stamps `DESIGN-CHECKED` (the "gauntlet passed" seal) with **no gauntlet** — a false
  attestation (prior finding #4).

**Net:** every feature registered through the product is registered *contract-less and un-governed*,
while the governed engine sits unused.

## 2. Goal

Make the governed, human-in-the-loop feature-contract flow the **real** registration path in the
product: two human gates, an enriched (but scoped) contract, and assisted — never autonomous — setting
of the safety-critical target. Close findings #3 (frontend auth on the real path), #4 (honest stamp),
and #5 (contract trust) as a by-product.

## 3. Governing principles (non-negotiable)

1. **LLM proposes; deterministic code and humans dispose.** The LLM never has final authority over a
   safety control and never *validates* something that needs data.
2. **The gauntlet stays deterministic.** Leakage / freshness / point-in-time are code checks, not LLM
   opinions. Leakage = a set-membership test (`target_ref ∈ derives?`).
3. **No predictiveness validation.** "Does this feature support the target" is empirical → needs data
   → **out of scope** (no data plane post-2026-07-04 pivot). LLM output stays an honestly-caveated fit
   suggestion, never a performance claim.
4. **The seal must be earned.** `DESIGN-CHECKED` only ever comes from a path that ran the gauntlet.

## 4. The flow (target)

```
Gate 1 — approve the BRIEF                 (human)
  hypothesis  +  assisted target column (LLM proposes, human confirms)  +  data scope
        │
        ▼
Generation — considered set                (deterministic router + LLM + gauntlet)
  anchor (from the definition, if given) + alternatives, EVERY option gauntlet-passed
        │
        ▼
Gate 2 — approve the FEATURE(S)            (human)
  pick from the considered set  →  mints the enriched, governed CONTRACT (earns DESIGN-CHECKED)
```

Two human moments only: **approve the brief**, then **approve the features**. Picking a feature *is*
how its contract is created — they are not separate gates.

### 4.1 Gate 1 — approve the brief (NEW checkpoint)

The brief = `{hypothesis (mandatory), definition (optional anchor), target_ref, catalog scope}`.

- **Assisted target resolution (the sound half).** An LLM reads the **hypothesis** (not the feature
  definition — the definition is the predictor, the hypothesis names the outcome) and **proposes a
  target column**, constrained to columns that actually exist in the catalog (grounded — no
  hallucinated targets). If ambiguous it offers options + expresses uncertainty. **The human confirms**
  the target. This feeds the deterministic leakage gate; the LLM is never the final authority.
- The reviewer approves the brief (or returns feedback → the `feedback` param already threads into
  generation). Approval is recorded (audit).

### 4.2 Generation — the considered set (mostly built)

`build_considered_set` already produces the anchor + gauntlet-passed alternatives + an advisory
recommendation, and persists the snapshot. Reused as-is. The confirmed `target_ref` from Gate 1 now
reliably arms the leakage gate.

### 4.3 Gate 2 — approve features → mint the contract (wire + enrich)

The human picks from the considered set; `confirm_contract` (hardened) governs the pick and mints the
**enriched** contract (§5), stamped `DESIGN-CHECKED` (earned — the gauntlet ran).

## 5. The contract model — enrich to governance/safety fields ONLY

**In scope** (what makes it a real, examiner-ready contract):

| Field | Source |
|---|---|
| `target_ref` | Gate-1 confirmed target |
| `feature_grain` | `feature.grain_table` (+ grain columns) — already captured |
| point-in-time rule / `as_of_column` | `feature.as_of_column` (+ an explicit rule text) |
| `lookback_window` | **new** — captured at draft/confirm |
| `calculation_method` (chosen) | `feature.aggregation` + a method label |
| `hypothesis` | `contract_intent` via `intent_id` — already linked |
| `approved_by` + timestamp | Gate-2 actor (**new** as a first-class field) |

**Out of scope (data-plane / mapping — YAGNI):** `entity_table`, `entity_key_column`, physical column
bindings, compilation/execution fields, "considered calculation methods" bookkeeping. The pivot cut the
data plane; do not build storage for a compute engine that does not exist.

### 5.1 Decision A — store vs. assemble → **ASSEMBLE** (decided 2026-07-07)
The spec sheet is **assembled as a read-time view** over `feature` + `contract_intent` + `contract`.
Only the genuinely-new facts are **stored** on the contract: `lookback_window`, explicit PIT-rule text,
calculation-method label, `approved_by` + timestamp. Grain / aggregation / derives are **never
duplicated** onto the contract — the view reads them from `feature`, so the two can't drift apart.

## 6. Decision B — same-approver vs. four-eyes → **CONFIGURABLE, default same-approver** (decided 2026-07-07)

The two gates default to **same-approver** (analyst writes the brief, generates, approves the features —
the 2026-07-04 pivot retired heavy four-eyes). A deployment flag (e.g. `FEATUREGEN_CONTRACT_FOUR_EYES=1`)
turns on **four-eyes** for regulated installs: the Gate-2 confirmer must be a **distinct** identity from
the Gate-1 brief actor, enforced server-side (a 4xx if the same subject tries both). The approver
identity is recorded either way, so the audit trail holds in both modes.

## 7. UI (the integration — closes #3/#4/#5)

The Workbench becomes the two-gate flow instead of quick-register:

1. **Brief screen** — hypothesis + optional definition; the assisted-target picker (proposed column +
   confirm); catalog scope → **Approve brief** (Gate 1).
2. **Considered-set screen** — the anchor + alternatives, each shown as *gauntlet-passed*, with the
   advisory recommendation → pick one/some.
3. **Confirm screen** — review the assembled contract sheet → **Confirm** (Gate 2) → contract minted.

Wire `frontend/src/api.ts` to `/contract/considered-set`, `/contract/draft`, `/contract/confirm`,
`/contracts` using the **real Bearer session** (resolves #3 on this path). Deprecate the direct
`POST /features` registration in the Workbench; keep the endpoint but stamp it `UNVERIFIED` (#4) so the
governed flow is the only path that earns `DESIGN-CHECKED`.

## 8. Build phases (dependency order — detailed plan comes later)

1. **Enrich the contract model** (§5) — the assembled spec-sheet view + the few new stored fields +
   `POST /features` → `UNVERIFIED` default; `confirm_contract` explicitly stamps `DESIGN-CHECKED`.
2. **Assisted target resolution** (§4.1) — grounded LLM proposal + confirm endpoint; arm the leakage
   gate from the confirmed target.
3. **Gate 1 as a real checkpoint** — record brief approval (+ the same-approver/four-eyes flag).
4. **UI** (§7) — the three screens on real Bearer auth; deprecate direct-register in the Workbench.

## 9. Explicit non-goals

- No data-plane compute, no feature-value serving, no training-set generation.
- No predictiveness / "feature supports target" validation.
- No runtime/proxy leakage detection (needs data) — only design-time direct-leakage (set membership).
- No mapping/compilation/execution layer.

## 10. Decisions (resolved 2026-07-07)

- **A — store vs. assemble** the contract spec sheet → **assemble + store-the-few** (§5.1).
- **B — same-approver vs. four-eyes** → **configurable, default same-approver** (§6).
