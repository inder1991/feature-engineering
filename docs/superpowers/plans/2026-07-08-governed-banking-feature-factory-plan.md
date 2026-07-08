# Governed, Banking-Intelligent Feature Factory — Phased Build Plan (v2)

> **v2 restructure** (after a senior-EM critique of v1): vertical-slice-first (value in weeks, not
> quarters) · two parallel workstreams · T-shirt sized · feature-flagged rollout · a golden-set quality
> bar · early flywheel capture · an explicit value statement. v1 was horizontal layers with back-loaded
> value and no sizing.

## 0. Value statement & first user (read first)

**What this delivers (no data plane — be honest):** not computed feature values, but **(1) trustworthy
governance** — every model feature has a human-approved, versioned contract with its target, safety
checks, and approver (examiner-ready provenance); and **(2) accelerated, de-risked feature *definition*
** — banking knowledge proposes safe features and blocks leaky/ineligible ones *before* an engineer
wastes a modelling cycle.

**First user (pilot):** the model-development team building the **retail-churn** model. They author
feature definitions, get a banking-aware assist, and walk away with a governed audit trail. Success =
that team prefers this flow to hand-registering features, and their contracts pass an internal
model-risk review. If we can't win that team, we stop and rethink — before building the long tail.

## 1. Global constraints (every increment)

- **No data plane** — no compute/serving/training/predictiveness. Templates are *definitions*.
- **LLM proposes; deterministic code + humans dispose.** The four safety checks are deterministic.
- **Four safety checks** gate every governed feature: leakage (3-part) · point-in-time incl. bi-temporal
  · currency · eligibility (consent/purpose/residency/fair-lending/additivity).
- **`DESIGN-CHECKED` is earned**; direct registration is `UNVERIFIED`.
- **Every user-facing / breaking change is behind a feature flag** with a documented backout.
- **RBAC-gated · TDD · frequent commits · migrations are new files** (0973+).
- **Backend:** `uv run pytest -q`, `ruff check src tests`. **Frontend:** `tsc -b`, `vitest`.
- **Sizes:** S ≈ days · M ≈ 1–2 wks · L ≈ 3–5 wks · XL ≈ 6+ wks (always split).

## 2. Kick off NOW (parallel to M0 — external/long-lead, not on eng's critical path)

- **LLM enablement** — wire + validate the real `ClaudeLLM` seam (today's default is the fake). *Gate
  for all intelligence.* Owner: platform. **[S–M]**
- **Compliance/domain-owner ratification** for the pilot use-case's regulatory defaults (the catalog is
  inert until ratified — §Workstream B3). *Org dependency, long lead.* Owner: product + a bank sponsor.
- **Golden set v0** — ~20 expert-curated churn hypotheses with expected/【approved】 features + known
  leaky traps. This is the **quality bar** for the intelligence (there's no data, so human eval is it).
  Owner: a domain SME. **[S]**

---

## 3. M0 — Walking skeleton (the vertical slice) · **[M]** · *the first shippable increment*

**Goal:** a data scientist drives the **whole two-gate flow end-to-end for `retail_churn`, in a minimal
UI, on real login** — thin on depth, complete on shape. Behind a flag, shipped to the pilot team for
feedback. This de-risks the architecture and delivers value in weeks.

**Deliberately thin (deepened later):** uses the *existing* gauntlet (full 4 checks come in A2); the
churn target is proposed by a *hardcoded* rule, not the catalog (real recognition in B4); no template
seeding yet; 3 bare-bones screens.

**Slices touched:** honest stamp (thin) · the existing `/contract/*` endpoints · 3 minimal screens ·
Bearer auth · **flywheel capture from day one** (record every approve/reject — cheap, compounds).

**Done-when:** the pilot DS completes brief → considered set → approve → **governed contract with an
honest stamp** for churn, in the UI, logged-in, with the decision captured. Suite + frontend green.

**After M0, the two workstreams run in parallel and deepen behind this working slice.**

---

## 4. Workstream A — Governed Flow (harden the skeleton)

| # | Increment | Size | Key files | Done-when |
|---|---|---|---|---|
| **A1** | Contract model + honest lifecycle | **M** | `features.py` (default→UNVERIFIED), `govern.py` (snapshot@confirm + explicit DESIGN-CHECKED + assembled view), mig `0973` (CHECK + re-stamp existing, **flagged + backup**) | immutable snapshotted contracts; existing features re-stamped `UNVERIFIED`; #4 closed |
| **A2** | The 4 deterministic safety checks | **L** | mig `0974` (target `{label,source_cols}`, `system_time`); `feature_assist.py` (3-part leakage + currency + eligibility); graph reads (bi-temporal `system_time≤as_of`) | each check deterministic + tested; `days_since_last_txn` passes, restated/cross-currency/ineligible caught |
| **A3** | Gate-1 checkpoint + four-eyes | **M** | `contract.py` (approve-brief), `permissions.py` (`feature:approve` + `FEATUREGEN_CONTRACT_FOUR_EYES`) | brief approval recorded; four-eyes rejects same-subject Gate-2 when on |
| **A4** | UI hardening | **M** | new screens hardened: batch approve, **show rejects + "safe not proven" caveat**, confirm-failure→back, promote-to-governed | multi-approve mints N; rejects visible; unhappy paths land gracefully |
| **A5** | Rollout + observability | **S–M** | flags default-on, deprecate old `POST /features` register path (window), metrics (adoption, gate pass/fail, rejection reasons) | flow is the default path; dashboards live; old path deprecated |

## 5. Workstream B — Domain Intelligence (brain + content)  *(parallel to A; integrates at B4)*

| # | Increment | Size | Key files | Done-when |
|---|---|---|---|---|
| **B1** | Solid vocabulary | **L** | `concepts.py` (11→~70 structured concepts + behaviour + is-a); `enrich.py` | concepts carry additivity/PIT/sensitivity/entity-link; behaviour drives a check |
| **B2** | Parametric template engine + first set | **L** | new `templates.py` (model + deterministic engine, PIT baked in) + churn/credit templates | a template grounds to columns → a leakage-safe feature by construction; ungroundable skipped |
| **B3a** | Governed knowledge store | **L** | mig `0975` (catalog tables, versioned/audited); new `domain/catalog.py` (load seed, query, onboard) | DB-backed catalog seeded from the JSON; onboard a new use-case |
| **B3b** | Ratification + curation | **M** | ratify flow (owner + Compliance flip `compliance_confirmed`); RBAC curation routes | regulatory rules **inert until ratified**; edits versioned/audited |
| **B4** | Reasoning wired into the flow *(← integration with A)* | **M** | `gate1.py`/`feature_assist.py` — use-case recognition, known-target proposal, template-seeded generation, regulatory filter | slice now recognises the use-case, proposes the known target, seeds from templates, blocks protected data — scored on the **golden set** |
| **B5** | Flywheel steering *(capture already live since M0)* | **M** | `feature_assist.py` steering; mig `0977` if needed | prior approvals measurably steer generation; curator can promote learned refinements |

---

## 6. Convergence & critical path

```
Kick-off (LLM · ratification · golden set)  ──┐
                                              ▼
M0 walking skeleton  ──►  A1 A2 A3 A4 A5  (Governed Flow) ──┐
                    └──►  B1 B2 B3a B3b     (Domain Intel)  ──►  B4 wire-in ──►  A5 default-on
```
- **Critical path to real value:** M0 → **A2** (safety) + **B4** (reasoning, needs B1+B2+LLM) → A5
  default-on. Everything else parallelises around it.
- **Two teams:** Stream-A eng (governance/flow/UI) and Stream-B eng (vocabulary/templates/catalog).
  Converge at B4. Halves calendar time vs. v1's serial chain.
- **XL split done:** v1's Phase 3/4 are now B1/B2/B3a/B3b (each L/M).

## 6a. Architecture fixes folded in (head-of-architect review) — where each lands

| # | Fix (in the specs) | Increment |
|---|---|---|
| 1 | **Honest safety model** — enforced (leakage/eligibility/currency-tag/additivity) vs. declared-only (bi-temporal/runtime); `DESIGN-CHECKED` ≠ runtime guarantee | **A2** (+ contract/UI copy) |
| 2 | **Drift stales contracts** — `contracts_affected_by` flags governed contracts; add **`NEEDS_REVIEW`** stamp; snapshot target/template versions | **A2/A5** (extend the drift path; A1 adds the stamp value) |
| 3 | **Use-case is a safety input** — human-confirmed at Gate 1; **fail-to-strictest** on uncertainty | **B4** (+ A3 Gate-1 UI) |
| 4 | **Unratified fails CLOSED** — block protected/sensitive by default + `NEEDS_REVIEW` until Compliance ratifies | **B3b** |
| 5 | **Read-scope the snapshot** — filter frozen column refs by role at render (no PII side-channel) | **A1** |

## 7. Cross-cutting (apply throughout — the v1 gaps)

- **Feature flags + backout:** every breaking/user-facing change flagged (M0, A1 re-stamp, A5 default-on,
  old-path deprecation). The re-stamp is the only data-touching step → backup + reversible-by-reconfirm.
- **Quality bar = the golden set:** B4 (and every generation change) is scored against the expert golden
  set each release — the only quality signal available without data. Ship gate: no regression on it.
- **Early flywheel capture:** the approve/reject signal is captured from **M0** (cheap), so steering (B5)
  has months of data when it lands. Don't wait.
- **Observability (A5):** adoption, gate pass/fail rates, rejection reasons, contract mint rate, LLM
  latency/cost. A dashboard is a phase deliverable, not an afterthought.
- **UAT / acceptance:** each user-facing increment (M0, A4) has explicit pilot-user acceptance criteria —
  "suite green" is a code bar, not product sign-off.
- **Performance budget:** LLM latency + the gauntlet over large catalogs; set a budget in A2/B4.
- **Docs/runbooks:** the ratification workflow (B3b) and knowledge-base curation need operator docs.

## 8. Sequencing, risk, and the honest checkpoint

- **Land M0 + A1 first** — a working governed slice with honest stamps (closes #4, real value, pilot
  feedback) in ~3–4 weeks.
- **Riskiest:** A2 bi-temporal `system_time` (if uploads don't carry knowledge-time, default to
  ingest-time + document); B2 real templates + B4 quality (mitigated by the golden set).
- **Org risk:** B3b ratification depends on a bank Compliance sponsor — start at kick-off; if it stalls,
  the flow still works unratified (regulatory rules simply don't enforce).
- **Go/no-go after M0:** if the pilot team won't adopt the slice, **stop and rethink the value** before
  funding Streams A+B. This is the plan's built-in kill-switch (the value statement, §0, is the test).

## 9. Execution
Each increment → expand to bite-sized TDD tasks (writing-plans) at its start, own branch,
merge-and-review. **Start with M0** (the walking skeleton) — smallest thing that proves the shape and
puts value in a user's hands.
