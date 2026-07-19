# Phase 3C.2b-i — Governed LLM Cross-Catalog (Shadow) — Parent Decomposition

**Status:** approved decomposition; child specs drive planning (A first)
**Date:** 2026-07-19
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` — currently based on `8636b4d`; **must rebase onto current `origin/main` (`d90d457`+) before implementation** (finding #14) and re-confirm migration numbers.

## Why this is two slices, not one

3C.2b makes LLM-proposed **cross-catalog** feature ideas governable (compiled through the deterministic planner) instead of blanket-rejected, then removes the permissive `entity.find_cross_catalog_path`. The original single-adapter framing was wrong: `plan_bindings` **cannot** assemble operands spanning >1 catalog, confirmed on `origin/main`:

- `enumerate_single_catalog_plans` requires every non-optional need to bind within one `catalog_source` (an out-of-catalog operand → `partially_resolved`, never `resolved`).
- `_assemble_rollups` (`planner/plan.py:238`): *"Only RESOLVED tier-1 plans supply source bindings"* — it moves one resolved **single-catalog** computation to a target grain; it never combines operands from different catalogs.

The load-bearing work is therefore a **governed multi-source operand assembly** capability, with a FeatureIdea normalizer riding on top. Split so the risky new planner capability is proven before an untrusted normalizer feeds it.

## The two children

- **[3C.2b-i-A — Governed Multi-Source Assembly](2026-07-19-phase3c2b-i-a-governed-multi-source-assembly-design.md)** (planner capability). Accepts an already-authoritative, fully-typed `MultiSourcePlannerIntentV1` (pinned operands + authoritative concepts + **governed structural bindings** + **per-operand path strategy** + an ordered **final expression**). Builds an independent governed path per operand over **VERIFIED** bridges, **converges every path to one exact physical landing `{catalog, table, grain_key}`** (the final governed join point — not merely the same logical entity), proves cardinality/aggregation/temporal correctness, asserts operand + slot preservation, and compiles via a **new `compile_multi_source_contract`** (the existing `compile_contract` takes one `BindingPlanV1`+`Template`). New `MultiSourceBindingPlanV1`. Shadow-only, driven by a **synthetic gold set** with immutable expected outcomes; may test `RATIO`/`DIFFERENCE` via authored numerator/denominator (planner preserves ordered roles, never infers them).

- **[3C.2b-i-B — FeatureIdea Adapter](2026-07-19-phase3c2b-i-b-feature-idea-adapter-shadow-design.md)** (untrusted→authoritative conversion). Captures the **raw pre-`_vet` proposal** (the gauntlet silently drops unknown operands), resolves concept authority over raw evidence, assigns computation roles via a **versioned role policy over real concept fields** (`group`/`pit_role`/`additivity`), resolves a **governed** grain and time anchor, emits the per-operand strategy + final expression, and hands a `MultiSourcePlannerIntentV1` to A. Shadow-only via a **worker** off the request path. Order-sensitive ops (`RATIO`/`DIFFERENCE`) return `OPERAND_ORDER_AUTHORITY_MISSING` — no ordering from `derives_pairs`/`measure_refs` order, name, description, or rationale.

## Ordered operands: modeled now, accepted later

Ordered semantic roles (`numerator`/`denominator`, `minuend`/`subtrahend`) are **first-class in A's contracts from the start** (avoids a later planner-contract migration). A tests them via synthetic gold. B does not yet populate them from LLM input — the concept registry carries `group/additivity/pit_role/entity_link` but **no feature-algebra role declarations**; a later slice adds a governed operation-signature registry or a human-confirmed ordered intent.

## Shared invariants (both children)

1. **Shadow-only** — log, measure, never surface. No data plane; no signing.
2. **Authority, not display** — governed operand concepts and structural grain/key bindings come from source-attested/human-confirmed evidence, never `graph_node.concept`/`is_grain` (advisory) or the always-null `concept.load_bearing_value_hash`.
3. **No inference latitude in governed output** — heuristics only ever produce shadow diagnostics that never resolve and never count toward a gate.
4. **Fail-closed** — missing/ambiguous/conflicting/stale/unregistered/untypeable/lossy input rejects. Never guess.
5. **Preservation is proof** — a resolve preserves every original operand, its semantic slot, and the operation; "compiled" alone is not proof (the compiler validates physical-hop aggregation, not the final expression or ordering).
6. **Technical ≠ semantic ≠ incomplete** — DB/infra failures are *technical* outcomes; budget truncation is *capture-incomplete*; neither is a semantic reject and neither is a resolve.
7. **F4 preserved** — output is a contract definition with a governed physical plan, never an attested cross-catalog `approved_join`.
8. **Capture integrity** — each shadow harness uses a run manifest + expected set + two-phase writes + reconciliation (the `0999` pattern) so a dropped write is detectable.
9. **Bounded** — per-run idea/operand/catalog/path caps + one shared elapsed-time and compile budget + deterministic truncation telemetry.
10. **Gold is partitioned** (findings #7, #11) — each gate splits gold into a **correctness population** (immutable expected outcomes; positive cases that MUST resolve with the exact expected physical landing/paths/slots/expression — a reject-everything implementation fails) and **fault-observability controls** (injected DB error / truncation / authority drift — pass when *exactly classified*, excluded from the clean population). Each gate also defines the minimum distinct authoritative shapes per activatable cohort. Resolution *rate on real traffic* stays descriptive.

## Dependency: concept AND structural authority provisioning (findings #5, #9)

B accepts only `(SOURCE,ATTESTED)`/`(HUMAN,CONFIRMED)` evidence, but **no production writer attests `concept` today**: enrichment writes `LLM/PROPOSED`; the `d90d457` source-capability profiles attest `unit/currency/entity/data_type/...` but **not** `concept`; no human concept-confirmation writer exists. On real traffic B would classify every operand `CONCEPT_AUTHORITY_MISSING`. Provisioning rules: a source profile may attest `concept` **only** when the source **explicitly supplies a canonical concept under a governed capability contract** — an LLM-derived mapping must **never** be relabeled `SOURCE/ATTESTED`. Provisioning must also cover **structural authority** (grain / time anchor / key), since FTR carries no structural fields and concept alone still leaves those unavailable. Before 3C.2b-ii both are required, and B's gate must prove a **non-vacuous** attested/confirmed population per activatable cohort — not just "zero false resolves."

## Sequencing gate

Both children are shadow-only and independently gated. **3C.2b-ii (the live flip + `find_cross_catalog_path` removal) may proceed only after A's assembly gate AND B's normalization gate independently pass**, and the authority-provisioning dependency is met. Plan **A first**; B's plan waits until A's compiler and plan-carrier interfaces land and A's exact-outcome gold gate passes.

Migrations: A = **1005**, B = **1006** (after the rebase over `1004_ingestion_run_source_profile.sql`); re-confirm free at build time.
