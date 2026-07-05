# Feature-Engineering Loop — design

Date: 2026-07-05. Status: design (blueprint for the next backend chapter). Builds on the
[upload-catalog pivot](2026-07-04-upload-catalog-pivot-design.md) + the merged v1 catalog/feature layer.

## Thesis
A **deterministic backbone** (ingestion → facts → an **entity-anchored** knowledge graph → search) with
the LLM confined to **bounded, audited judgment nodes**, feeding a **code-owned generate-validate-refine
loop** that assembles features **across domains** anchored on **entities**, gating every write on a
human. The intelligence is in the *composition*, not an autonomous agent. **The LLM reasons about FIT
(relevance/coverage against a stated hypothesis) — it never predicts performance.**

## Principles (the spine — non-negotiable)
1. **Deterministic control flow, bounded LLM nodes.** The graph, joins, validation, and the loop itself
   are code; the LLM only *proposes/classifies/recommends*, and every output is checked by deterministic
   tools before it counts.
2. **Entity-anchored, cross-domain.** Features hang on entities (Customer/Account) that span catalogs —
   that's what makes it *the customer's* feature, not *a deposits* feature.
3. **Fail-closed, audited, read-scoped, everywhere.** Stale → not served; every LLM call recorded; PII
   gated by the session's roles at every read (incl. the candidate menu).
4. **Advisory vs load-bearing.** LLM output is advisory; the only mutation (`register_feature`) is
   human-gated.
5. **LLM = fit/coverage reasoner, not a performance predictor.** Without data it cannot know which set
   performs best; it reasons over metadata + the hypothesis and recommends, *caveated*. Truth = backtest.

## The stack (built vs missing)
| Layer | State |
|-------|-------|
| Ingestion (parse → map → validate → brake) | built |
| Catalog + facts (event-sourced, drift, fail-closed serve) | built |
| Knowledge graph (tables/columns/concepts/domains + joins) | built; **entity layer MISSING** |
| Enrichment (concept/domain/definition, audited) | built (real-provider) |
| Search (full-text + graph-rank + read-authz, cross-catalog) | built |
| **Feature loop (cross-domain, entity-anchored, validated)** | **MISSING — this doc** |
| Serving (resolve facts, features, lineage/impact/freshness) | partial |

## The entity layer (the keystone — MISSING)
Cross-domain is impossible without an anchor. Add:
- **Entity nodes** (`Customer`, `Account`, …) + **`entity_membership` edges** (column → entity), spanning
  catalogs. So `deposits.cust_ref`, `cards.cust_id`, `loans.customer_no` all → the one `Customer`.
- **Resolution**, in confidence order: (1) the declared `entity` field (trusted); (2) an LLM *suggestion*
  from name/type/concept similarity — **human-confirmed**, especially cross-source; (3) shared-concept
  matching. Resolution is advisory + human-gated (no-DB: can't verify by value overlap).

## Cross-source join-path (MISSING)
Extend `find_join_path` to traverse **cross-catalog** join edges + entity-membership, anchored on the
shared entity — so it can route `cards.spend → Customer → deposits.balance`. Today it is single-source.
Cross-source joins are declared/entity-resolved, **not value-verified** → lower confidence → **the join
is surfaced for human confirmation** before a feature that uses it is registered.

## The loop (the headline — today `recommend_features` is single-shot and ignores its own checks)
```
INPUT: hypothesis (structured), objective, target label (optional), anchor entity, timeframe

repeat until (enough validated features) OR (iteration budget spent):
  ① GATHER   (deterministic, CROSS-DOMAIN) — resolve the entity → candidate columns from EVERY
             catalog linked to it, widened by relevant domains, read-scoped to the user's roles.
  ② GENERATE (LLM node — the ONLY non-deterministic step) — propose features from the cross-domain
             menu + last round's FEEDBACK.
  ③ VALIDATE (deterministic gauntlet — every candidate, every pass): ground · assemble (cross-source
             join path) · leakage · freshness · aggregation-safety (additivity/unit) · point-in-time.
  ④ ACCEPT survivors; collect REJECTIONS + reasons.
  ⑤ REFINE → summarize rejections into FEEDBACK for the next GENERATE.

OUTPUT: ranked, validated feature recipes — each with its cross-domain join path explicit.
HUMAN GATE: analyst confirms (esp. cross-source joins) → register_feature.
AUDIT: every LLM call recorded; every accepted feature carries provenance + freshness lineage.
```
Properties: **code owns the loop** (budget/target/stop — no LLM spiral); the **gauntlet is deterministic**
(a leaky feature *cannot* slip through); non-determinism is fenced to GENERATE, sandwiched by
deterministic gather + validate; refinement compounds each round.

## The validation gauntlet (deterministic — the pieces EXIST, need wiring into the loop)
| Check | Backed by | Reject when |
|-------|-----------|-------------|
| ground | column grounding | a derives-from column doesn't exist |
| assemble | cross-source `find_join_path` | no join path / target not loaded |
| **leakage** | `leakage_check` | uses/derives the target label |
| **freshness** | `feature_freshness` (cross-source) | any source stale |
| **aggregation safety** | `additivity`/`unit` metadata | unsafe SUM (semi/non-additive) / wrong unit |
| point-in-time | the table's `as_of` | no as-of filter (future leakage) |

## Multi-set advisory recommendation
- **Generate N diverse sets** — each seeded with a different *strategy/lens* (behavioral, monetary,
  cross-domain risk, engagement) so they're different *hypotheses*, not near-duplicates.
- **Every set passes the gauntlet** → the human only ever curates among **safe** options.
- **Rank on deterministic signals first** (leakage = hard gate; freshness; **domain coverage**;
  **redundancy** — correlated/duplicative features), **LLM qualitative fit second**.
- **The LLM recommends ONE set with reasoning** grounded in the hypothesis + feature/table/column
  definitions + metadata + domain/subdomain + timeframe — **advisory + caveated** ("confirm by backtest").
- **UI:** show all sets side by side; recommended one highlighted with a *"why I'd pick this"* panel +
  the caveat; each feature a checkbox (à la carte); a "take this set" per set; a live selection tray that
  **re-validates** as the analyst mixes. Safety badges (leak-free/fresh/join/additivity) visible per feature.
- **À-la-carte curation produces a NEW set that's re-validated** (redundancy, combined leakage, grain
  compatibility) before `register_feature`. Curation never bypasses validation.
- **Provenance:** every set, the recommendation + reasoning, and the human's final pick are recorded.

## The hypothesis as a first-class input
The analyst states a **structured hypothesis** (the yardstick), and the LLM assesses how well each
feature/set *operationalizes* it, from:
| Input | What the LLM reasons about |
|-------|----------------------------|
| hypothesis | does this capture the signal the analyst theorized? |
| feature definition | does what it computes match the hypothesized signal? |
| table/column definitions | is this column actually measuring what the hypothesis needs? |
| metadata (grain/as-of/additivity/unit/cardinality) | is the feature well-formed and appropriate? |
| domain/subdomain | coverage — does the set span the implicated areas? gaps? |
| timeframe | does the feature's window align with the prediction horizon? |

## The no-DB constraint (honest limits)
- Cross-source joins are **declared/entity-resolved, not value-verified** → human-confirm.
- **"Best set" is a structural/qualitative fit judgment, NOT a performance prediction.** The real winner
  is a **downstream backtest** (train + measure), outside this platform today.
- **Future — close the data loop:** once chosen features are computed and a model trained, feed the
  **feature-importance / backtest results back as loop feedback** — turning "recommend a starting set"
  into "learn which strategies actually work." This is the version where the platform genuinely improves.

## Governance & audit (reuse what's built)
Read-scope on every candidate read (roles from session, M6); every LLM call audited
(`enrich_llm.audited_enrich_call` pattern); every accepted feature carries provenance + freshness
lineage; only `register_feature` mutates, human-gated.

## Build plan (dependency order)
1. **Entity layer** — entity nodes + `entity_membership` edges + resolution (declared → LLM-suggest →
   human-confirm). Keystone.
2. **Cross-source `find_join_path`** — traverse cross-catalog edges anchored on the entity.
3. **The loop** — integrate the deterministic gauntlet (leakage/freshness/additivity/point-in-time,
   currently unused by `recommend_features`) + refinement into `recommend_features`; bounded budget/target.
4. **Cross-domain GATHER + multi-set advisory recommendation** — domain-guided candidate breadth; N
   diverse validated sets; the explained, caveated recommendation.

## Reuses (already built)
`find_join_path`, `leakage_check`, `feature_freshness`, `additivity`/`unit` metadata, column grounding,
`recommend_features`/`feature_recipe` (single-shot → to be looped), the audited LLM seam
(`audited_enrich_call`), `read_scope`.
