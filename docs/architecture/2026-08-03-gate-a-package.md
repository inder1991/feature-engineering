# Gate A — Release A Deploy Approval Package (2026-08-03)

**Requesting approval to:** deploy tag `release-a-candidate-2026-08-03` (commit `48cd0a2f`,
branch `feature/integration-2026-08-01-semantic-profiles`) to the kind cluster, apply its
migrations, and enable two flags. **Nothing in this approval uploads a catalog, calls a live LLM,
or touches Hive/ODS — those are Gate B and later, separately.**

## What ships

Release A complete: semantic Tasks 1-9 + profile Tasks 1-5 + D13 (BIAN/process axis, sub_domain)
+ the Task 0.6 seam repairs, all adversarially reviewed, fixed, and probe-confirmed
(9 review cycles; every finding closed or explicitly deferred with trigger). Evaluation:
`docs/architecture/2026-08-03-release-a-evaluation-record.md` — thin→rich concept accuracy
2/22 → 22/22, forbidden selections 16 → 0, table selection 0/6 → 6/6, 16/16 must-die mutations
killed with expected-failure verification, Pass-B replay 11/11, all numbers independently
reproduced by the reviewing agent.

## Migrations (live ledger read 2026-08-03, read-only)

Live head-set ends at `1041_catalog_engine`. Pending on deploy, in the runner's lexical order:

| # | Migration | Origin |
|---|---|---|
| 1 | `1040_graph_node_party_role` | pre-existing main, never deployed |
| 2 | `1042_graph_node_sensitivity_display` | pre-existing main, never deployed |
| 3 | `1043_semantic_binding_fixed_currency` | pre-existing main, never deployed |
| 4 | `1045_catalog_semantic_scope` | Release A (scope table only — NO entity rewrite) |
| 5 | `1046_structured_result_current` | Release A (generic CAS pointer) |
| 6 | `1047_catalog_profile_revision` | Release A (narrative + graph_node profile columns) |
| 7 | `1051_graph_node_classification_axes` | Release A (bian_path/process_path/sub_domain) |
| 8 | `1052_graph_node_data_role_and_table_prose` | Release A (data_role + table FTS slots) |

All eight are additive (CREATE TABLE / ADD COLUMN IF NOT EXISTS); each Release-A migration was
legacy-replay-tested against seeded pre-migration rows. Note: live applied `1041` before
`1040_graph_node_party_role` existed — the name-keyed ledger handles this; `1040_*` has no
dependency on `1041`.

## Flags (deploy env changes)

- `FEATUREGEN_FEATURE_CONTEXT=1` — rich feature context (contract v4).
- `FEATUREGEN_DATASET_PROFILES=1` — profiles, facets, narrative, Context section.
- `FEATUREGEN_FEATURE_CONTEXT_VERSION` — NOT set; documented rollback lever (=3 restores the
  pre-v4 rich context; flag off entirely restores the thin menu, byte-identical, proven by the
  flag-matrix tests).

## Post-deploy smoke (I run these, in order, and report)

1. Migration-aware smoke: pending set applies clean; ledger name-set matches expectation.
2. `python -m featuregen backfill-projections` — ONE run, fills `data_role` + table-prose FTS on
   existing rows (without it the new facets/table search are empty until Gate B's re-upload).
3. Dossier + Context section on an existing column; search facets visible; catalogs list intact.
4. Flag-off spot check unnecessary (matrix-tested) but the version lever's presence verified.

## Rollback

Flags off = byte-identical pre-release behavior (tested). Migrations are additive; no destructive
change; no rollback migration needed.

## Costs and risks

- Zero LLM spend at this gate. The v4 payload-cost note (mid-size catalogs send ~2.2× more prompt
  bytes per feature-generation call) applies only when feature generation is USED, and only
  matters at Gate B and after.
- Live data risk: none identified — the entity ledger read (branch=8, counterparty=8, customer=1)
  confirms the 8 counterparty-keyed rows keep their fact keys under the shipped read-time-only
  design; rejections stick; nothing re-keys on deploy.

## DECISION ITEMS (yours)

1. **Approve the deploy above?** (Gate A proper.)
2. **Bar 4 — the feature use-gate gap.** The platform refuses target-leakage features but
   currently ACCEPTS features built on PII, protected characteristics, currency-blind sums and
   description-as-join-key: sensitivity controls who SEES a column; nothing yet controls whether
   a visible column may be USED in a feature. This is the pre-existing state made visible (strict
   xfail in the release bars), not a regression. Options:
   (a) **Ship with the gap recorded** (recommended: it is not worsened by Release A, and the
   use-gate is a well-scoped follow-up slice — feature-policy checks in the validator layer,
   no migration); or (b) hold Gate A until a use-gate slice lands (~1 implement+review cycle).
3. **Awareness, not approval: Gate B follows separately** — catalog re-upload + live LLM
   re-enrichment (full concept/domain re-classification under the new vocabulary + sub_domain +
   Pass-B profiles + BIAN/FIBO display population). Its own package will come with call/cost
   estimates and the witness checklist (incl. source-glossary/BIAN coverage per column).

**No action is taken until you approve. Release B development and the codegen-merge review
proceed on branches in the meantime; the tag isolates this deploy from both.**
