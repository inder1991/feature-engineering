# Governed Feature-Contract Flow — Design

**Status:** v2 — shape + decisions approved, review fixes folded in (2026-07-07) · ready for the
implementation plan · **Date:** 2026-07-07

> **v2 changelog** (from a 4-lens review — architect / product / data-scientist / critic):
> - **Correctness:** the target is a *set of source columns*, not one column, and the leakage test runs
>   against that set (§5). Safety-critical fields are **snapshotted** onto the contract at confirm, not
>   assembled from a live table (§6.1). Four-eyes requires a `feature:approve` *authority*, not just a
>   distinct person (§7).
> - **Paths added:** the `UNVERIFIED → governed` lifecycle + transition of existing features (§8), and
>   the unhappy paths (confirm-failure, stale target/set) (§9). Batch approve + surfacing rejects (§4.3).

## 1. Problem

The platform has a hardened, tested **feature-contract governance engine** in the backend
(`considered-set → draft → confirm → INSERT INTO contract`), but it is **orphaned**:

- **No UI** drives it — zero frontend calls to `/contract/*`. The Workbench registers features via
  `POST /features` (`registerFeature`), which writes a `feature` row with **no contract at all**.
- The **contract model is thin** — `feature_name, definition (narrative), version, join_path,
  intent_id, verification` — missing the governing spec fields (grain, PIT rule, lookback, calc method,
  target, approver).
- There is **no human-approval flow** — the "brief" (`submit_intent`) is recorded and immediately used,
  never approved; and `target_ref` (the leakage-safety anchor) is client-supplied, optional, and
  un-assisted, so in practice it is never set.
- `POST /features` stamps `DESIGN-CHECKED` with **no gauntlet** — a false attestation (finding #4).

**Net:** every feature registered through the product is registered *contract-less and un-governed*,
while the governed engine sits unused.

## 2. Goal

Make the governed, human-in-the-loop feature-contract flow the **real** registration path: two human
gates, an enriched (but scoped) contract, and assisted — never autonomous — setting of the
safety-critical target. Closes findings #3 (frontend auth on the real path), #4 (honest stamp), #5
(contract trust).

## 3. Governing principles (non-negotiable)

1. **LLM proposes; deterministic code and humans dispose.** The LLM never has final authority over a
   safety control and never *validates* something that needs data.
2. **The gauntlet stays deterministic.** Leakage / freshness / PIT are code checks. Leakage = a
   **set-intersection** test: `derives ∩ target_source_columns ≠ ∅` (see §5 — *not* a single column).
3. **No predictiveness validation.** "Does this feature support the target" is empirical → needs data →
   **out of scope** (no data plane). LLM output stays an honestly-caveated fit suggestion, never a
   performance claim — **and the UI must say so**, so "governed" is never mistaken for "validated".
4. **The seal must be earned.** `DESIGN-CHECKED` only ever comes from a path that ran the gauntlet.

## 4. The flow (target)

```
Gate 1 — approve the BRIEF                 (human)
  hypothesis + assisted TARGET (its source columns; LLM proposes, human confirms) + data scope
        │
        ▼
Generation — considered set                (deterministic router + LLM + gauntlet)
  anchor (from the definition, if given) + alternatives, EVERY option gauntlet-passed
        │
        ▼
Gate 2 — approve the FEATURE(S)            (human; batch)
  pick from the considered set  →  mints an enriched, governed CONTRACT each (earns DESIGN-CHECKED)
```

Two human moments only: **approve the brief**, then **approve the features**. Picking a feature *is* how
its contract is created — not a separate gate.

### 4.1 Gate 1 — approve the brief (NEW checkpoint)

The brief = `{hypothesis (mandatory), definition (optional anchor), target (§5), catalog scope}`.

- **Assisted target resolution.** An LLM reads the **hypothesis** (not the feature definition — the
  definition is the predictor, the hypothesis names the outcome) and **proposes the target's source
  columns** (§5), grounded to columns that exist in the catalog. If ambiguous it offers options + states
  uncertainty. **The human confirms.** The LLM is never the final authority on the safety anchor.
- The reviewer approves the brief (or returns feedback → the existing `feedback` param threads into
  generation). Approval + approver identity are recorded (audit).

### 4.2 Generation — the considered set (mostly built)

`build_considered_set` already produces the anchor + gauntlet-passed alternatives + an advisory
recommendation, and persists the snapshot. Reused. The confirmed **target set** from Gate 1 now
reliably arms the leakage test for every candidate.

### 4.3 Gate 2 — approve features → mint contracts (wire + enrich)

- **Batch:** a hypothesis yields several features; the human may approve **multiple** from the
  considered set at once — each mints its own enriched contract (preserve the Workbench selection tray).
- **Show the rejects:** surface the gauntlet's *rejected* candidates + reasons ("considered
  `avg_balance_next_30d`, rejected: leakage") — builds trust and teaches; the data already exists in the
  `rejections` payload.
- On confirm, `confirm_contract` (hardened) re-runs the MCV against the **live** graph and mints the
  contract, stamped `DESIGN-CHECKED` (earned). Failure paths in §9.

## 5. The target is a SET of source columns (leakage correctness — must-fix #1)

Real targets are usually *defined*, not a raw column: *"churn = **no transaction for 90 days** after
as_of"* is computed from `transactions.date`, not a column named `churned`.

- **Model:** the target is `{ name, definition_text, source_columns: [(catalog_source, object_ref), …] }`.
  For a raw-column target the source set is just that one column; for a derived label it is every column
  the definition reads.
- **Leakage test (deterministic):** reject a feature if **any** of its derives is in the target's source
  set — `derives ∩ target_source_columns ≠ ∅`. This catches the case the single-column test misses (a
  feature reading `transactions.date`, which *defines* churn, would leak even though it never reads
  `churned`).
- **Assist:** the LLM proposes the source columns from the hypothesis; the human confirms the set.
- **Honest limit:** still design-time / **direct** leakage only. Proxy leakage (a *different* column
  statistically near-identical to the label) needs data and stays out of scope (§12).

## 6. The contract model — enrich to governance/safety fields only

**In scope** (what makes it a real, examiner-ready contract):

| Field | Origin |
|---|---|
| `target` (name, definition, source columns) | Gate-1 confirmed target (§5) |
| `feature_grain` | `feature` at confirm |
| point-in-time rule + `as_of_column` | `feature.as_of_column` + explicit rule text |
| `lookback_window` | new — captured at draft/confirm |
| `calculation_method` (chosen) | `feature.aggregation` + a method label |
| `derives_from` | `feature` at confirm |
| `hypothesis` | `contract_intent` via `intent_id` |
| `approved_by` + timestamp | Gate-2 actor |

**Out of scope (data-plane / mapping — YAGNI):** `entity_table`, `entity_key_column`, physical column
bindings, compilation/execution fields. The pivot cut the data plane; do not store for a compute engine
that does not exist.

### 6.1 Decision A (revised) — **SNAPSHOT the safety-critical fields; assemble only the static**

*Correction from v1's pure "assemble".* A contract is an **immutable, versioned attestation** — it must
be point-in-time. If it assembled grain/derives from the *live* `feature` row, a later re-confirm (v2)
would retroactively change what v1 claims. So:

- **Snapshot at confirm** (frozen onto the contract row/JSON): `target` + source columns, `feature_grain`,
  `as_of_column` + PIT rule, `lookback_window`, `calculation_method`, `derives_from`. These are the
  fields a governance record must freeze.
- **Assemble at read time** only truly-static reference data (e.g. the `hypothesis` text via `intent_id`,
  the human-readable feature name) — things that don't change the attestation's meaning.

Net: the contract is a self-contained, immutable sheet; the *live* `feature` row can evolve without
rewriting history.

## 7. Approval modes — same-approver vs. four-eyes (must-fix #3)

Default **same-approver** (the 2026-07-04 pivot retired heavy four-eyes). `FEATUREGEN_CONTRACT_FOUR_EYES=1`
turns on **four-eyes** for regulated installs, which requires BOTH:

1. the Gate-2 approver holds a **`feature:approve`** permission — a *new* capability, distinct from
   `feature:generate`, so approval is a real segregation of duty (a lead/approver role), not two peers
   rubber-stamping; **and**
2. the Gate-2 approver is a **distinct subject** from the Gate-1 brief actor (server-enforced 4xx).

In same-approver mode `feature:approve` is implied by `feature:generate`. Approver identity is recorded
in both modes. RBAC change: add `feature:approve`; grant it to `platform_admin` and a
(new/assigned) approver role; keep it off `feature_engineer` in four-eyes deployments.

## 8. Feature lifecycle: `UNVERIFIED → governed` + transition (path design)

Full governance for *every* throwaway feature is too much friction — people will route around it. So:

- **Fast path stays, honestly labelled.** `POST /features` still exists for quick/exploratory
  registration, but stamps **`UNVERIFIED`** (never `DESIGN-CHECKED`). Fine for exploration.
- **Promote to governed.** An `UNVERIFIED` feature can be taken **through the two gates** (brief → set →
  confirm) to earn `DESIGN-CHECKED` and a contract when it matters (e.g. before a model uses it).
- **Verification vocabulary:** `UNVERIFIED` (default for direct registration) · `DESIGN-CHECKED`
  (gauntlet passed via the governed flow). Add a `CHECK` constraint on the column so the set is closed.
- **Transition of existing features (migration).** Every feature registered before this change is
  contract-less yet stamped `DESIGN-CHECKED` — a *false* attestation today. A one-time migration
  **re-stamps all contract-less features `UNVERIFIED`** (honest), leaving governed ones untouched. Users
  promote what they still trust. Log the count re-stamped.

## 9. Unhappy paths (path design)

- **MCV fails at confirm.** A column the chosen feature needs was dropped/retyped between generation and
  Gate 2 → `confirm_contract` returns the specific MCV rejection (422); the UI returns the user to the
  considered-set step with the reason, no contract minted.
- **Stale target.** Re-validate at confirm that the target's source columns still exist; if the target
  was dropped since Gate 1, block confirm with a clear message (the leakage anchor is invalid).
- **Stale considered set.** The snapshot is keyed by `intent_id`; if the catalog changed materially, the
  MCV re-run at confirm is the backstop (a now-invalid pick is rejected). Optionally warn "catalog
  changed since generation — regenerate?".
- **Double-confirm / idempotency.** The `contract (feature_name, version)` UNIQUE fences a concurrent
  double-confirm → 409, not a duplicate.

## 10. UI (the integration — closes #3/#4/#5)

Workbench becomes the two-gate flow:

1. **Brief screen** — hypothesis + optional definition; assisted-target picker (proposed **source
   columns** + confirm); catalog scope → **Approve brief** (Gate 1).
2. **Considered-set screen** — anchor + alternatives, each marked *gauntlet-passed* (with a plain-English
   "safe, not proven predictive" caveat per §3.3), plus the **rejected** candidates + reasons, and the
   advisory recommendation → multi-select.
3. **Confirm screen** — review the snapshotted contract sheet(s) → **Confirm** (Gate 2) → contracts
   minted. On MCV failure, land back here with the reason (§9).

Wire `api.ts` to `/contract/*` on the **real Bearer session** (resolves #3). Demote direct
`POST /features` in the Workbench to the labelled `UNVERIFIED` fast path (§8); the governed flow is the
only path that earns `DESIGN-CHECKED`.

## 11. Build phases (dependency order — detailed plan comes later)

1. **Contract model + lifecycle** — snapshot fields at confirm (§6.1); verification vocabulary + `CHECK`;
   `POST /features` → `UNVERIFIED`; `confirm_contract` explicitly stamps `DESIGN-CHECKED`; the
   existing-feature re-stamp migration (§8).
2. **Target as a source set + leakage** (§5) — the target model, grounded LLM proposal + confirm, and the
   deterministic `derives ∩ target_source` test replacing the single-column check.
3. **Gate 1 checkpoint + approval modes** (§4.1, §7) — record brief approval; `feature:approve` + the
   four-eyes flag.
4. **UI** (§10) — the three screens on Bearer auth; batch approve + show rejects; demote direct-register.

## 12. Explicit non-goals

- No data-plane compute, feature-value serving, or training-set generation.
- No predictiveness / "feature supports target" validation.
- No **proxy** leakage detection (a different-but-near-identical column) — needs data. Only design-time
  **direct** leakage via the target source-set intersection (§5).
- No mapping / compilation / execution layer.
- Composed features (a feature derived from *other features*) are a **known limitation** — `derives_from`
  is columns only; defer.

## 13. Decisions (resolved 2026-07-07)

- **A — store vs. assemble** → **snapshot the safety-critical fields at confirm; assemble only static
  reference data** (§6.1). *(revised from v1's pure assemble — a versioned attestation must be frozen.)*
- **B — same-approver vs. four-eyes** → **configurable, default same-approver**; four-eyes requires
  `feature:approve` + a distinct subject (§7).
- **C — target shape** → **a set of source columns**, leakage tested by intersection (§5).
- **D — governance scope** → mandatory governance is *not* forced on every feature; `UNVERIFIED` fast
  path + promote-to-governed (§8).
