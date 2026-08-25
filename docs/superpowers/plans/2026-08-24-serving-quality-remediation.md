# Serving-Quality Remediation — 2026-08-24

**Parent evidence:** the 135-candidate quality audit (AML run `grun_01M0SQYJ2AAEZY38M9X9XCNMB6`,
SME-keep 0/135), the target-proposal post-mortem (`cust_susp_flg`), the intent-rejection
post-mortem (8/8 on a schema/parser contract gap), and the interlock/frontend-masking incident.
Owner approved the direction in conversation (2026-08-24). Branch:
`feature/serving-quality-remediation` off main `463498ed`. **Zero new migrations. Zero new flags.**

**The one law of this program:** the platform must never present confidence it does not have —
a card with unbound required operands, a DESIGN-CHECKED badge over a FORMULA_BLOCKED recipe, a
proxy target without a proxy label, and a schema that accepts what the parser refuses are all the
same defect.

## Tasks (each: fresh implementer + fresh reviewer, red-first pins, pasted gates)

### T1 — The intent contract: schema tells the truth the parser enforces
`enrich_llm.py:1598` (`feature_intents` v1): `output.output_type` and `output.unit_kind` become
schema **enums** of `recipe_contract_v2.OUTPUT_TYPES` / `UNIT_KINDS` (imported/derived, never
re-spelled — `computation_kind` above shows the house pattern), so the provider's structured
output cannot return off-vocabulary values. Belt: a small normalization map at the parse seam
(`days→duration_days`, `count_rate→rate`; `unit_kind=boolean` → move to `output_type` iff
output_type absent/consistent) applied BEFORE `_closed`, each application recorded in the
rejection-free trace. Follow the registry's own versioning convention for schema evolution (read
how sibling schemas evolved before deciding v1-in-place vs v2). REPORT, do not implement:
`categorical` has no home in `OUTPUT_TYPES` — a governed-vocabulary extension is an owner/design
decision; the task documents which of the 8 rejected intents remain unrepresentable.
Pin: replay the 8 rejected shapes from the run's audit → ≥6 parse; the 2 vocabulary-gap ones
reject with a detail naming the missing vocabulary entry, not INTENT_REJECTED_PARSE generic.

### T2 — Serve no card whose required operands are unbound
`recipe_planning_lens`/`semantic_projection`: a candidate with ANY unbound required operand never
reaches `ideas`/`actionable` — it goes to an explicit `needs_setup` lane carrying WHICH concepts
are missing and (when derivable) which catalog has them. Kills the 135-noise-cards failure shape.
Pin: the audit's cib arrangement → 0 served cards + a typed needs-setup summary.

### T3 — Badges tell the corpus's truth
`semantic_projection.py:156`: `verification` derives from corpus `readiness` + validation status;
`FORMULA_BLOCKED` never renders as `DESIGN-CHECKED`. Pin: the 132-blocked case.

### T4 — The projection serves the corpus's riches
`semantic_projection.py:142-172` (`_served_idea`): `rationale` ← `business_definition` (43/43
populated) + `decision_context`; `aggregation` derived from `formula.result_class`/
`output.aggregation_over_time`; operands projected into `measure_refs`/`time_ref`/
`operation_kind`. Never read `conceptual_reason` (0/43, forbidden on executables).
Pin: a served card carries non-empty rationale byte-derived from the recipe.

#### T2-T4 OPERATOR CONSEQUENCES — the second identity move (written down 2026-08-24, post-review)

**▲ The card edit re-mints OPTION identity.** Every field on a served card rides
`gate1._idea_json` into `canonical_candidate_identity_hash` → `option_id` →
`considered_content_hash`. T2/T3/T4 change four of them (`verification`, `aggregation`,
`rationale`, `operation_kind`, plus the typed operand refs), so the hash moves for **every
engine-served card**. Measured by execution at both ends of the task, same construction, for
`net_transaction_flow`:

| | identity hash | verification | aggregation | rationale |
| --- | --- | --- | --- | --- |
| parent `d27cae66` | `85bdaca3…` | `DESIGN-CHECKED` | `None` | *(empty)* |
| after T2-T4 | `dffc17d1…` | `UNVERIFIED` | `sum` | the business definition |

The value is pinned in `tests/featuregen/overlay/upload/test_semantic_projection.py` with a
do-not-just-paste docstring, so the next such edit must be a conscious act, and this note must be
updated with it.

Consequences traced, and they are BENIGN — unlike T11's, which re-minted AUTHORING identity:

| Surface | Consequence |
| --- | --- |
| Option ids | Minted per generation run at revision-persist time; they never span runs, so nothing in flight is invalidated. |
| Considered-revision verification | Every check compares a STORED identity against the same stored identity (revision → its own options map), never against a recomputation, so old revisions keep verifying. |
| `considered_content_hash` | Moves for NEW runs only. It addresses the revision it was minted with; no reader recomputes it for an old one. |
| Formula/authoring identity | **Untouched.** No `_idea_json` field feeds `authoring_config_hash` — that chain runs off the planning request and the provider contract. |
| Sealed shadow work items / coupons / tombstones | Untouched, same reason. |

**The product-visible change is the point, and it is large.** `DESIGN-CHECKED` is now reachable
for exactly **3 of 317** registry recipes (the three the registry marks `FORMULA_AUTHORABLE`), so
essentially every engine card now reads `UNVERIFIED` — 12 of 12 on the enriched bank fixture. That
is the audit's finding rendered honestly, not a regression: the 132 `DESIGN-CHECKED` badges over
`FORMULA_BLOCKED` recipes were the defect. **The badge moves back on its own** the moment a recipe
earns a reviewed formula expectation; nothing here needs re-running to restore it.

**Two further consequences worth an owner's eye:**
* **A3's saveable-undecided-option lane no longer exists in practice.** Every candidate that used
  to populate it had an unbound REQUIRED operand, so `actionable_ideas` now holds only the
  bound-but-planless (UOA-mismatch) case. Undecided work is still visible — as `needs_setup` — but
  it is no longer *save_idea*-able, because it mints no option id.
* **The A6 readiness literal is re-admitted as a second opinion.** `card_verification` reads the
  registry's authored `readiness` alongside the lens's fold. A6's law is that the fold is the
  answer and the literal is an assertion the fold must not contradict; reading both here is a
  DEFENCE AGAINST A FOLD BUG (weakest-wins can only withhold a badge, never grant one), not a
  restoration of the literal's authority.

### T5 — Catalog satisfiability: refuse with directions, never serve junk
Before planning: intersect eligible recipes' required concepts with the context's concept index.
Below a floor (e.g. zero `monetary_flow` against amount-requiring recipes), a typed refusal that
NAMES the catalog that does satisfy them (the cross-catalog concept inventory is queryable).
Pin: the audit's exact arrangement (AML brief + catalog_source=cib) → typed refusal naming ftr.

### T6 — Variant collapse: 135 → 43
`recipe_planning_lens.py:196-227`: serve the primary variant per recipe; siblings ride
`param_alternatives` (already computed at :309-316) as a card control, never as sibling cards.
Pin: the run's arrangement yields ≤ recipe-count cards.

#### T5 OPERATOR CONSEQUENCES — the floor's third clause (written down 2026-08-24)

**The floor took three measured cuts.** Its rationale, its numbers and its accepted costs live in
`catalog_satisfiability.py`'s module docstring, which is the authority; the two facts an operator
needs are here.

* **A mis-aimed brief now 422s before the run is minted.** `CATALOG_CANNOT_SATISFY_SCOPE` names the
  operand class, the eligible recipes that require it, the concepts the corpus asks for in it with
  per-concept counts, and the catalogs that carry them. No run row, no scope row, no intent row, no
  provider call — the same "leave no trace" law `SEMANTIC_REQUIRES_CATALOG_SOURCE` follows, and for
  the same reason.
* **▲ The refusal fires ONLY when it can give directions.** No pre-planning statistic separates a
  mis-aimed catalog from a genuinely narrow one — measured, the audit's `cib` (5 columns, 1 of 15
  eligible recipes structurally servable) and the four-objective coverage fixtures (6 columns, 1 of
  17) are the same object by every number this seam can see. What separates them is whether another
  readable catalog carries the missing concepts. Without that clause the four-objective journeys
  were refused outright, losing the hero recipe each of them governs end to end. **Operator
  consequence: a deployment whose whole estate lacks a concept is never refused for it** — the run
  proceeds and T2's `needs_setup` lane reports the absence per card. That is deliberate, and it is
  the reason the refusal is worded as directions rather than as a verdict on the catalog.

#### T6 OPERATOR CONSEQUENCES — the third identity move (written down 2026-08-24)

**▲ The collapse re-mints OPTION identity WITHOUT changing a single field on the card.**
`gate1._candidate_identity` hashes `path` alongside the feature, and `path` is POSITIONAL
(`alternative:<set>:<index>`, from `_option_positions`). Removing the siblings changes which
candidates exist, so every survivor's INDEX moves — and `canonical_candidate_identity_hash` →
`option_id` → `considered_content_hash` move with it, while `_idea_json` is byte-identical to what
that same variant served before. This is a different mechanism from T2-T4's (which moved the FIELDS)
and worth naming: **an ordering change is an identity change here.**

Measured by execution at both ends of the task, same construction, whole engine lens on the v2bank
fixture with a "90 days" hypothesis:

| | position | identity hash |
| --- | --- | --- |
| `net_transaction_flow@window=90` before | 2 | `910f5dc6…` |
| `net_transaction_flow@window=90` after | 0 | `6ae4bdb2…` |
| `inflow_outflow_ratio@window=90` before | 5 | `2fc6c115…` |
| `inflow_outflow_ratio@window=90` after | 1 | `809bdab2…` |

Both values are pinned in `tests/featuregen/overlay/upload/test_recipe_planning_lens.py` with a
do-not-just-paste docstring, so the next such edit must be a conscious act, and this note must be
updated with it. The T2-T4 pin does NOT move: it constructs one candidate at a fixed
`alternative:0:0`, so it measures the FIELDS and this one measures the ORDER.

Consequences traced, and they are BENIGN for the same two reasons T2-T4's were — option ids are
minted per generation run at revision-persist time and never span runs; every considered-revision
check compares a STORED identity against the same stored identity, never against a recomputation.
`considered_content_hash` moves for NEW runs only. Formula/authoring identity is untouched (no
`_idea_json` field feeds `authoring_config_hash`).

**Every consumer of the multi-variant stream, decided and measured** (v2bank fixture, 7 eligible
recipes; candidates 21 → 7):

| Consumer | Decision |
| --- | --- |
| Served lanes (`ideas` / `actionable_ideas`) | 6 → 2. One card per recipe; the siblings ride `param_alternatives` on it. **This is the task.** |
| `needs_setup` (T2's lane) | 15 → 5 — **yes, one entry per recipe too.** On the audit's arrangement that is 45 → 15. The lane reports which CONCEPTS did not bind, and the binder chooses columns per role rather than per parameter, so the sibling entries were near-duplicates of one answer (see the C9 caveat in the row below for the one axis on which they can genuinely differ). |
| Dispositions (`grounded_ids` / `rejected_ids`) | 2 → 2 and 5 → 5: **unchanged**, because both are keyed on `recipe_id`, never on `variant_key`. ▲ One real narrowing: a recipe's disposition is now its PRIMARY variant's, not a union over its parameterizations. The binder is variant-invariant except for C9's history-depth law (which reads `window_days`), so a recipe whose 30-day variant bound and whose 180-day variant did not now reports the 30-day answer alone. That is the answer belonging to the card that is served. |
| Workbench SQL budget | **391 → 198 measured** (non-replay SELECTs 14 against the 40 ceiling). The per-candidate observation writes and the tie-break replay lookups both fall with the candidate count. `SQL_BUDGET` stays 600: it is a ceiling that exists to catch a per-candidate read coming back. |
| `semantic_candidate_store` observations / option-decision rows | One per served candidate, so they fall with it. Nothing keyed on `variant_key` breaks: the surviving variant keeps its exact key. |
| `_engine_recipe_contexts` (formula-shadow capture, private revision) | Its leading-variant fold is now trivially satisfied for the recipe lens. **Kept**, because it folds over every served idea including LLM intents, and its docstring now says so instead of describing B5. |
| `suggestions.semantic_parity_block` (per-table page) | One entry per recipe instead of one per parameterization — the same collapse, on the surface that shares the lens by construction. |

**The product-visible change, and its one honest limitation.** The 43-recipe AML arrangement can no
longer produce 135 cards; it produces at most 43. Each surviving card names its alternatives
(`param_alternatives`, e.g. `window: 30/[90]/180`). **Choosing a different one is not yet an
action** — the primary is picked by the deterministic hypothesis token match, so an operator gets
the 180-day variant by saying "180 days" in the hypothesis and re-running, not by clicking the
control. Making that control actionable is a route+frontend change nobody has chartered; it is
listed here rather than done, because inventing a parameter-override seam is a bigger decision than
this task.

**▲ AND THE COST OF THAT LIMITATION, WRITTEN DOWN (the reverse direction, added 2026-08-25 after
review).** The section above recorded only the favourable half. When the primary variant is the one
C9's history-depth law BLOCKS, T6 serves nothing where the parent served a working sibling — and
that is the LIKELIER path, because real hypotheses carry window tokens and the audit's own did.

Measured on the v2bank fixture with `history_depth_days=60` declared on `transactions`
(net_transaction_flow and inflow_outflow_ratio both author window ∈ 30/90/180):

| hypothesis | parent (all variants) | T6 (primary only) |
| --- | --- | --- |
| **"money moves over 180 days"** | candidates `@30, @90, @180`; **`@30` served as a card**, `@90`+`@180` to needs_setup. Whole projection: **ideas 2**, needs_setup 19 | candidate `@180` only → blocked → needs_setup. Whole projection: **ideas 0**, needs_setup 7 |
| "money moves between customers" *(no token)* | candidates `@30, @90, @180`; `@30` served. ideas 2, needs_setup 19 | candidate `@30` only; `@30` served. **ideas 2**, needs_setup 5 |

So: with no window token the two agree exactly; with a window token naming a depth the catalog
cannot cover, **the operator goes from two cards to zero**. The parent hid the block behind a
sibling that answered a question nobody asked (a 30-day figure for a "180 days" brief); T6 reports
the block and offers nothing. Both are defensible — serving a silently-different window is the
confidence-without-warrant this programme exists to remove — but the second is a REAL loss of
served work, not a neutral cleanup, and it belongs on the record beside the 135→43 win.

The design answer is the `param_alternatives` control becoming actionable (offer the 30-day
variant *as* the alternative it already names, instead of silently substituting it). That stays
unchartered per the paragraph above; this note is the reason it is worth chartering.

### T7 — Target intake honesty
(a) abstain-by-default when no outcome-family concept exists — answer names the nearest proxies;
(b) deterministic goal-text window extraction; a ticket whose `target_window_days` contradicts
the goal's stated horizon is a typed refusal (0 vs "next 90 days");
(c) proxy disclosure: confirming a target whose concept is not outcome-family requires the
banner + records `target_is_proxy` on the intent.
Pin: this run's exact goal text + catalog → abstention listing `cust_susp_flg` as PROXY ONLY.

### T8 — Counterparty ≠ population
A `counterparty`/`payer`/`payee` operand must not bind the population's own grain key (extends
`distinct_binding_group`). Pin: the "New counterparty → cust_num" case refuses.

### T9 — Frontend truth-telling
(a) `WorkbenchScreen.tsx:787`: render the response's own `detail` for 503s, never the hardcoded
"no LLM provider" sentence; (b) the `needs_setup` lane and proxy banner render honestly
(vocabulary per the honest-absence rule).

### T10 — Journey + suites
One journey: AML brief on cib → T5 refusal naming ftr → re-aim at ftr → cards served ≤43, all
badges truthful, rationale non-empty, intents parsed. Full suites vs main baseline; STOP (no
deploy — cutover on the owner's word).

## Deliberately OWNER-GATED, not in this program
Vocabulary extension (`categorical`); enrichment re-review of the 94 bulk confirmations;
`booking_status`/`corridor`/alert-family concept mapping (SME session); cross-catalog planning
(chartered elsewhere — d9's program); live activation ceremony for the cross-catalog interlock;
ingesting true AML review labels; the free-form wire into hypothesis flow (owner said yes — but
T1/T2 change what it would serve; re-scope AFTER this program lands).

### T11 — The author schema the provider accepts (diagnostic-sourced, 2026-08-24)
Live diagnostic (draft fd_01M0SZTAJCQDR0KG4JPV16T9ZP): every `formula.author` call dies with
`anthropic rejected structured-output schema (HTTP 400, keyword=type)` — schema
`formula_author_turn_v3`'s `$defs.typedLiteral.properties.type` and
`$defs.parameterDecl.properties.type` are BARE ENUMS (`{"enum": [...]}` with no `"type"`), which
the provider's structured-output subset refuses. This has blocked ALL live LLM formula authoring.
Two layers: (a) source — the two subschemas gain `"type": "string"` (find the construction site
in `formula/turns_v3.py`; check v2 for the same shape); (b) general — `project_for_anthropic`
normalizes any enum-only subschema by inferring its type from the enum values (all-strings →
"type":"string"), the same defensive philosophy as its existing type-array normalization.
Pin: a SCHEMA-AUDIT test that walks EVERY registered LLM schema + the formula turn schemas
post-projection and asserts the provider-subset invariants (every subschema carries a type; no
list-valued type; enums typed) — the class-killer that would have caught this AND prevented the
next one. Red-first against the unfixed v3 schema.

#### T11 OPERATOR CONSEQUENCES — the identity move (written down 2026-08-24, post-review)

**The census was wider than the diagnostic:** 17 bare enums in v3, 15 in v2, 14 in v1 — 46 in all,
not the two the 400 named (v3's two beyond v2 are ``semanticRowSelection.properties.kind`` and
``.semantic_value``). v1 is fixed on the wire by the projection but still bare at rest — and the
review established the stronger fact: **v1 is production-dead** (``AUTHORABLE_EXPECTATION_SCHEMAS``
is a one-element v2 set, and the live draft worker calls only ``run_authoring_v2_replay`` with
``turn_contract ∈ {v2, v3}``). "Not fixed" and "cannot be reached" are different operator facts;
this is the second one. One line if at-rest uniformity is ever wanted.

**Why the schemas were audited by nothing.** Both existing auditors — the 2026-08-14 ratchet and
`enrich_llm.register_enrichment_schemas`' fail-closed bootstrap guard — iterate `enrich_llm._SCHEMAS`,
which carries no `formula_author_turn*` id. The primary blind spot was COVERAGE, not detection; the
detector defect (`provider_incompatibilities` treating `enum` as dispatchable) was real but second,
and would have mattered only once something looked. Both are now fixed.

**▲ Layer 1 re-mints authoring identity.** `freeze_provider_contract` hashes the schema BYTES, so
declaring the enum types moves `schema_content_hash` → `contract_hash` → `authoring_config_hash` →
`formula_identity`, for **both v2 and v3** (verified by execution; all six hashes moved). Layer 2
alone — a wire-only projection — would have fixed the outage and moved nothing. Layer 1 was chosen
anyway, because a recorded schema that misdescribes what was actually sent is precisely the
confidence-without-warrant this program exists to remove. The two hashes are now pinned by value in
`tests/featuregen/intake/test_provider_schema_audit.py`, so the next such edit must be a conscious
act, and this note must be updated with it.

What moves downstream the moment the new image runs, traced:

| Surface | Consequence |
| --- | --- |
| Sealed shadow work items | `ConfigurationDrifted` (typed, expected) — they need re-sealing. |
| Candidate-wide tombstones | **HOLD.** 1103's scope-key design is vindicated under exactly this event. |
| ▲ EXACT-draft tombstones | **SILENTLY STOP COVERING the LLM lane.** By design per 1103, but an operator who retired an exact draft will find it re-draftable with no notification. *This note is the notification.* |
| In-flight regeneration coupons | Permanently unredeemable. Fail-closed, uses unburned, re-approval required. |
| Existing FAILED drafts | Re-mint WITHOUT exception. Correct — different configuration, and those failures were *caused by the defect being fixed* — but it is a real spend-authorization consequence. |
| Deterministic lane | Untouched. |

**Also closed in the correction round:** the `x-wire-enum` swap mints an `enum` during projection,
so declaring types upstream of it left a second door into this same outage class open; the
declaration now runs downstream of both producers, pinned. `_type_of_enum_members` gained `number`
and `null`. Rider: `recipe_formula_worker.py`'s header claimed it "authors BOTH generations" — the
v1 arm was retired long ago, and the stale sentence misled this very adjudication.
