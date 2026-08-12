# SE-0 Baseline Audit — Semantic-Eligibility Program

**Date:** 2026-08-11
**Baseline commit:** `83488929` (`feat(review-ui): recipe-review screen — the BR-23 sign-off surface in the browser`)
**Plan:** `docs/superpowers/plans/2026-08-11-semantic-eligibility-feature-generation-workflow.md`
**Pin tests:** `tests/featuregen/overlay/upload/test_semantic_eligibility_baseline.py`

This is the one source-controlled checklist SE-0 requires: what the BR-1..24 program owns and
finished, versus what this program builds. Every "verified" line below was checked against code
at the baseline commit, not described from memory.

## 1. Boundary: landed (BR program — consume, never rebuild)

| Fact | Evidence |
|---|---|
| V2 registry: 317 atomic recipes, 1,195 operands (298 deterministic / 11 conceptual / 8 model-output; readiness 3 authorable / 295 blocked / 19 conceptual) | `recipe_registry_v2.py`; partition pinned by baseline test |
| Legacy registry frozen at 157 templates; alias map covers all 157 exactly | BR-17 id-set pin; baseline test `test_alias_map_covers_every_legacy_template_exactly` |
| Suggestion contracts v1/v2 byte-frozen; v3 carries V2 execution truth behind `FEATUREGEN_SUGGESTION_CONTRACT_V3` | `test_suggestion_contract.py`, `test_suggestion_identity.py`, `test_suggestion_trace_differential.py` |
| Review governance live: append-only store (migrations 1060+1061), validity fold, routes, Recipe-reviews UI | BR-23; deployed and verified on the kind cluster 2026-08-11 |
| Rollout controls live: `recipe_rollout.py` flag family, frozen defaults, allowlists, 8-reading canary gate | BR-24 |
| Considered-set response byte-stability | `test_contract_scoped.py::test_no_scope_call_is_byte_unchanged` |

## 2. Boundary: NOT landed (this program's work)

| Gap | Verified evidence at baseline |
|---|---|
| Hypothesis path still grounds legacy templates | `gate1._template_candidates(templates=ALL_TEMPLATES)` default; pinned by `test_hypothesis_path_still_grounds_legacy_templates_today` |
| Binder ignores most V2 declarations | `recipe_operand_policy.py` consumes `operand_class` only for ambiguity codes; `allowed_source_grains`, authority floors, join/temporal roles, unit expectations unenforced |
| LLM validator types every input as a measure | `feature_assist.py` builds `measure_refs=tuple(pairs)` — all `derives_from` pairs |
| Frontend omits `validation_status`/`requirements` on hypothesis candidates; renders unconditional `verification` badge | `frontend/src/api.ts` FeatureIdea shape; `WorkbenchScreen.tsx` badge |
| Suggestion `semantic_context_hashes` / `dataset_profile_hashes` hardcoded empty | `suggestion_contract.py` |

## 3. Live-catalog metadata measurements (kind cluster, 2026-08-11)

| Measure | Value |
|---|---:|
| Catalogs | 2 |
| Column nodes | 237 |
| Columns with ACTIVE concept evidence | 229 (97%) |
| Active concept evidence by producer/strength | `llm/proposed`: 229 (100%) |
| Declared or human-confirmed concept facts | **0** |
| Operand suggestion floors requiring ≥ `declared` | 1,195 / 1,195 |

Two consequences, recorded as gates:

1. **Enrichment coverage is NOT the bottleneck on this cluster** (97% of columns carry a
   concept proposal — the dev catalogs are enriched). Coverage must be re-measured per target
   catalog; the plan's precondition stands for catalogs where the live run has not happened.
2. **Authority is the bottleneck, totally:** zero facts clear a `declared` floor. Until the
   SE-4b funnel moves this distribution, authority-floor enforcement stays in shadow (SE-5
   step 8) — flipping it live today would make every candidate provisional.

## 4. Response-snapshot freeze inventory (SE-0 step 5)

Already held by existing tests; no new snapshots required: suggestion v1/v2 byte-frozen,
v3 differential-traced, considered-set byte-unchanged (see §1 table). New response versions
(SE-11's considered-set v2, SE-13's suggestion v4) must arrive as NEW versions beside these,
never as edits to frozen shapes.

## 5. Drift corrected in this commit

- `taxonomy/recipe_applicability.py` docstring said "153 legacy recipes"; the frozen registry
  holds 157. Reworded to derive from the registry rather than restate a count.
