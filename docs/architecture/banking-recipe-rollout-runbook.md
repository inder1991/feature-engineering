# Banking Recipe Contract v2 — rollout runbook (BR-24)

The engineering side of BR-1..24 is complete on `main`. This runbook is the OPERATOR's half:
what to deploy, which levers to pull, in what order, and how to verify or roll back each step.
Nothing below happens automatically — every promotion is a deliberate action.

## 0. What is already true (stage 3, no action needed)

- The V2 registry (317 recipes, 8 model specs) is source-controlled and ACTIVE for contract
  v3's execution truth and release coverage. It serves no ordinary suggestion traffic.
- v1/v2 suggestion contracts are byte-frozen and still generate from the legacy projection.
- Contract v3 is available by explicit `?contract_version=3` (flag default ON).
- All rollout flags beyond v3 default OFF; the allowlists default empty — pinned by the
  frozen-configuration test.

## 1. Deploy prerequisites (backend BEFORE frontend)

Two migrations are pending on the cluster and MUST land backend-first:

- `1060_recipe_review_event.sql` — the append-only review store (UPDATE/DELETE are DB errors).
- `1061_recipe_review_event_seq.sql` — the `recorded_seq` ordering fix (same-transaction
  timestamps tie; the sequence is the append order as a stored fact).

Verify after deploy: `GET /recipes/posted_debit_amount/reviews` returns 200 with
`validity.current: false` and the three required roles named.

## 2. The levers

| Env var | Default | Meaning |
|---|---|---|
| `FEATUREGEN_SUGGESTION_CONTRACT_V3` | on | v3 by explicit query; off = typed 422, v1/v2 untouched |
| `FEATUREGEN_RECIPE_CONTRACT_V2` | off | V2 registry may serve suggestions (with allowlists) |
| `FEATUREGEN_RECIPE_V2_FAMILIES` | empty | per-FAMILY promotion allowlist (CSV) |
| `FEATUREGEN_RECIPE_V2_CANARY_CATALOGS` | empty | per-CATALOG canary allowlist (CSV) |
| `FEATUREGEN_FORMULA_V2` | off | Formula-v2 authoring (start: foundation families only) |
| `FEATUREGEN_RECIPE_V2_MATERIALIZATION` | off | execution of approved recipes on one engine |

A family is active only when the flag is on AND the family is allowlisted. An aggregate pass
rate promotes nothing.

## 3. Stage-by-stage promotion

1. **Stage 4 — internal v3 viewing.** No flag change; point internal SME/engineering users at
   `?contract_version=3`. Gather readiness-language feedback.
2. **Populate reviews.** SMEs record decisions through `POST /recipes/{id}/reviews`
   (`governance:confirm`; optimistic concurrency on the revision hash). The activation gate
   reads `review_coverage_report` — promote nothing whose reviews are not current.
3. **Stage 5 — canary suggestions.** `FEATUREGEN_RECIPE_CONTRACT_V2=on`,
   one family (`retail_churn`) + one catalog in the allowlists. Run the canary gates (§4).
4. **Stage 6 — authoring.** `FEATUREGEN_FORMULA_V2=on` for the foundation families; the
   exemplar (`posted_debit_amount`) is the first authorable target with reviewed expectation
   and gold.
5. **Stage 7 — materialization.** Only for recipes with CURRENT review validity, on one
   engine, after its `EngineCapabilityV1` is registered.
6. **Stages 8-10.** Expand family by family on individual gate passes; make v3 the frontend
   default; v1/v2 retirement is a separate, explicitly approved project.

## 4. Canary gates (all eight, per family — `canary_gate` is the fold)

Zero ambiguous required bindings; zero PIT compilation errors; zero formula/gold mismatch; no
read-scope regression; latency inside the route budget; no unexplained empty-state increase;
every canary-active recipe SME-approved (current validity); rollback tested. An unmeasured
gate BLOCKS — the fold's inputs default to the failing side.

## 5. Rollback (tested shape)

- Turn the flag(s) off / empty the allowlists. The registry, revisions, reviews and stored
  identities are untouched — configuration holds no state.
- v1/v2 behavior never depended on the flags; it continues unchanged.
- Formula-v1 authoring and stored artifacts are independent of every lever here.
- Historical suggestion and feature identities are never rewritten (the alias map and frozen
  legacy registry guarantee resolution).

## 6. Truthful metrics

`rollout_metrics()` reports registry counts by readiness, primary and executable coverage and
the gold-linked authorable set. Suggestion count is deliberately absent — it is not a success
metric and must not become one.
