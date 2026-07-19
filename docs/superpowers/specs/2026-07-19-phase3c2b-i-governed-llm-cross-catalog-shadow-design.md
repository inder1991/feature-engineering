# Phase 3C.2b-i — Governed LLM Cross-Catalog (Shadow) — Parent Decomposition

**Status:** approved decomposition; child specs drive planning
**Date:** 2026-07-19
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` off `origin/main` (`8636b4d`+)

## Why this is two slices, not one

3C.2b makes LLM-proposed **cross-catalog** feature ideas governable (compiled through the deterministic planner) instead of blanket-rejected, then removes the permissive `entity.find_cross_catalog_path`. The original 3C.2b-i spec assumed the adapter could hand a synthetic `Template` to `plan_bindings` and reuse the existing pipeline. **That premise is false for the real cross-catalog case**, confirmed against `origin/main`:

- `enumerate_single_catalog_plans` (`planner/enumerate.py`) requires every non-optional need to bind **within one `catalog_source`**; an operand in another catalog makes the plan `partially_resolved`, never `resolved`.
- `_assemble_rollups` (`planner/plan.py:238`) states *"Only RESOLVED tier-1 plans supply source bindings"* — it seeds from the `SOURCE_ENTITY_KEY` of a resolved **single-catalog** computation and moves it to the target grain over a governed path. It never combines operands originating in different catalogs.

The population the adapter targets — ideas whose operands span >1 catalog, exactly what `_reject_cross_catalog_llm` rejects — is precisely what the planner cannot assemble. The load-bearing work is therefore a **governed multi-source operand assembly** capability, on which a FeatureIdea normalizer rides. These are separated so the risky new planner capability is proven before an untrusted normalizer feeds it.

## The two children

- **[3C.2b-i-A — Governed Multi-Source Assembly](2026-07-19-phase3c2b-i-a-governed-multi-source-assembly-design.md)** (planner capability). Accepts an already-authoritative, fully-typed `MultiSourcePlannerIntentV1`; builds an independent governed path per operand over **VERIFIED** bridges to a common target grain; proves cardinality/aggregation/temporal correctness; unions read sets; asserts operand + semantic-slot preservation; compiles the operation at the common grain. New `MultiSourceBindingPlanV1`. Shadow-only, driven by a **synthetic gold set** of authored intents (it may test `RATIO`/`DIFFERENCE` because those gold intents explicitly author numerator/denominator — the planner preserves ordered roles, never infers them).

- **[3C.2b-i-B — FeatureIdea Adapter](2026-07-19-phase3c2b-i-b-feature-idea-adapter-shadow-design.md)** (untrusted→authoritative conversion). `FeatureIdea → concept authority → deterministic computation roles → governed grain → OperationSpecV1 → MultiSourcePlannerIntentV1 → proven A planner`. Owns the concept-authority resolver, cohort model, canonical input + LLM provenance, role assignment, and the normalization gate. Shadow-only, driven by a gate hook over real LLM ideas. Order-sensitive ops (`RATIO`/`DIFFERENCE`) return `OPERAND_ORDER_AUTHORITY_MISSING` for now — no ordering is ever taken from `derives_pairs`/`measure_refs` order, feature name, description, or LLM rationale.

## Ordered operands: modeled now, accepted later

To avoid a second planner-contract migration, ordered semantic roles (`numerator`/`denominator`, `minuend`/`subtrahend`) are **first-class in A's `MultiSourcePlannerIntentV1` and `MultiSourceBindingPlanV1` from the start**. A tests them via synthetic gold intents. B simply does not yet populate them from LLM input (the concept registry today carries concept/entity/temporal/additivity metadata but **no governed feature-algebra role declarations**); a later slice adds a governed operation-signature registry or a human-confirmed ordered intent.

## Shared invariants (both children)

1. **Shadow-only** — log, measure, never surface. No customer-visible change; no data plane; no signing.
2. **Authority, not display** — governed operand concepts come from source-attested/human-confirmed evidence, never `graph_node.concept` (discovery hint) or the always-null `concept.load_bearing_value_hash`.
3. **No inference latitude in governed output** — heuristics may only produce shadow diagnostics that never resolve and never count toward a gate.
4. **Fail-closed** — missing/ambiguous/conflicting/stale/unregistered/untypeable input rejects. Never guess.
5. **Preservation is proof** — a resolve must preserve every original operand *and* semantic slot; "compiled" alone is not proof (the compiler validates physical-hop aggregation, not operation expressions/ordering).
6. **Technical ≠ semantic** — DB/infra/read failures are technical outcomes in isolated savepoints, never semantic rejections, never resolves.
7. **F4 preserved** — output is a contract definition with a governed physical plan, never an attested cross-catalog `approved_join`.
8. **Capture integrity** — each shadow harness uses a run manifest + expected set + two-phase result writes + reconciliation (the `0999` recipe-shadow pattern), so a dropped write is detectable.
9. **Bounded** — per-run idea/operand/catalog caps + one shared elapsed-time and compile budget + deterministic truncation telemetry; prefer an outbox/worker boundary so request latency is not customer-visible.

## Sequencing gate

Both children are shadow-only and independently gated. **3C.2b-ii (the live flip + `find_cross_catalog_path` removal) may proceed only after A's assembly gate AND B's normalization gate independently pass.**

Migrations: A claims the next free number (**1004** at time of writing), B the one after (**1005**); both re-confirmed free at build time given recurring parallel-session collisions.
