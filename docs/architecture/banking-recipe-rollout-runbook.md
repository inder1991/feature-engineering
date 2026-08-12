# Banking Recipe Contract v2 — rollout runbook (BR-24)

The engineering side of BR-1..24 is complete on `main`. This runbook is the OPERATOR's half:
what to deploy, which levers to pull, in what order, and how to verify or roll back each step.
Nothing below happens automatically — every promotion is a deliberate action.

## 0. What is already true (stage 3, no action needed)

- The V2 registry (317 recipes, 8 model specs) is source-controlled and ACTIVE for contract
  v3's execution truth and release coverage. It serves no ordinary suggestion traffic.
- v1/v2 suggestion contracts are byte-frozen and still generate from the legacy projection.
- Contract v3 is available by explicit `?contract_version=3`, unconditionally (its gate flag
  retired 2026-08-11).
- The BR-24 flag family is RETIRED (see §2): promotion happens by direct cutover under the
  activation invariant, not by lever.

## 1. Deploy prerequisites (backend BEFORE frontend)

Two migrations are pending on the cluster and MUST land backend-first:

- `1060_recipe_review_event.sql` — the append-only review store (UPDATE/DELETE are DB errors).
- `1061_recipe_review_event_seq.sql` — the `recorded_seq` ordering fix (same-transaction
  timestamps tie; the sequence is the append order as a stored fact).

Verify after deploy: `GET /recipes/posted_debit_amount/reviews` returns 200 with
`validity.current: false` and the three required roles named.

## 2. The levers — with their HONEST consumer status

**Read the "Consumer today" column before flipping anything.** Several levers are
forward-declared controls whose serving-path consumers arrive with the semantic-eligibility
program's tranches (`docs/superpowers/plans/2026-08-11-semantic-eligibility-feature-generation-workflow.md`).
Flipping a lever with no consumer changes NOTHING at runtime — it neither promotes nor breaks.
This column is corrected as consumers land; an operator must never have to read source to learn
whether a switch is connected.

| Env var | Default | Meaning | Consumer today (2026-08-11) |
|---|---|---|---|
| `FEATUREGEN_SUGGESTION_CONTRACT_V3` | — | — | **RETIRED 2026-08-11** — v3 serves unconditionally; version selection stays explicit via `contract_version` |
| `FEATUREGEN_SEMANTIC_PLANNING` | legacy | semantic-planning pipeline mode (legacy / semantic_shadow / semantic_v1) | **WIRED for shadow** — `semantic_shadow` runs the V2 lens beside Gate-1's template lens (log-only); `semantic_v1` has no consumer yet (Tranche 3) |
| `FEATUREGEN_RECIPE_CONTRACT_V2` | — | — | **RETIRED 2026-08-11** (never had a consumer) — V2 serving arrives by direct cutover with SE Tranche 3 |
| `FEATUREGEN_RECIPE_V2_FAMILIES` | — | — | **RETIRED 2026-08-11** (never had a consumer) |
| `FEATUREGEN_RECIPE_V2_CANARY_CATALOGS` | — | — | **RETIRED 2026-08-11** (never had a consumer) |
| `FEATUREGEN_FORMULA_V2` | — | — | **RETIRED 2026-08-11** (never had a consumer) — the v2 authoring path stays unchartered work that must precede stage 6; when built it ships direct, not behind a flag |
| `FEATUREGEN_RECIPE_V2_MATERIALIZATION` | — | — | **RETIRED 2026-08-11** (never had a consumer) — arrives direct with an engine that advertises Formula-v2, gated by the activation invariant in code |

The `canary_gate` fold and `review_coverage_report` likewise have no
runtime collector yet: their inputs are measurements the SE-14 shadow metrics will supply —
until then, gate readings are operator-supplied and the gate's failing defaults are the only
protection.

**Pre-live simplification (2026-08-11):** the tool is under development with no production
users. No NEW levers will be added to this table without a load-bearing reason (real spend, a
shared cluster, review governance). The unconsumed levers above are candidates for RETIREMENT
rather than wiring — the semantic-eligibility program's direct cutover (validate in shadow →
flip → delete the legacy path) supersedes the staged promotion they were reserved for.

## 3. Stage-by-stage promotion

1. **Stage 4 — internal v3 viewing.** No flag change; point internal SME/engineering users at
   `?contract_version=3`. Gather readiness-language feedback.
2. **Populate reviews.** SMEs record decisions through `POST /recipes/{id}/reviews`
   (`governance:confirm`; optimistic concurrency on the revision hash). The activation gate
   reads `review_coverage_report` — promote nothing whose reviews are not current.
3. **Stage 5 — V2 serving.** Arrives by DIRECT CUTOVER with SE Tranche 3 (no flag): the
   first serving consumer must enforce review validity IN CODE (the activation invariant),
   and the §4 gate readings must be green before the cutover commit merges.
4. **Stage 6 — authoring.** Ships direct when the Formula-v2 authoring path is built
   (UNCHARTERED work — the orchestrator is v1-only today; charter it first). The exemplar
   (`posted_debit_amount`) is the first authorable target with reviewed expectation and gold.
5. **Stage 7 — materialization.** Ships direct when an engine advertises Formula-v2
   (admission deliberately refuses non-v1 until then), only for recipes with CURRENT review
   validity — enforced in code per the activation invariant.
6. **Stages 8-10.** Expand family by family on individual gate passes; make v3 the frontend
   default; v1/v2 retirement is a separate, explicitly approved project.

## 4. Canary gates (all eight, per family — `canary_gate` is the fold)

Zero ambiguous required bindings; zero PIT compilation errors; zero formula/gold mismatch; no
read-scope regression; latency inside the route budget; no unexplained empty-state increase;
every canary-active recipe SME-approved (current validity); rollback tested. An unmeasured
gate BLOCKS — the fold's inputs default to the failing side.

## 5. Rollback (tested shape)

- Pre-cutover: nothing to roll back — behavior ships direct, so rollback = revert the commit.
  The registry, revisions, reviews and stored identities are untouched either way —
  configuration holds no state.
- v1/v2 behavior never depended on any lever; it continues unchanged.
- Formula-v1 authoring and stored artifacts are independent of every lever here.
- Historical suggestion and feature identities are never rewritten (the alias map and frozen
  legacy registry guarantee resolution).

## 6. Truthful metrics

`rollout_metrics()` reports registry counts by readiness, primary and executable coverage and
the gold-linked authorable set. Suggestion count is deliberately absent — it is not a success
metric and must not become one.
