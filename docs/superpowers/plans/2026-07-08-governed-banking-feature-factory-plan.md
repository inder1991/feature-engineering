# Governed, Banking-Intelligent Feature Factory — Phased Build Plan

> **For agentic workers:** each phase is an independently shippable, testable slice. When a phase
> starts, expand it into bite-sized task-by-task steps (superpowers:writing-plans) — this doc is the
> program roadmap, not the per-task detail.

**Goal:** turn the orphaned contract engine into the real product flow — a two-gate human-approval flow,
four deterministic banking-safety checks, a banking-intelligent generation engine, and a bank-grade
knowledge base.

**Architecture:** a governance *skeleton* (the contract flow) with a domain-intelligence *brain* that
plugs into it at Gate 1 / generation / Gate 2, over a structured banking *knowledge base*.

**Design sources:** `governed-feature-contract-flow-design.md` · `banking-domain-intelligence-design.md`
· `banking-taxonomy-reference.md` · `banking-taxonomy-sme-review.md`.

## Global constraints (apply to every phase)

- **No data plane** — no compute/serving/training-sets/predictiveness. Templates are *definitions*.
- **LLM proposes; deterministic code + humans dispose.** The four safety checks are deterministic.
- **The four safety checks** gate every governed feature: leakage (3-part) · point-in-time incl.
  bi-temporal · currency · eligibility (consent/purpose/residency/fair-lending/additivity).
- **`DESIGN-CHECKED` is earned** (gauntlet ran); direct registration is `UNVERIFIED`.
- **RBAC-gated** (permissions layer already merged); every new route gates on a permission.
- **TDD, frequent commits, migrations are new files** (runner checksum-guards applied ones).
- **Backend:** `uv run pytest -q`, `uv run ruff check src tests`. **Frontend:** `tsc -b`, `vitest`.

## Migration numbering
Continue from `0972`. Reserve: `0973` (verification vocab), `0974` (target model), `0975` (domain-catalog
store), `0976` (approval/gate1), `0977` (flywheel signals). Adjust as phases land.

---

## Phase 1 — Contract model + honest lifecycle  *(closes finding #4)*

**Goal:** the signed contract is immutable + complete; stamps are honest.
**Files:**
- `src/featuregen/overlay/upload/features.py` — `FeatureSpec.verification` default → `"UNVERIFIED"`.
- `src/featuregen/overlay/upload/contract/govern.py` — `confirm_contract` **snapshots** the safety-critical
  fields onto the contract (target, grain, as_of/PIT rule, lookback, calc-method, derives) + explicitly
  stamps `DESIGN-CHECKED`; add the `feature_detail` assembled view over the snapshot + `contract_intent`.
- `src/featuregen/api/routes/features.py` — `POST /features` keeps `UNVERIFIED` (no change beyond default).
- **Migration `0973`** — `feature.verification` / `contract.verification` `CHECK IN ('UNVERIFIED',
  'DESIGN-CHECKED')`; **re-stamp** all contract-less features `UNVERIFIED` (honest); the new snapshot
  columns/JSON on `contract`.
**Tasks (TDD):** verification-vocab CHECK + backfill test → snapshot-at-confirm test (re-confirm doesn't
mutate v1) → `POST /features` → `UNVERIFIED` test → assembled `feature_detail` view test.
**Tests:** `tests/.../contract/test_govern.py`, `test_features_registry.py`, `tests/.../db/test_migrations.py`.
**Deliverable / done-when:** a governed feature has an immutable snapshotted contract stamped
`DESIGN-CHECKED`; direct registration is `UNVERIFIED`; existing features re-stamped; suite green.

## Phase 2 — Target model + the four deterministic safety checks

**Goal:** the safety spine — the leakage/PIT/currency/eligibility checks — deterministic and testable.
**Files:**
- **Migration `0974`** — `contract_intent.target` becomes `{name, definition, label_column,
  source_columns[]}` (JSON); `system_time` capture on the fact/graph rows (bi-temporal).
- `src/featuregen/overlay/upload/contract/gate1.py` — store/read the structured target.
- `src/featuregen/overlay/upload/feature_assist.py` — the **calibrated 3-part leakage** (`_validate_idea`:
  HARD-reject `label_column` in derives; SOFT-flag `source_columns`; keep PIT as the real gate);
  **currency** check (no cross-currency aggregate without base + fx); **eligibility** check
  (consent/purpose/residency/fair-lending/additivity — reads the column metadata).
- `src/featuregen/overlay/upload/join_path.py` / graph reads — bi-temporal `system_time ≤ as_of` filter.
**Tasks (TDD):** target-model round-trip → 3-part leakage (label hard-reject; shared-col flag not reject;
`days_since_last_txn` passes) → bi-temporal filter drops a restated value → cross-currency aggregate
rejected → protected-attribute/consent ineligible feature rejected.
**Tests:** `test_feature_loop.py`, new `test_safety_checks.py`, `test_gate1.py`.
**Deliverable / done-when:** all four checks deterministic + tested; a shared-source-column feature is
flagged (not killed); a restated/cross-currency/ineligible feature is caught.

## Phase 3 — Solid vocabulary + parametric template engine

**Goal:** the structured concept ontology and safe-by-construction templates.
**Files:**
- `src/featuregen/overlay/upload/concepts.py` — replace the flat 11 with the ~70 structured concepts
  (behaviour: additivity/PIT-role/sensitivity/entity-link; is-a edges). Classifier maps to nearest +
  `unclassified`.
- **New** `src/featuregen/overlay/upload/templates.py` — the parametric template model + a small
  deterministic **template engine** (ground `{params}` to real columns; PIT baked in) + a first real set
  for the seeded domains (churn/credit).
- `src/featuregen/overlay/upload/enrich.py` — enrichment writes the richer concept + behaviour.
**Tasks (TDD):** concept behaviour drives a check (a `monetary_stock` can't be summed over time) →
is-a generalisation test → a template grounds to columns + produces a leakage-safe feature by
construction → an ungroundable template is skipped.
**Tests:** `test_concepts.py`, new `test_templates.py`.
**Deliverable / done-when:** concepts carry behaviour + relate; templates are groundable, parametric, and
safe-by-construction; suite green.

## Phase 4 — Domain case catalog + governed knowledge store

**Goal:** the living, ratifiable banking knowledge base (not a shipped constant).
**Files:**
- **Migration `0975`** — `domain_use_case`, `domain_template`, `domain_concept`, `domain_entity`,
  `domain_ratification` (versioned, audited); `compliance_confirmed` per use-case.
- **New** `src/featuregen/overlay/domain/catalog.py` — load `banking-domain-catalog.seed.json` into the
  store on setup; read/query; **onboard** a new use-case; **ratify** (owner + Compliance flip
  `compliance_confirmed`); regulatory rules NOT authoritative until ratified.
- Admin/curation routes (RBAC-gated) to edit the catalog.
**Tasks (TDD):** seed loads → query by use-case → onboard new banking use-case → ratification gate (rules
inert until confirmed) → version/audit on change.
**Tests:** new `tests/.../domain/test_catalog.py`.
**Deliverable / done-when:** a DB-backed, versioned, ratifiable catalog seeded from the JSON; unratified
regulatory rules do not enforce; suite green.

## Phase 5 — Gate 1 checkpoint + approval modes + reasoning wired in

**Goal:** the two-gate governed flow that reasons in banking (backend).
**Files:**
- `src/featuregen/api/routes/contract.py` — Gate 1 **approve-brief** checkpoint; record approval + actor.
- `src/featuregen/identity/permissions.py` — add `feature:approve`; **four-eyes** flag
  (`FEATUREGEN_CONTRACT_FOUR_EYES`): Gate-2 approver must hold `feature:approve` **and** differ from Gate-1
  actor (server-enforced).
- `src/featuregen/overlay/upload/contract/gate1.py` + `feature_assist.py` — wire **use-case recognition**,
  **known-target proposal** (from the catalog), **template-seeded generation**, **regulatory filter**
  (block/flag data classes for the use-case).
**Tasks (TDD):** brief-approval recorded → four-eyes rejects same-subject Gate-2 (flag on) / allows (off)
→ use-case recognised → known target proposed + confirmed → generation seeded from templates → a
protected-attribute feature blocked for a credit use-case.
**Tests:** `test_contract.py`, `test_admin.py`/authz, `test_feature_loop.py`.
**Deliverable / done-when:** end-to-end backend flow — approve brief → banking-reasoned considered set →
approve → contract; four-eyes enforced when configured; suite green.

## Phase 6 — UI  *(closes findings #3 + #5-frontend)*

**Goal:** the usable governed flow on real auth; retire the fake fast-stamp path in the Workbench.
**Files (frontend):**
- `frontend/src/api.ts` — wire `/contract/considered-set`, `/draft`, `/confirm`, `/contracts` on the
  **real Bearer session**.
- **New screens** — Brief (hypothesis + assisted-target picker + scope), Considered-set (safe candidates
  + **rejects with reasons** + "safe, not proven" caveat + multi-select/**batch**), Confirm (snapshot
  sheet review → Gate 2).
- `frontend/src/screens/WorkbenchScreen.tsx` — demote direct `POST /features` to the labelled
  `UNVERIFIED` fast path; add the "promote to governed" entry.
**Tasks (TDD, vitest):** brief screen → target confirm; considered-set renders safe + rejects + caveat;
batch approve mints N contracts; confirm-failure returns to the set with the reason; Bearer-auth path.
**Tests:** new screen `.test.tsx` files; `tsc -b`.
**Deliverable / done-when:** a human can drive the whole two-gate flow in the UI on real login; direct
register is honestly `UNVERIFIED`; frontend green.

## Phase 7 — Learning + curation flywheel  *(gets smarter with use)*

**Goal:** the system improves from every human decision and grows the knowledge base.
**Files:**
- **Migration `0977`** — persist Gate-2 approve/reject signals per use-case + team.
- `src/featuregen/overlay/upload/feature_assist.py` — steer generation toward approved patterns; demote
  repeatedly-rejected ones.
- Curation admin surface (routes + UI) to edit vocabulary/cases/templates (versioned, audited).
**Tasks (TDD):** decision captured → generation steered by prior approvals → curator edit versioned +
audited → learned target refinement proposed (human ratifies).
**Tests:** new `test_flywheel.py`; curation route tests.
**Deliverable / done-when:** repeated use measurably steers generation; the knowledge base is
curator-editable + audited; suite green.

---

## Sequencing & risks

- **Order is dependency-driven:** 1→2 (safety spine needs the contract) → 3→4 (brain content) → 5 (wire
  reasoning) → 6 (UI) → 7 (learning). Each ships working software; ship + review between phases.
- **Biggest-value early:** Phases 1–2 alone close findings #4 + the leakage-safety gaps and make stamps
  honest — worth landing first even if the rest waits.
- **Biggest effort:** Phase 3–4 (real templates + the governed catalog) — the domain-intelligence content;
  ship a thin first set, grow via curation (Phase 7).
- **Riskiest:** Phase 2 bi-temporal (`system_time`) — requires capturing knowledge-time on ingest; if the
  upload doesn't carry it, default `system_time = ingest_time` and document the limitation.
- **Reversibility:** each migration is a new file; the `UNVERIFIED` re-stamp (Phase 1) is the one
  data-touching step — log the count, it's reversible by re-confirming.

## Execution
Each phase → expand to bite-sized TDD tasks (writing-plans) at its start, own branch, merge-and-review
between phases. Start with **Phase 1** (small, high-value, closes #4).
