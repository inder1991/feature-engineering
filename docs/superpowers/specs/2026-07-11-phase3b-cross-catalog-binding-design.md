# Phase 3B — Cross-Catalog Recipe Binding in Shadow: Design

> **Status:** Design, ready for planning. Phase 3B is decomposed into **four sequential sub-parts (3B.1–3B.4)**, each an independently testable increment; each gets its own task-by-task TDD plan at execution time. This document resolves the six decisions the Phase 3 spec deferred to 3B.
> **Initiative:** Phase 3B of intent-aware recipe selection. Builds directly on Phase 3A (the entity/grain graph, shipped to `origin/main`).
> **Parent spec:** `docs/superpowers/specs/2026-07-10-phase3-cross-catalog-design.md` (WHAT, invariants, asymmetric-C governance, the locked 3A contracts, the canonical acceptance scenario).
> **Branch:** to be created off `main` at execution time. **Next DB migration number is `0977`** (per the known duplicate-number wart).

## What 3B does, and what it deliberately does not

The deterministic recipe lens binds a recipe's ingredients from a **single** catalog today. Phase 3B teaches it to compute a **cross-catalog binding plan** — a recipe's ingredients bound across separately-uploaded catalogs, joined through a governed entity path — and does so **in shadow**: the plans are computed, persisted append-only, and measured against expert-authored expected plans, with **zero user-visible behaviour change**. Making the planner *live* (wiring it into grounding, enforcing its rejections) is **Phase 3C**.

3B activates the two dormant 3A edge classes (`CatalogEntityRelationshipV1`, `EntityBridgeV1`) and the proposal class (`EntityRelationshipProposalV1`), and reads the active global graph (`ENTITY_GRAPH` / `resolve_entity_compatibility`) that 3A shipped.

**The canonical acceptance scenario** (from the parent spec): target entity `customer`; `customer_master`, `transactions`, `accounts` uploaded as separate catalogs; the recipe *"average transaction value per customer over the previous 90 days"* must bind `transaction_amount`/`transaction_timestamp` from `transactions`, cross to `accounts` via a governed `account` entity bridge, roll `account → customer`, and produce one auditable binding-and-join plan at customer grain — or an explicit non-VALID status if the path is ambiguous or unsanctioned.

## Invariants (carried forward — fold into every sub-part)

1. **NO data plane.** A binding plan is a *feature-contract definition* (a declared join + aggregation plan), never a computed value or a stored row-level join.
2. **Shadow-first.** 3B computes + persists + measures plans and changes **no** disposition, ranking, or user-visible output. Flag-gated default-off. "Fail-closed" in 3B means an **explicit non-VALID status on the plan record**, not a blocked user path (that enforcement is 3C).
3. **F4 preserved.** No cross-catalog `approved_join` attestation. Cross-catalog paths are composed of *intra-catalog* joins/approved-joins stitched by *governed entity bridges*. Because the result is a contract *definition*, no cross-catalog fact is ever attested — F4 is never approached.
4. **Fail-open asymmetry.** Relevance/grain uncertainty stays soft (3A already). A cross-catalog **join** that is ambiguous, unsanctioned, or conflicting fails **explicit** — a definite non-VALID status — and the planner never silently returns a partial or degraded plan.
5. **LLM proposes, deterministic code + humans dispose.** Catalog realizations and candidate bridges are derived by **deterministic code over declared metadata**; a metadata-derived bridge is a **proposal**, never an automatically-active governed edge.
6. **Append-only / immutable.** Binding-plan records, relationship proposals, and bridge sanctions are append-only.
7. **Declared-metadata-only boundary.** 3B validates *declared* semantics (relationships, cardinality, sanctioned bridges, path uniqueness, provenance, freshness). It cannot and must not claim to validate observed join coverage, orphan/duplicate rates, or row-level alignment.
8. **Universal safety is additive-only** and runs per participating column of a cross-catalog plan.

## Decomposition

| Sub-part | Delivers | Resolves | Behaviour |
|---|---|---|---|
| **3B.1** Enriched `Template.needs` | the per-need grain/role metadata the planner reads | decision 5 | behaviour-neutral, no flag |
| **3B.2** Composite physical graph | catalog realizations + governed bridges (activate the dormant 3A edge classes) | decisions 1, 2 | additive, nothing user-visible |
| **3B.3** Binding planner | `CrossCatalogBindingPlanV1` + preference + conflict outcomes + freshness split | decisions 3, 4, 6 | shadow-computed, no disposition change |
| **3B.4** Shadow harness + gold set | run the planner in shadow, measure vs expert plans | shadow-first | flag-gated, log-only |

Strict dependency order: each reads the one before. 3C (live grounding) reads 3B.3's planner.

---

## 3B.1 — Enriched `Template.needs` (decision 5)

The current grounding contract is `Need(role, concept, optional)`. The planner needs three more facts per ingredient: what grain it sits at, what role it plays in the join, and its temporal role.

**The three additions (optional fields on `Need`):**

```python
class JoinRole(StrEnum):
    ENTITY_ANCHOR = "entity_anchor"   # the need that fixes the recipe's source grain
    MEASURE = "measure"               # a value aggregated/carried to the target grain
    TIME = "time"                     # a timestamp used for the window / as-of

class TemporalRole(StrEnum):
    NONE = "none"
    EVENT_TIME = "event_time"         # the event's own timestamp (e.g. transaction_timestamp)
    AS_OF = "as_of"                   # a point-in-time / snapshot anchor

# added to Need (all optional, default None -> derived):
#   source_grain: str | None      # the entity this need sits at; None -> derive from concept.entity_link
#   join_role: JoinRole | None    # None -> derive (the entity-role need is ENTITY_ANCHOR, else MEASURE/TIME)
#   temporal_role: TemporalRole | None  # None -> derive from the concept (as-of concept -> AS_OF, ...)
```

**Migration across the ~153 recipes: derive-or-default, not hand-author.** A `derive_need_metadata(template)` pass fills each unset field from existing metadata — `source_grain` from the need concept's `entity_link`; `join_role` from whether the need is the entity-role need (the first entity-linking need → `ENTITY_ANCHOR`) vs a value (`MEASURE`) vs a time concept (`TIME`); `temporal_role` from the concept's as-of/event nature. Only a genuinely ambiguous recipe gets an explicit override in `templates.py`. This keeps 3B.1 mostly mechanical.

**Behaviour-neutral, no flag:** nothing consumes these fields until the 3B.3 planner. The existing grounding path (`ground_template`, single-catalog) is untouched. Exit: every one of the 153 recipes derives + validates cleanly; the existing overlay/api suites stay green.

**Two prunes from the parent spec's original need sketch (YAGNI):**
- **No `target_grain` on the need.** The target grain is the *confirmed scope's* `target_entity` (one value per run), not a per-ingredient property; the planner rolls each need's `source_grain` up to it. A per-need target would let 153 recipes disagree with the scope.
- **No `unit`/`currency` on the need.** The existing grounding gauntlet already rejects `MIXED_UNITS`/`MIXED_CURRENCY`; cross-catalog doesn't change that check, and re-declaring it on needs duplicates a working guard.

---

## 3B.2 — The composite physical graph (decisions 1, 2)

3A shipped the **global semantic** graph (entity → entity roll-ups). 3B.2 adds the two physical layers that let a recipe actually *reach* those entities across catalogs.

### Catalog realizations (decision 2)

A `graph_edge` join (`catalog_source`, `from_ref`, `to_ref`, `cardinality`) *physically realizes* a semantic relationship when its two endpoints resolve to the relationship's entities. 3B.2 derives realizations deterministically:

- Resolve each join endpoint's entity (via `graph_node.entity` / the column concept's `entity_link`).
- If the `(from_entity, to_entity)` pair matches a **global** `EntityRelationshipDefinitionV1`, bind it → an active `CatalogEntityRelationshipV1` (carrying `relationship_id` + the resolved endpoint entities that 3A locked into the contract).
- **Cardinality conflict** — the join's declared cardinality contradicts the global relationship's → **`RELATIONSHIP_CONFLICT`**, surfaced, **never silently overridden** (the parent spec's joint-accounts example: global `many_to_one` vs catalog `many_to_many` → conflict, evidence the semantic model is too simple).
- **Unmapped pair** — a join whose entity pair has no global relationship → a `catalog_local_relationship`: usable only *within* its own catalog, **not** cross-catalog-traversable, and recorded as an `EntityRelationshipProposalV1` for later governance.

Realizations depend on what's uploaded, so they're derived per `catalog_source` and cached (keyed by source + a derivation version), not baked in-code like the global registry.

### Governed bridge transition (decision 1) — the subtle one

Today `cross_join_via_entity` treats **any** two columns sharing an entity as a bridge (permissive, fail-open). The end state is **"only a sanctioned `EntityBridgeV1` is traversable; an unsanctioned shared-entity coincidence is a proposal."** But *enforcing* that gate is a behaviour change — which 3B (shadow) may not make. So 3B threads it:

- **Derive candidate bridges** from shared-entity columns across catalogs → append-only `EntityRelationshipProposalV1` (bridge proposals) with provenance (which columns, which entity, which catalogs).
- Provide a **minimal sanction path**: a proposal, once reviewed, materializes an active `EntityBridgeV1` in an append-only governed store. Code/admin only — the **ratification UI is Phase 3C** (same way 3A deferred its UI).
- The **shadow planner** may traverse a proposed-but-unsanctioned bridge, but the resulting plan carries an explicit **`UNSANCTIONED_BRIDGE`** status. In shadow this is *measured*, not blocked; in 3C it becomes an actual rejection. This is exactly what makes the shadow run valuable: it surfaces every bridge the system wants, so a human sanctions the legitimate ones during the gold-set review.
- The old permissive `cross_join_via_entity` / `find_cross_catalog_path` stay **dormant** — the new planner uses only the governed store (+ proposals-with-status).

### The composite graph

A read model composing three edge classes by role (never one numeric priority — the 3A "precedence-capable, not one score" rule): **global semantic** edges (from `ENTITY_GRAPH`) validate the relationship; **catalog realizations** validate the physical hop within a catalog; **sanctioned bridges** validate cross-catalog identity. Built per-run from the active global graph + the run's catalog realizations + the sanctioned bridge store.

**Storage (migrations `0977`+):** an append-only `entity_bridge` governed store; an append-only `entity_relationship_proposal` store (bridge + local-relationship proposals); realizations may be a cache table or derived on demand.

---

## 3B.3 — The cross-catalog binding planner (decisions 3, 4, 6)

For each **applicable** recipe (3A applicability) and the confirmed `target_entity`, the planner composes intra-catalog realization paths + sanctioned bridges + entity roll-ups into a binding plan.

### The plan (`CrossCatalogBindingPlanV1`)

```python
class CrossCatalogPlanStatus(StrEnum):
    VALID = "valid"
    AMBIGUOUS = "ambiguous"
    RELATIONSHIP_CONFLICT = "relationship_conflict"
    UNSANCTIONED_BRIDGE = "unsanctioned_bridge"
    MISSING_REALIZATION = "missing_realization"
    UNKNOWN = "unknown"

class ResolutionStatus(StrEnum):
    RESOLVABLE = "resolvable"
    BLOCKED_BY_STALE_CATALOG = "blocked_by_stale_catalog"

# CrossCatalogBindingPlanV1 (illustrative — the plan pins exact fields):
#   recipe_id, target_entity
#   ingredient_bindings: tuple[...]        # each need -> (catalog_source, object_ref)
#   entity_path: tuple[...]                # the semantic roll-up hops (from resolve_entity_compatibility)
#   bridge_refs: tuple[...]                # the sanctioned/unsanctioned bridges used
#   participating_catalogs: tuple[str, ...]
#   provenance: tuple[(catalog_source, object_ref), ...]
#   preference_tier: int                   # 1 single-catalog, 2 one-bridge, 3 multi-bridge
#   bridge_count: int
#   status: CrossCatalogPlanStatus
#   resolution_status: ResolutionStatus
#   graph_version, planner_version
```

### Conflict outcomes (decision 3)

A plan is **`VALID`** only when it has a complete, sanctioned, unambiguous, conflict-free path. Otherwise the planner returns the single most-actionable failure, precedence: **`RELATIONSHIP_CONFLICT` > `UNSANCTIONED_BRIDGE` > `MISSING_REALIZATION` > `AMBIGUOUS` > `UNKNOWN`**. Fail-open asymmetry: anything non-VALID fails *explicit* — never a silent partial plan.

### Preference (decision 4)

Deterministic tier order: **(1)** complete authoritative single-catalog binding → **(2)** cross-catalog via one sanctioned bridge → **(3)** multiple sanctioned bridges. Prefer single-catalog (simpler, less governance). In shadow the planner **computes all tiers, picks the highest-preference VALID one, and records the tier + bridge count** so the shadow run measures how often multi-bridge is actually needed; whether multi-bridge ships *live* is a 3C/3D call. Ambiguous / conflicting / unsanctioned-only → the corresponding non-VALID status.

### Freshness (decision 6)

The plan exposes `participating_catalogs`; **resolvability reuses `resolve_fact`'s existing per-source drift guard unchanged** (already fails closed unless every catalog a fact spans is fresh). Keep **plan-authorable** (`status = VALID`) distinct from **resolvable-now** (`resolution_status`): a stale participating catalog yields `BLOCKED_BY_STALE_CATALOG`, never a permanent `unbuildable`. Shadow measures both.

Exit: the planner returns the correct `status`/`preference_tier`/`resolution_status` for the acceptance scenario and for synthetic conflict / ambiguity / unsanctioned-bridge / missing-realization / stale-catalog cases; **no disposition or ranking output changes** (shadow).

---

## 3B.4 — Shadow harness + expert gold set (shadow-first)

Mirrors the Phase-1A recognition-shadow pattern.

- **Shadow hook** (flag-gated, e.g. `FEATUREGEN_INTENT_CROSS_CATALOG_SHADOW`, default off, log-only): on an entity-scoped, multi-catalog run, run the planner over the applicable recipes and **persist the plans + statuses append-only** (a `cross_catalog_binding_plan` shadow store, migration `0977`+). No disposition/ranking change.
- **Expert gold set:** a set of `(recipe, uploaded catalogs, expected plan/status)` cases authored with domain input (moved to `src/` so the run is self-contained, like `gold_recognition.py`).
- **Eval module** (like `recognition_eval`): computes **binding recall** (did the planner find a VALID plan the expert expected), **incorrect-path rate**, **ambiguity-detection rate**, **unnecessary-bridge use**, **provenance completeness**, and **freshness-participant completeness**.
- **Exit gate to 3C:** agreed thresholds on those metrics + a **human review of the gold set** and the surfaced bridge proposals — the same shape as the 1A recognition gate. Only when it holds does 3C enforce.

---

## What 3B does NOT do (deferred to 3C / later)

- **Enforcement** — making `UNSANCTIONED_BRIDGE`/`RELATIONSHIP_CONFLICT` actually reject, and wiring the planner into live grounding so the deterministic lens stops being skipped on multi-catalog runs. 3B only *measures*.
- **The review UIs** — bridge ratification, realization-conflict review, and any Gate-#1 surface for cross-catalog plans.
- **The multi-bridge live decision** — 3B measures multi-bridge usage; whether tier-3 plans are offered live is a 3C/3D call.
- **Row-level anything** — the declared-metadata boundary holds; no join-coverage/orphan/alignment validation, ever.

## Scope boundary (holds for all of Phase 3)

3B validates declared entity relationships, declared cardinality, sanctioned bridge usage, metadata-level temporal semantics, freshness of all participating catalogs, path uniqueness, and source provenance. It does **not** validate observed join coverage, orphan/duplicate-key rates, row-level temporal alignment, or empirical leakage.

## Testing approach

- **3B.1:** every recipe's needs derive + validate; a per-field derivation test (grain from concept, role from position, temporal from concept); existing grounding suites byte-identical.
- **3B.2:** realization derivation from real `graph_edge` joins; cardinality-conflict → `RELATIONSHIP_CONFLICT`; unmapped join → local-only + proposal; bridge derivation → proposal; sanction path proposal→active bridge; composite graph builds; no user-visible change.
- **3B.3:** the acceptance scenario → a VALID one-bridge plan at customer grain with correct provenance + participating_catalogs; synthetic cases for each non-VALID status + precedence; single-catalog preferred over an equivalent bridged plan; stale participating catalog → `BLOCKED_BY_STALE_CATALOG` (plan still `VALID`); **no disposition/ranking change** (a shadow-off run is byte-identical).
- **3B.4:** the shadow hook persists plans append-only under the flag and is a no-op with the flag off; the eval computes each metric over the gold set; a fixture gold case with a known expected plan scores as expected.

## Named 3C decisions to record now

- **Enforcement fold-in:** where the planner's status becomes a real disposition (`UNBUILDABLE` for `UNKNOWN`/`MISSING_REALIZATION`? a new cross-catalog disposition? `UNSANCTIONED_BRIDGE` → rejected), and the flag that flips the deterministic lens on for entity-scoped multi-catalog runs.
- **Bridge ratification UX** — how a data owner sanctions a proposed bridge (the append-only sanction event + the UI).
- **Multi-bridge live** — offer tier-3 plans, or cap at one bridge for v1?
- **`AMBIGUOUS` presentation** — how a multi-plan ambiguity is surfaced to the human for disambiguation at Gate #1.
- **Consumer `AMBIGUOUS` handling** — `ranking.py`/`contract.py` gained the reserved `EntityCompatibility.AMBIGUOUS` note in 3A; 3C must teach them to handle it when the planner surfaces multi-path plans live.
