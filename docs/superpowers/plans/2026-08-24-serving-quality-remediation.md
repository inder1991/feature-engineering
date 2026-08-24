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

### T5 — Catalog satisfiability: refuse with directions, never serve junk
Before planning: intersect eligible recipes' required concepts with the context's concept index.
Below a floor (e.g. zero `monetary_flow` against amount-requiring recipes), a typed refusal that
NAMES the catalog that does satisfy them (the cross-catalog concept inventory is queryable).
Pin: the audit's exact arrangement (AML brief + catalog_source=cib) → typed refusal naming ftr.

### T6 — Variant collapse: 135 → 43
`recipe_planning_lens.py:196-227`: serve the primary variant per recipe; siblings ride
`param_alternatives` (already computed at :309-316) as a card control, never as sibling cards.
Pin: the run's arrangement yields ≤ recipe-count cards.

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
