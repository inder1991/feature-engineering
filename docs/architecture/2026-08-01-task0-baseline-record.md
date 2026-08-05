# Task 0 — Executed Baseline Record (2026-08-01)

**Implementation baseline:** `origin/main @ fa9a20b0` — identical to the SHA the adversarial
review (`2026-08-01-plan-review-semantic-context-and-catalog-profiles.md`) verified, so every
review citation applies to this baseline unchanged.

**Worktree:** `.claude/worktrees/integration-aug01`, branch
`feature/integration-2026-08-01-semantic-profiles`. Track 2 (codegen remediation) runs in its own
worktree (`.claude/worktrees/codegen-review-remediation`); no files are shared between tracks
until the synchronization point.

**Root-checkout dirt (excluded from this implementation):** modified `frontend/src/{App.tsx,
App.test.tsx,index.css,nav.ts,nav.test.ts}`, `uv.lock`, untracked
`frontend/src/screens/AssetDetailSampleScreen.{tsx,test.tsx}` and assorted plan docs. Only the
controlling documents were copied onto this branch (commit `55785c86`); no user code changes were
carried over.

**Migration ledger (source tree):** ledger identity is the full filename set, not a head number.
Max prefix `1043_semantic_binding_fixed_currency.sql`; duplicate prefixes exist at
0973, 0974, 1034, 1036, 1037, 1038, 1040. Runner applies `*.sql` in lexical order, ledgered by
name + SHA-256 (`db/migrations.py:260-317`). Reservations for this program: see verified-interfaces
doc D7 (1044 codegen … 1050 crosswalk). **Live kind ledger was NOT re-inspected this session**
(last known head `1041_catalog_engine` per the profile plan §2); it must be re-read at Gate A
before any deploy.

**Flags at baseline:** `FEATUREGEN_FEATURE_CONTEXT` read only in `feature_assist.py:189-196`,
absent from `deploy/kind/k8s/20-backend.yaml` and `.env.example` (off in kind).
`OVERLAY_TABLE_SYNTH`, `OVERLAY_PASS_C` default off. `FEATUREGEN_DATASET_PROFILES`,
`FEATUREGEN_SOURCE_TEMPORAL_SELECTION`, `FEATUREGEN_CROSSWALK_EXECUTION` do not exist yet.

**Plan-specific modules verified absent** (no parallel implementation): `semantic_context.py`,
`identifier_scope.py`, `semantic_adjudication.py`, `semantic_gap.py`, `context_graph.py`,
`api/routes/context.py`, `profile_vocab.py`, `dataset_profiles.py`, `catalog_profiles.py`,
`profile_store.py`, `source_selection.py`, `temporal_policy.py`, `serving_policy_store.py`,
`temporal_policy_store.py`, `crosswalk.py`, `crosswalk_store.py`, `api/routes/dataset_policies.py`,
`attest/dataset_profile_critic.py`.

**Focused baseline suites — GREEN, 238 passed in 8.85s:**

```
uv run --extra dev pytest -q \
  tests/featuregen/overlay/upload/test_enrich.py \
  tests/featuregen/overlay/upload/test_enrich_llm.py \
  tests/featuregen/overlay/upload/attest/test_concept_critic.py \
  tests/featuregen/overlay/upload/test_concept_critic_acceptance.py \
  tests/featuregen/overlay/upload/test_feature_menu_enrichment.py \
  tests/featuregen/overlay/upload/test_asset_detail_dossier.py \
  tests/featuregen/overlay/upload/test_entity_map.py \
  tests/featuregen/analysis/test_retrieval.py \
  tests/featuregen/overlay/upload/test_field_resolution.py \
  tests/featuregen/overlay/upload/test_table_synth_assemble.py \
  tests/featuregen/overlay/upload/test_table_synth_propose.py \
  tests/featuregen/overlay/upload/test_table_synth_wide.py \
  tests/featuregen/overlay/upload/test_search.py \
  tests/featuregen/overlay/upload/test_feature_assist.py \
  tests/featuregen/overlay/upload/test_feature_metadata_snapshot.py \
  tests/featuregen/materialize/test_joins.py \
  tests/featuregen/data_agent/test_analysis_ir.py
```

(Note: there is no `tests/featuregen/data_agent/test_dimensions.py` — dimension attribution tests
live inside `test_analysis_ir.py`. A first run listing the nonexistent file exited "green" through
an output pipe; the recorded run above used `pipefail` and a verified `EXIT=0`.)

**Test-count gate rule (per D9):** the literal-count gate for this program is scoped to the file
list above (238 at baseline), never the whole repo (DEFERRED-WORK §C contamination).

**Rebaseline ledger.** `RELEASE_GATE_BASELINE` in `tests/eval/mutation/test_must_die.py` holds the
CURRENT literal count for exactly the seventeen files above. A DROP is a deleted guard and must be
explained; a RISE is legitimate but must be recorded here in the same commit that moves it.

| Count | Date | Why it moved |
|---|---|---|
| 238 | 2026-08-01 | Task-0 baseline, `origin/main @ fa9a20b0`. |
| 273 | 2026-08-03 | Release-A integration added 31; the Track-2 merge added 4 first-hop cardinality tests to `test_joins.py`. |
| **277** | **2026-08-05** | **Release C Task 13: +4 crosswalk adapter tests in `test_joins.py`.** `joins.plan_crosswalk_join` lives in that module and had NO test there at all — its entire coverage was in `tests/featuregen/materialize/test_crosswalk_ir.py`, which drives it THROUGH the IR and therefore cannot pin the adapter's own answers. The four go at it directly: two steps rather than one endpoint-equality collapse; both legs' column pairs reaching the plan so neither escapes Gate 2; each direction gated on its OWN measured cardinality; and a fanning direction refused rather than deduplicated. Three of Task 13's eight required mutations (`crosswalk_renders_endpoint_equality`, `crosswalk_cardinality_inverted`, `crosswalk_deduplicates_instead_of_refusing`) name them as victims, so the rise buys real kill coverage rather than count. |

**Mutation-registry size (a different number, recorded here so the two are not confused).**
`pytest -m eval tests/eval/mutation/` collected **34** on 2026-08-05 and collects **35** after the
Task-13 review fixes: one added must-die entry, `crosswalk_policy_pin_leaves_the_measurement`, which
reinstates the shipped defect the review found (an execution pinning a mapping row rule that did not
come from the measurement). `RELEASE_GATE_BASELINE` is **unchanged at 277** — no test was added to
any of the seventeen files, and the registry size is not gated on a literal.

**Repo-wide suite status at baseline (recorded 2026-08-01, Task 0.6 review):** 2 pre-existing
failures in `tests/featuregen/api/test_nginx_proxy_covers_frontend_calls.py` — the nginx proxy
location list is missing `/data-sources`; both failures are present at `fa9a20b0` and unrelated to
this branch. Overlay-tree precision (correcting an earlier imprecise citation): **3371** tests is
the `tests/featuregen/overlay/upload` subtree; the full `tests/featuregen/overlay` tree is
**3660**.

**No deploy, no upload, no live LLM call, no Hive/ODS connection was made.**
