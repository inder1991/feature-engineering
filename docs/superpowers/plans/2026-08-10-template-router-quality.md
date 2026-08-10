# Template router quality — measured gaps and the work that closes them

**Status:** proposed, not started. Every number below was measured against the deployed cluster on
2026-08-10, after the `ftr` re-enrichment (run `ingrun_01KZKW3RW7FNSYZKZ2KYM21N4T`).

**Task numbers are identifiers, not an order — they record the sequence tasks were DISCOVERED in, and
several were added after the first draft. Read "Sequencing" for the order.** The map (S ≤ 1 day,
M = 2–5 days; sizes are rough and for sequencing only):

| task | what | surface | size | rollout |
|---|---|---|---|---|
| **6→Step 0** | funnel instrumentation (reject codes end to end) | both | S | direct |
| **2b** | content-addressed tie-break verdict store, warmed at ingest | both | M | direct (additive) |
| **2** | break tied bindings on meaning, not spelling | both | M | **shadow → flag** |
| **3** | near-label leakage critic | hypothesis flow | M | **flag-only verdicts first** |
| **4** | rank recipes by hypothesis → use_cases | hypothesis flow | S–M | log-and-compare → on |
| **4b** | hypothesis-chosen PARAMETERS (23 of 147 emitted today) | hypothesis flow | M | gated on gauntlet-cost measurement |
| **4c** | author recipes against 71 unused concepts | both | SME | per-card review |
| **1** | a template can need a concept the classifier cannot produce | both | S | direct (hygiene) |
| **7** | acceptance telemetry: which recommendations humans keep | hypothesis flow | S | telemetry only |
| **5 / 5b** | concept coverage; which source system to onboard next | both | data + SME | continuous |
| **0** | cross-catalog grounding | both | L | governance + engineering |

**Two surfaces, deliberately different.** The per-table suggestions page has NO hypothesis — it is
the curator's stable inventory of what a table can support, and its stability is a feature. The
hypothesis/contract flow (`build_considered_set`) is where a question arrives and where the
"same 23 features for every hypothesis" complaint is actually answered: Tasks 4 and 4b act THERE.
Tasks 2/2b (binding correctness) and 4c (new recipes) improve both surfaces.

## The process after this plan — plain-language walkthrough (owner-validated 2026-08-10)

The canonical end-to-end description, kept in the plan so every reader — SME, product, engineering —
shares one picture. **[code]** = deterministic · **[AI]** = model · **[human]** = a person.

**At upload, once (the prep shift):**

* **A1. Label everything** [AI, code-validated] — concept + one-line summary per column; critic
  reviews high-impact labels; `unclassified` is a legal answer.
* **A2. Make the judgement calls, pin them up** [AI, once] — ground all recipes transiently, find
  the genuine ties, decide each ONCE with a written reason, store under content fingerprints
  (`structured_result`), discard the trial grounding (the caching rule: keep only the thinking).
* **A3. Registry health** — the SME cards, the `Need` fixes, the `tran_time`-class reclassifications.

**Per hypothesis:**

* **B1. Read the question — ALWAYS** [AI, one cached call] — the mandatory intake read fills the
  full ticket: target column, label window, target type, business domain. A literally-typed column
  name is PINNED (the model cannot override it; a name-vs-prose disagreement becomes a confirm-screen
  warning).
* **B2. Human approves the target** [human] — Yes / Change (ranked runners-up) / Just exploring
  (near-label candidates withheld). On confirm: a recorded human decision.
* **B3. Fresh glance** [code, ms] — ground the registry against the live catalog. Never stored; a
  curator's correction is visible to the next request automatically.
* **B4. Read the pinned judgements** [code] — tied bindings resolve by verdict lookup, rationale
  attached; a fingerprint miss falls back to deterministic order for one request while the worker
  re-adjudicates (curator "Re-adjudicate now" button skips even that wait).
* **B5. Gauntlet** [code] — unchanged checks; a refused winner re-binds to the runner-up; every
  rejection carries a reason code.
* **B6. Leakage** [code + AI flag-only] — the veto removes anything built on the confirmed target
  (string match). The near-label critic answers ONE fenced question: *"is this feature the user's
  label definition in disguise?"* — `dormancy_days` (days since activity) vs a 90-day-inactivity
  churn label is the canonical catch; `txn_frequency_trend` (related, not identical) is the
  canonical pass. Verdicts `no_finding | too_close | abstain`; `too_close` can only ADD a warning.
  **Origin-blind**: runs on every surviving candidate, template-grounded or LLM-proposed — the same
  invented dish gets the same taste, whoever cooked it.
* **B7. Order the menu** [code — no fresh AI] — B1's `business_domain` already translated the
  user's words to the experts' tags; B7 is set-intersection against each card's `use_cases` and a
  sort. Ordering never removes.
* **B8. Tune the settings** [AI, closed choice, cached] — pick WHICH of the SME-authored parameter
  values fits this question (churn → the 90-day window; structuring → the 30-day). Off-list values
  are rejected by `_bind_params`; the model cannot invent a setting.
* **B9. Record the choice** [code] — Gate-1 selections land per recipe/domain: the feedback signal
  and, with B2's corrections, the metric set that gates every future upgrade.

**The fenced-question summary — the AI's entire request-time role:**

| step | the AI's one question | it can never |
|---|---|---|
| B1 | "what is this question's target, window, type, domain?" | override a typed name; skip the human gate |
| B6 | "is this feature the answer in disguise?" | approve — warnings only |
| B7 | (answered in B1) "which expert tags match these words?" | remove — order only |
| B8 | "which of the allowed settings fits?" | invent a setting — list only |

The model translates and judges MEANING — the one thing code cannot do — inside boxes whose walls
are all code. Everything repeatable is code; everything the model produces is validated, human-gated
where it matters, and recorded.

## The funnel, measured

```
157 templates in the registry
 ├─ ftr : 23 ground  →  9 survive the gauntlet  →  9 shown
 └─ cib : 10 ground  →  (not measured)
```

"Ground" means every non-optional `Need` found a column whose `concept` matches. The 134 that do not
ground for `ftr` are overwhelmingly CORRECT silence — a payments export has no lending facilities
(`facility_id`, 17 templates), no insurance policies (`policy_id`, 9), no balances (`monetary_stock`,
20). **Adding templates is not the lever.** The blocked-concept histogram is the evidence:

| blocked concept | templates blocked (ftr) | verdict |
|---|---|---|
| `monetary_stock` | 20 | genuine absence — flows, not stocks |
| `facility_id` | 17 | genuine absence |
| `product_type` / `policy_id` | 9 each | genuine absence |
| `obligor_id` / `limit` / `fund` | 8 / 7 / 7 | genuine absence |
| `effective_date` | 6 | **SME call** — `ftr` has `value_date`, concept `value_date` |
| `counterparty_id` (cib) | 11 | **BUG — see task 1** |

## Task 1 — templates can require a concept the classifier can no longer produce

`Need.concept` is validated against `CONCEPT_REGISTRY`, which still contains retired legacy aliases.
The classifier vocabulary EXCLUDES them, and `_accept_concept` canonicalizes a fresh selection to the
successor. Matching is string equality (`col.concept == need.concept`). So:

```
counterparty_id  in CONCEPT_REGISTRY .......... True   (Need() validation passes)
                 offered to the classifier .... False  (retired alias)
                 a fresh selection becomes .... customer_id
```

`counterparty_id` appears as an unmet need on 11 `cib` templates, and no column can ever satisfy it.
The single column still carrying the value (`ftr.counter_party_cif_id`) is almost certainly one of
the 3 rows that stayed on a superseded vocabulary fingerprint; once re-derived, the last binding
disappears.

**MEASURED: fixing this recovers ZERO templates today.** Canonicalizing the need and re-grounding
gives `cib: 10 → 10`, `ftr: 23 → 23`. The "11 blocked" figure counts each template under EVERY unmet
concept, so every one of those 11 has other unmet needs as well. An earlier draft of this plan
claimed it "recovers 11 templates" — that was arithmetic misread as opportunity.

This task is therefore **hygiene, not recovery**: it closes a silent-death class (a template can
require something the classifier can no longer produce, and simply stops working with no signal) and
it will matter the moment a catalog arrives that WOULD satisfy the rest of those needs. Prioritise it
as correctness, not as feature gain.

Fix: canonicalize `Need.concept` at import, or validate it against the classifier vocabulary rather
than the registry. Test: *every `Need.concept` across all 157 templates is producible by the
classifier today* — which would have failed the moment `counterparty_id` was retired.

**Companion capability (from SME triage finding F1): ordered alternate concepts on `Need`** —
`Need(concept=("origination_date", "effective_date"))`, first match wins. `tenure_days`' author
wanted exactly this and could only write it as a comment; the engine's one-concept `Need` is why the
template binds KYC dates while two true origination columns sit unused. Also unblocks the triage's
R1 sister card and R6's `instrument_type` alternate. Same file, same validation seam — bundle with
this task.

## Task 2 — a tied binding is resolved alphabetically (MEASURED: half of all bindings)

**This is the largest correctness lever in the plan.** Measured over the grounded set on the
deployed catalogs:

```
ftr : 13 of 26 bindings AMBIGUOUS   (50%)
cib :  6 of 27 bindings AMBIGUOUS   (22%)
```

Nineteen of 53 bindings across both catalogs are decided by alphabetical order. Real examples, live
today:

```
dormancy_days.event_ts        chose tran_date          from [tran_date, tran_time]
txn_frequency_trend.event_ts  chose tran_date          from [tran_date, tran_time]
tenure_days.origination       chose cust_kyc_complete_dt
                              from [cust_kyc_complete_dt, cust_kyc_last_dt, cust_reactv_dt]
product_gap_whitespace.segment chose cust_priority_cd   from [cust_priority_cd, cust_seg_cd]
```

`tenure_days` measures how long a customer has been with the bank — and it is currently computed
from **KYC completion date**, picked alphabetically over two other candidates. That is a compliance
milestone, not the start of the relationship. The feature is not obviously broken; it is quietly
wrong, and nothing on the card says a coin was flipped.

The same three-candidate tie feeds `product_breadth`, `relationship_deepening_breadth` and
`tenure_upsell_readiness` — one arbitrary pick propagating into four features.

**SME CORRECTION (triage doc, finding F1): `tenure_days` is NOT a tie-break case.** All three tied
KYC dates are correctly classified `effective_date`; the template's need asks for `effective_date`
while the catalog holds two TRUE origination columns (`cust_acct_opn_dt`, `cust_reln_start_dt` →
`origination_date`) that no recipe uses. The right answer is not in the tied set, so no adjudicator
can pick it — only re-authoring the need fixes it (blocked on the ordered-alternates `Need`
capability; see the triage doc). Task 2's honest flagship examples are `dormancy_days`
(`tran_date` vs `tran_time`) and `product_gap_whitespace.segment` (`cust_priority_cd` vs
`cust_seg_cd`) — genuine ties where every candidate is plausibly right. And finding F2 there removes
~6 of `ftr`'s 13 ties by a single field correction (`tran_time` is a time-of-day component
mis-classified as an event clock), so Task 2's live adjudication set is smaller than 19: **correct
what should never tie; adjudicate only what genuinely does.**

Why they tie: `tran_date` and `tran_time` both match `event_timestamp` and score identically (`-4`
concept match; no as-of bonus — `event_timestamp.pit_role` is `event`, not `as_of`; no entity bonus —
it declares no `entity_link`; same table, so `prefer_table` cannot separate them). The tie-break is
`sort(key=(score, table, column, object_ref))`, so **`tran_date` won because "d" precedes "t"**.

The data model for the fix ALREADY EXISTS — grounding records it:

```
GroundedNeedBinding(role='event_ts', graph_object_ref='...tran_date',
  binding_resolution      = AMBIGUOUS,
  tied_candidate_logical_refs = ('...tran_date', '...tran_time'),
  tied_candidate_set_hash = '94d338224b79...')
```

Fix: rank tied candidates by their enrichment (definition / ai_summary / semantic_terms — all at
237/237 coverage) against the template's own English `intent`. Escalate to an LLM only for residual
ties, and key the decision on the `tied_candidate_set_hash` already computed, so the verdict is
CACHED and replay-stable. Grounding is deterministic today; a live model call in the binding path
would break sealed-plan replay, so the cached-evidence pattern (the concept critic's) is required,
not optional. Degrade to today's deterministic order on miss or timeout.

### Implementation notes

`_load_columns` does not currently select `definition`, `ai_summary` or `semantic_terms` — the
12-field `_Col` record has no room for them. They must be loaded for the tie-break to have anything
to reason over. Load them, but consult them ONLY among tied candidates, so a need with one candidate
or a clear score winner grounds byte-identically to today.

The tie-break belongs in `_ranked_matches` / `_match`, which is where the ordering is decided —
upstream of where `GroundedNeedBinding` records `binding_resolution=AMBIGUOUS`. Both must agree: the
recorded tie metadata should reflect the candidates that were actually tied, not a set the ordering
has already collapsed.

**How to test it so the test can fail.** Asserting `tran_date` wins on the real catalog proves
nothing — `tran_date` is ALSO the alphabetical answer, so the assertion passes over the bug. The
fixture must make spelling and meaning DISAGREE: name the semantically correct column so it sorts
LAST.

```
aaa_etl_batch_ts  "the ETL batch load timestamp — when the warehouse ingested this row"   sorts FIRST
zzz_txn_event_ts  "when the customer's transaction actually occurred"                     sorts LAST
```

Against `dormancy_days`' intent ("Days since the customer's last activity"), binding the batch stamp
would measure PIPELINE FRESHNESS rather than customer behaviour — a confidently wrong feature of
exactly the kind this platform exists to refuse. Three companion assertions: a guard that the fixture
really does make the alphabetical answer the wrong one (or the main test proves nothing), a
clear-winner case that must be untouched, and a no-intent case that must keep the historical order so
the change is additive rather than a silent re-binding of every existing pass.

### Production requirements (architecture review, 2026-08-10)

* **Governed seam or nothing.** The tie-break is a NEW LLM call type and must ride the same audited
  dispatch as every existing one: a registered prompt id/version and output schema
  (`overlay_tie_break` v1) in the schema registry, redacted input recorded in `llm_call`, cost
  metadata, and its own `CallLedger` budget entry so a pathological catalog cannot burn spend.
  Column definitions already egress under the enrichment's definition-grade classification; the
  template `intent` is authored text. Nothing crosses the egress boundary unclassified.
* **Closed output.** The model returns the REF of the winning candidate — validated ∈ the tied set,
  exactly as concept answers are validated ∈ the vocabulary — plus a bounded rationale string. An
  off-set answer falls to the deterministic order, never to a write.
* **Shadow first.** Phase A records verdicts WITHOUT changing bindings and reports disagreement with
  the alphabetical choice. The live tie population is 19 bindings, so the full shadow review is an
  afternoon of SME time, not a quarter. Phase B flips behaviour behind a flag with flag-off
  byte-identity — the platform's own pattern (`feature_context_enabled` precedent).

## Task 2b — a content-addressed verdict store, warmed at ingest (the enabler for Task 2)

**Owner decision, 2026-08-10: this deployment has a SINGLE read scope — every caller holds every
role.** That removes the one genuinely hard obstacle to caching grounding, and this task assumes it.
Recorded as an assumption, not a fact of the code: `_load_columns` still filters
`visible_requires <@ allowed_sensitivities(roles)` (templates.py:262), so if roles ever diverge, the
stored result must either be keyed by role set or post-filtered by dropping candidates that bind a
column the caller cannot see. Do not delete that filter on the strength of this note.

### The split

| stage | depends on | precomputed? |
|---|---|---|
| which recipes bind which columns | the CATALOG only | no — recomputed per request; it is cheap arithmetic (see redesign below) |
| tie-break VERDICT between equal candidates | the catalog + the recipe's intent | **yes — warmed at ingest, cached by content. This is the point.** |
| gauntlet (type / additivity / PIT / units / join) | the catalog + freshness | no; freshness has a TTL |
| `target_ref` leakage veto | the HYPOTHESIS | no — arrives with the request |
| near-label adjudication (Task 3) | the hypothesis's LABEL | no (its verdicts cache by content, but per-hypothesis) |

`dormancy_days` is sound for forecasting transaction volume and dangerous for predicting churn —
same recipe, same columns, opposite verdict. Anything question-dependent stays live.

### REDESIGNED in architecture review (2026-08-10): cache VERDICTS, never grounding

The first draft of this task stored grounded bindings per `(catalog_source, template_id)` and then
spent forty lines on an invalidation policy, because six writers mutate grounding inputs OUTSIDE
ingest (field corrections via `field_resolution.py`, fact confirms via `table_fact_projection.py`,
`type_attestation.py`, `semantics.py`, entity suggestions via `graph.py`, `axis_projection.py`).
Every option weighed — recompute-at-read, a writer-bumped version counter, over-invalidation on
every event — inherits the bug class that wedged the read model on 2026-08-09: **a keyed cache whose
writers must all remember to invalidate it.**

The review found the simpler design already implied by the plan's own facts:

* Grounding is arithmetic and the plan admits it is cheap → **recompute it per request. Never store
  it.** There is then no grounding cache to go stale.
* The only expensive, non-deterministic step is the tie-break VERDICT → **store only verdicts,
  content-addressed**, in the store built for exactly this: **`structured_result` (migration 1039)**,
  indexed on `(result_type, result_version, input_content_hash)`, read via `find_structured_result`,
  with `structured_result_provenance` linking each result to its `llm_call_ref` — the seam the
  concept critic replays through today, cross-run and re-validating code-side gates on every read.
  No new table. No migration. Nothing drawn from the 1059+ pool.
  **Implementation warning (verified in review):** do NOT build on `llm_call`'s own
  `input_hash` read (`intake/llm.py:644`) — that lookup is scoped `WHERE run_id AND task AND
  input_hash`, an in-run retry cache. A verdict warmed at ingest would be invisible to a later
  request through that path. `structured_result` is the cross-run store; the other one only looks
  like it.

The verdict key hashes EVERYTHING the answer depends on: the `tied_candidate_set_hash` grounding
already computes, the tied candidates' enrichment-text hashes, the template's `intent`, and the
prompt/schema version. **A stale entry is then impossible rather than detected**: correct a
definition, confirm a grain, retire a concept — the key changes, the old verdict is simply never
looked up again. Invalidation policy: none, by construction. This is the
`producer_configuration_hash` lesson applied at design time instead of as a post-incident fix.

### What "precompute at ingest" now means — warming, with the owner's goal intact

* The ingest TAIL (after `axis_projection`, own savepoint, fail-soft, `record_stage` — the standard
  pattern) grounds the registry once, finds the AMBIGUOUS bindings (13 on `ftr` today), and
  adjudicates them THERE — where the LLM budget, `CallLedger` and audited dispatch already operate.
  Ingest already costs ~60 minutes of LLM work; this adds at most one small call per tie.
* A human correction that changes a candidate set produces a cache MISS at the next request. The
  binding falls back to today's deterministic order (fail-open, behaviour-identical) and a
  re-adjudication job is enqueued on the durable runtime queue the worker — now actually deployed —
  already drains. The miss is transient and never user-facing as an error.
* **Requests never call the model.** The worst case for any request is exactly today's behaviour.

### Request-time after the change

1. Load columns (already happens), ground (arithmetic, recomputed — cheap by measurement).
2. Per AMBIGUOUS binding: look up the verdict by content key. HIT → bind it and cite the verdict's
   `llm_call_ref` on the grounding trace. MISS → deterministic order, an explicit
   `binding_rationale: unadjudicated` marker, enqueue for the worker.
3. Nothing else changes. The chosen ref + verdict reference enter the grounding context beside the
   `tied_candidate_set_hash` already recorded there, so a sealed plan replays to the identical
   binding from the identical stored verdict.

### The design rule, and the owner discussion that settled it (2026-08-10)

The owner probed this design from every side — recompute after every upload? a refresh button on
the UI? save the filled recipes the warming pass already computes? Each variant resolves under ONE
rule, recorded here because it settles every future version of the same question:

**Cache a result only when CHECKING the cached copy is much cheaper than REMAKING it.**

* The tie-break judgements: remaking = an LLM call; checking = a fingerprint comparison. Millions of
  times cheaper → **store them.**
* The grounding (which recipes have their columns): remaking = an in-memory glance; checking a
  saved copy = the same glance, because trusting it requires verifying no column changed since. The
  check costs what the work costs → **storing buys zero**, and the only way a saved copy ever
  "pays" is by skipping the check — which is how it becomes Saturday's bug (a stored summary,
  silently wrong after a write path that forgot to refresh it). The warming pass therefore fills
  recipes transiently to FIND the ties, then discards the filling on purpose.
* Refresh-after-every-upload is insufficient by timeline, not by principle: these catalogs go days
  to weeks between uploads (`ftr`: Jul 31 → Aug 9) while six write paths edit grounding inputs from
  the UI in between. A copy refreshed only by uploads lies for the whole gap.
* A correctness-bearing refresh button converts the computer's free glance into a HUMAN job split
  across two people who cannot see each other (the curator who corrected a label, the data
  scientist who asks the next hypothesis). "Press refresh just in case before every use" IS the
  per-request check, performed by the forgetful party.

**Adopted from the discussion — the button aimed at the right layer:** a curator-facing
**"Re-adjudicate recipes now"** action on the UI. After a batch of field corrections it re-runs the
warming pass immediately instead of waiting for lazy re-adjudication by the worker. Strictly an
optimisation: press it and the next request gets fresh judgements instantly; forget it and nothing
is ever wrong — one deterministic-fallback binding, then the worker catches up. A button the system
stays correct WITHOUT is the only kind this platform ships.

**The stopwatch guard:** the whole design leans on one measured fact — the glance is ~free at 237
columns. Step-0 instrumentation therefore records grounding time per request. If a future catalog
makes the glance genuinely expensive, that arrives as a NUMBER and the design is revisited with
evidence — the same metric-triggered posture as the intake search-loop upgrade. No
conviction-driven rebuilds in either direction; every unmeasured conviction this week measured
wrong.

## Task 3 — the near-label leakage control was specified and never built

23 templates carry `near_label=True`. `dormancy_days` says:

> ⚠ NEAR-LABEL: if churn is defined as "no activity in N days" this ≈ the label. **The 3-part leakage
> control must FLAG it** (confirm pre-as_of only, and window ≠ label window).

`near_label` appears in exactly ONE place outside the registry: `suggestion_contract.py:872`, where it
renders a warning chip. **Nothing enforces it.** The `target_ref` veto only rejects a candidate that
BINDS the target column; it cannot compare a 90-day inactivity feature against a 90-day inactivity
label.

Fix: a critic pass over surviving candidates that compares the hypothesis's label definition to the
feature's window. ADVISORY ONLY — it may flag, refuse or downrank, never clear. In this platform an
LLM output must never clear a design check (`_governed_read` and the gauntlet own clearance).

### Production requirements (architecture review, 2026-08-10)

* **The label definition is UNSTRUCTURED.** The intake captures hypothesis and prediction goal as
  free text; nothing carries "churn = 90 days of inactivity" as data. The critic must INFER the
  label window from prose and **ABSTAIN when it cannot** — an abstention shows today's warning chip,
  never a refusal. Abstention-as-designed-answer is the `shape_conflicts` discipline, applied here.
  (A structured label-definition field at intake is the better long-term fix; note it, do not block
  on it.)
* **Closed verdict vocabulary:** `{no_finding | too_close | abstain}` — deliberately no token that
  reads as "cleared", because this critic must be incapable of clearing anything. Only `too_close`
  has an effect, plus a bounded rationale for the card.
* **Bounded cost.** Runs only on candidates that GROUNDED and carry `near_label=True` (≤23 by
  registry; the grounded intersection today is far smaller). Cached by (template_id,
  label-definition hash) through the same content-addressed seam as Task 2b, with its own ledger
  budget. Hypothesis text reaches it only through `redact_free_text` — the redaction the generation
  prompt already applies.
* **Flag-only first.** Verdicts appear on cards and in the considered set before any refusal mode
  exists. Turning `too_close` into a hard refusal is a later, explicit product decision.
* **ORIGIN-BLIND (owner decision, 2026-08-10).** The critic runs on EVERY surviving candidate —
  template-grounded and LLM-proposed alike. The gap this closes: the check as first spec'd was
  triggered by the `near_label` flag, which only template cards carry — so the LLM path could invent
  "days since the customer's last transaction" for a 90-day-inactivity churn label and sail past the
  exact check that flags the identical template feature. Same feature, same leakage, flagged on one
  and not the other purely by origin. Rule: safety checks are origin-blind, exactly as the gauntlet
  already is. The template flag survives as a cheap "always taste this one" marker; for LLM-origin
  candidates the critic reads the candidate's own description and formula. Cost stays bounded by the
  surviving-candidate count, cached per (candidate content, label-definition hash).

## Task 4 — rank by the taxonomy that already exists; do not filter

All 157 templates carry `use_cases` (107 distinct), `family`, `stage`. Pre-filtering cannot help:
only 23 ground for `ftr`, so a top-K can only REMOVE from 23 — never add. Grounding all 157 is cheap
(in-memory over a column list loaded once; the only DB touch is `effective_entity`, gated on
`is_grain`, true for 3 of 237 columns).

Fix: map the hypothesis onto the closed `use_cases` vocabulary with the LLM — same pattern as the
concept classifier (closed vocabulary, structured output, cached by `input_hash`, replay-stable via
the vocabulary fingerprint), INCLUDING an abstain option so an unmapped hypothesis degrades to
unranked rather than to a confidently wrong order. Then order the grounded set by set intersection —
no model in the ranking step. Explicitly NOT vectors: there is no embeddings provider wired
(Anthropic has none), no `pgvector` in the deployed Postgres 15.18, no numpy, and an embedding index
is a versioned artifact that must be hashed into the sealed-plan identity or replay breaks.

### THE RULE: the hypothesis may remove a recipe only for being UNSAFE, never for being irrelevant

This is the design decision the whole task rests on. After the plan, the hypothesis acts at three
points, and only two of them remove anything:

| step | driven by | effect |
|---|---|---|
| 1. grounding (precomputed, Task 2b) | the CATALOG | 157 → 23. The hypothesis plays no part. |
| 2. use-case match (Task 4) | the hypothesis | **ORDERS the 23. Removes nothing.** |
| 3. `target_ref` veto (existing) | the hypothesis's TARGET | **REMOVES** anything binding the label column |
| 4. near-label critic (Task 3) | the hypothesis's LABEL DEFINITION | **REMOVES or FLAGS** a feature whose window ≈ the label |
| 5. gauntlet (existing) | catalog + freshness | **REMOVES** on type / additivity / PIT / units / join |

Relevance is expressed as ORDER; safety is expressed as REMOVAL. Never the reverse.

Why: a recipe dropped for low relevance is indistinguishable, on screen, from one that never ground —
both are simply absent, with no reason given. Since only 23 ground for `ftr` and 10 for `cib`, a
relevance filter can only shrink an already-small set, and its mistakes are invisible. A ranking's
mistakes are merely annoying: the right feature is at position 7 instead of 1, and still there.

A safety removal is different in kind — it MUST remove, and it must say why (the reject code), because
showing a feature that leaks the label is worse than showing nothing.

### Production notes (architecture review, 2026-08-10)

* **Surface scoping:** the ranking runs in the HYPOTHESIS flow (`build_considered_set`) only. The
  per-table suggestions page has no hypothesis and its stable order is deliberate — untouched.
* **Map onto `use_cases` ∪ `family`,** not `use_cases` alone: 107 use-case labels average ~1.5
  templates each, so a use-case-only match is brittle; `family` generalises when the finer match
  abstains. Abstain → unranked, today's order.
* **Pin the vocabulary with a registry test.** `use_cases` values are free-authored strings with no
  validation — a typo in a new card silently mints a 108th use-case, and since the vocabulary is
  part of the mapping's cache identity, drift both fragments the taxonomy and churns the cache. One
  test asserting the closed set (updated deliberately when a new use-case is genuinely added) makes
  the vocabulary a contract instead of an accident.
* **Same seam discipline as Task 2:** registered prompt + schema (`overlay_use_case_map` v1),
  redacted hypothesis (`redact_free_text`), content-addressed caching — one call per NEW hypothesis,
  cached thereafter, so steady-state ranking adds zero LLM latency.
* **Rollout: log-and-compare.** Emit the proposed order beside the served order for a period; the
  cost of being wrong is ordering, so the bar is lower than Task 2's — but the comparison is nearly
  free and catches a bad mapping before users see it.

## Task 4b — the hypothesis must choose PARAMETERS (the "it looks hardcoded" defect)

**Owner observation, 2026-08-10: "through recipes I get the same fixed repeated features out of 23
for hundreds of hypotheses — it looks hardcoded."** Correct, and measurable.

```
23 grounded recipes on ftr  ->  147 distinct parameterisations available
features actually produced   ->  23
```

`_bind_params` resolves every parameter to "the first allowed value", always. So the recipes offer
147 legitimate features and the platform emits the same 23 for every question ever asked.

```
dormant_reactivation    window=3 x dormancy_days=3   ->  9 variants, 1 used
fan_in_fan_out          window=3 x measure=3         ->  9 variants, 1 used
amount_just_under_limit window_min=3 x band_pct=3    ->  9 variants, 1 used
inflow_outflow_ratio    window=4 x measure=2         ->  8 variants, 1 used
```

**Parameters are exactly where a hypothesis SHOULD differentiate.** "Customers churn when activity
drops off" and "detect structuring within a reporting period" want different windows from the same
recipe. A 30-day reactivation window and a 180-day one are different features answering different
questions — the registry already says both are valid, and the platform silently picks the first.

This is the most direct answer to the repetition complaint, and it is cheaper than every other task
here: no new concepts, no new templates, no coverage work. The parameter space is authored, bounded
and validated (`_bind_params` rejects a value outside the allowed tuple), so a hypothesis-driven
choice cannot invent an unsafe one.

Two sources of variety are being discarded, not one:

1. **Parameters** — 23 of 147, above.
2. **Alternative bindings** — grounding yields AT MOST ONE candidate per template, yet 13 of 26 `ftr`
   bindings are ties (Task 2). Each tied candidate is a legitimate second feature, not just a
   discarded runner-up: "dormancy measured on transaction date" and "on value date" are different
   features, and today the second is unreachable.

Scope carefully: 147 features per catalog is noise, not richness. The point is not to emit all of
them — it is to emit the ones the HYPOTHESIS implies, so two different questions produce two
different answers instead of the same 23.

**Emission policy (decided in product review; revisit with evidence, not by default):** still ONE
card per recipe — but its parameters are the hypothesis-chosen ones instead of first-in-list, with
the untaken alternatives named on the card ("also available: 30 / 180-day windows"). Card identity
must include the parameterisation — `semantic_parameter_binding_hash` exists for exactly this — so
the same recipe under two hypotheses is two identities, never a mutation of one card. Expanding to
top-K parameterisations per recipe is GATED on the gauntlet-cost measurement in Open Questions:
emitting 147 candidates instead of 23 means ~6× the `_validate_idea` runs, and that function does
per-candidate DB reads. Measure before widening.

**Mechanics:** the parameter choice is a closed selection — values must be ∈ the authored tuple,
which `_bind_params` already enforces — made through the same governed seam, cached per (template,
hypothesis hash). A hypothesis that implies nothing abstains to the current default, so flag-off and
abstain are byte-identical to today.

## Task 4c — write recipes against the concepts you ALREADY have (cheapest lever in the plan)

**35 concepts in `ftr` and 36 in `cib` are carried by real, enriched columns that NO recipe asks
for.** These need no data acquisition, no flag, no gate, no cross-catalog work: a card authored
against them grounds on the next pass.

```
ftr (35 unused, columns in brackets)
  party_name(11) payment_narrative(7) bank_bic(5) transaction_type(4) clearing_member_code(4)
  transaction_id(4) currency_code(3) code_label(3) statement_visibility_flag(2) channel_reference(2)
  postal_address(2) phone_number(2) record_author(2) system_time(2) internal_transaction_serial(2)
  counterparty_name external_account_ref kyc_document end_to_end_reference source_system
  account_name initiating_party instrument_type merchant_name module_id booking_date branch_name
  branch_id swift_uetr ultimate_creditor ultimate_debtor row_hash virtual_account_id value_date
  reporting_period

cib (36 unused)
  code_label(7) source_system_status(6) industry_code(6) restriction_reason(4) legal_entity_type(4)
  customer_relationship_status(4) kyc_document(4) system_time(3) postal_address(3)
  restriction_status(3) residency_status(3) boolean_flag(3) kyc_narrative(3) record_author(2)
  origination_date(2) fx_conversion_rate(2) new_to_bank_flag(2) party_name(2)
  customer_risk_rating(2) branch_id(2) branch_name(2) relationship_manager_name(2) consent_status
  protected_attribute customer_group_id fatca_crs_classification nominee_indicator npe_flag
  phone_number email_address rating staff_indicator bank_bic record_deleted_flag lei source_system
```

Obvious feature-bearing clusters an SME could author against today:

* **`payment_narrative` (7 cols)** — free-text payment purpose. Salary detection, rent/loan
  repayment regularity, structuring narratives, purpose-code drift.
* **`bank_bic` + `clearing_member_code` (9 cols)** — correspondent-network concentration, unusual
  routing depth, sanctioned-corridor exposure, intermediary-count anomalies.
* **`transaction_type` (4)** — the transaction MIX that drives most retail behaviour features.
* **`restriction_status` / `restriction_reason` / `customer_relationship_status` (11 cols, cib)** —
  the whole suspension / negation / blacklist lifecycle, with nothing built on it.
* **`industry_code` (6)** — sector concentration and industry-relative benchmarking.
* **`new_to_bank_flag`, `customer_risk_rating`, `npe_flag`, `fatca_crs_classification`** — the
  compliance and onboarding axes.

Some of the 71 are correctly unused (`row_hash`, `record_deleted_flag`, `module_id`, `system_time`
are plumbing, not signal). Triage before authoring — but the list is where an SME's hour buys the
most.

CONSTRAINT: SME-authored only. Each card must carry its PIT declaration, additivity, leakage flag and
eligibility note exactly as the existing 157 do. That is the safe-by-construction property; an
LLM-invented recipe has none of it. See "What NOT to do".

**TRIAGE COMPLETE — see `2026-08-10-recipe-triage-4c.md`.** All 71 concepts dispositioned:
**12 cards drafted** (Tier 1: correspondent concentration, currency diversity, back-valuation lag,
party-chain depth, narrative completeness, type-mix shift, STP reference quality, system-footprint
breadth, restriction lifecycle, KYC freshness, staff flag, industry risk), 12 deferred with named
blockers (Tier 2), the rest correctly idle (Tier 3 — including the tempting-but-wrong 11
`party_name` columns, which are entity-resolution material, not aggregation features). The triage
also produced three findings that feed OTHER tasks: `tenure_days` is mis-authored (F1 → corrects
Task 2's flagship example), `tran_time` is misclassified and causes ~6 of the 13 `ftr` ties (F2 →
shrinks Task 2's scope), and `cib`'s six `source_system_status` flags are a product-holding proxy
(F3 → the biggest single recipe win for the customer catalog). Estimated yield: ftr 23 → ~30,
cib 10 → ~15, to be confirmed by Step-0 instrumentation. Cards are drafted, not approved: each needs
a second banking reviewer — the four-eyes rule applies to recipe authoring too.

## Task 5b — which SOURCE SYSTEM to onboard next (measured)

Simulated by grounding the registry against realistic column sets for two export types:

```
today                    ftr=23   cib=10
+ an accounts/deposits export   standalone  4   ·  combined with ftr  29
+ a lending/facility export     standalone  9   ·  combined with cib  21
all four catalogs, cross-catalog ON                              49
```

**Two findings that change the earlier sizing.**

A new catalog on its own buys little — a deposits export grounds only 4 recipes alone, because
balance recipes also need the customer and time anchors that live in other catalogs.

**And Task 0's value GROWS with catalog count.** Measured at +2 against today's two catalogs (see
Task 0), it is the difference between roughly 40 and **49** with four. The earlier "+2, marginal"
sizing is true ONLY for the pair you have now — it is not a general verdict on cross-catalog
grounding, and the plan should not be read that way.

Recommended order: **lending/facility first** (9 standalone, the strongest single source), accounts
second — and pair the data work with Task 0, or most of the lift stays locked behind the catalog
boundary.

## Task 5 — coverage is the real lever for MORE features

```
concept / definition / ai_summary / semantic_terms / domain ...... 237/237
bian_path ......................................................... 224
party_role / process_path / fibo_path / sub_domain ..... 143 / 126 / 113 / 112
entity ............................................................. 36  (15%)
additivity ......................................................... 12  ( 5%)
unit ................................................................ 0   (12 proposals unresolved)
currency ............................................................ 0   ( 6 proposals unresolved)
unclassified columns ................................................ 8
```

The metadata we have most of is what the router uses least; the metadata it depends on we have least
of. `_candidate_score`'s entity branch additionally requires a GOVERNED entity, so the effective
coverage is below 36/237. Raising `entity` and resolving the stranded unit/currency proposals does
more for recipe quality than any prompt or selection change.

**Mechanisms already exist for every gap — this is review time, not new design:**

* `entity` — the entity-suggestion flow already writes `graph_node.entity` from confirmed
  suggestions (`graph.py` updates from `entity_suggestion`), and the governed `entity_assignment`
  path is the authority tier. The 36 today are registry projections from identifier concepts; the
  lift is running the existing suggest→confirm loop over the high-value non-identifier columns.
* `unit` / `currency` — 12 + 6 LLM proposals sit ACTIVE and unconfirmed in `field_evidence`. The
  E4a confirm loop exists. They need a reviewer's hour, not code.
* the 8 `unclassified` columns — honest abstentions; two are literal dummy fields. The remainder are
  registry-gap candidates (`intermediary_agent_3`, `previous_instructing_agent_*` want a
  payments-agent concept that does not exist yet).

## Task 6 — smaller, verified items

* `FEATUREGEN_DATASET_PROFILES=0` withholds the table profile from `analysis/retrieval.py`'s query
  expansion. Every feature-generation payload carries `missing_context: ['catalog_profile_absent',
  'dataset_profile_absent', ...]` — we generate the profile and then tell the model it is absent.
* `_column_tokens` omits `definition`, while search weights it at 'B'. The code comment claims the two
  surfaces agree; they do not.
* `cib`'s table node has NULL `definition` and NULL `business_context` despite `OVERLAY_TABLE_SYNTH=1`
  and a re-ingest — table-level prose is not produced for technical (non-glossary) catalogs.
* 3 `ftr` concept rows remain on superseded vocabulary fingerprints (likely the enrichment loop's
  skip paths for columns with no glossary record).
* Instrument the funnel (registry → grounded → gauntlet survivors, with reject codes). The drop from
  23 to 9 on `ftr` is currently invisible: the v2 payload reports `omitted_counts: {}`.
  **Promoted to Step 0 in Sequencing** — every other task's success is judged on these numbers.

## Task 7 — record which recommendations humans KEEP, and feed it back (product review, 2026-08-10)

The plan optimises what is SHOWN and never asks what happens next — the product review's largest
finding. Today no signal records whether a recommended feature was any good: the per-table page is
deliberately read-only (P4: no accept verb, correctly — it is a curator inventory), and while the
contract flow snapshots the considered set and the human's Gate-1 selection, nothing joins the
selections back to `template_id` / parameterisation as a QUALITY signal.

The strongest long-term ranking input is *"what did humans keep, for hypotheses that mapped to the
same use_cases?"* — and the data to answer it is ALREADY RECORDED (grounded ideas carry their
template ids into the considered-set snapshot; Gate-1 confirmation is durable). This is a join and a
report, not new capture.

Scope it honestly:

* **Phase 1 (now, size S): telemetry only.** Selection rate by template / use-case / parameterisation,
  queryable. This also becomes the plan's true north-star metric the moment it exists.
  **Record ORIGIN on every selection (owner decision, 2026-08-10)** — template-grounded vs
  LLM-proposed (the ideas already carry `origin`; this is a group-by, not new capture). The platform
  runs TWO proposer engines into one gauntlet, and nobody can currently answer which one humans
  actually keep, per question type. This tag turns the two-engine question from a permanent debate
  into a measured answer — and it is the evidence base for every future "invest in templates vs
  invest in the LLM path" decision.
* **Phase 2 (explicitly deferred): ranking prior.** Feeding selection history into Task 4's order is
  a decision to take when there is volume — tens of contracts, not three. Premature personalisation
  on three data points would just add noise with a feedback loop attached.

## Task 0 — cross-catalog grounding (MEASURED: worth 2 templates, not a transformation)

**Measured before writing this section, and it refuted the assumption behind it.** Grounding the
whole registry against the UNION of both catalogs' columns:

```
ftr alone            : 23
cib alone            : 10
union (today)        : 31          (dormancy_days + txn_frequency_trend ground in both)
BRIDGED (both cats)  : 33
NEW, only if bridged :  2          clv_revenue_trajectory, next_best_product_propensity
```

The intuition — "the highest-value retail features are cross-catalog, so the boundary is the real
ceiling" — is **wrong for THIS pair of catalogs**. The blocked concepts are absent from BOTH:
neither a payments export nor a customer master holds `monetary_stock` (20 templates),
`facility_id` (17), `policy_id` (9), `obligor_id` (8), `fund` or `limit` (7 each). Bridging two
catalogs that both lack balances, facilities and products unlocks almost nothing.

**The real ceiling is catalogue coverage, not the catalogue boundary** — for THIS pair of catalogs.

**IMPORTANT QUALIFIER (measured later, Task 5b): +2 is not a general verdict.** Task 0's value scales
with catalog count, because every added catalog multiplies the pairs a recipe can span. With four
catalogs (ftr, cib, accounts, lending) cross-catalog grounding is the difference between roughly 40
and **49**. So the honest sizing is: marginal today, substantial as soon as a third and fourth source
land — which makes it a natural companion to the data work, not a replacement for it.

Note also that adding a deposits export does NOT unlock the 20 `monetary_stock` templates: measured,
it grounds 4 standalone and 29 combined with `ftr`. The 20 figure counts every template with
`monetary_stock` among its unmet needs, and most of those need lending or insurance concepts too —
the same arithmetic-misread-as-opportunity that Task 1 records.

Everything below remains factually true and still matters for correctness — a cross-catalog feature
that IS built must be built safely — but it is no longer the headline.

**The router grounds ONE catalog at a time.** Every entry point takes a single `catalog_source`:

```
_load_columns(conn, catalog_source, roles)          # one catalog
ground_all(..., catalog_source=src)                 # one catalog
clearing_neighbourhood(conn, catalog_source, ...)   # widens to sibling TABLES, SAME catalog
```

So a recipe needing `ftr.txn_amt` and `cib.cust_reln_start_dt` never grounds. `also_tables` buys
cross-TABLE within one catalog ("average transaction amount per customer, key over a verified join"),
never cross-CATALOG.

**The consequence that matters most: the failure is SILENT and indistinguishable.** A recipe blocked
because its concepts are genuinely absent (`facility_id` in a payments export) and a recipe blocked
because its ingredients live in two catalogs both vanish the same way — no reject code, no
diagnostic, nothing in `omitted_counts`. There is no way to ask "what would ground if the bridge were
usable?"

### What exists, and its state

| piece | state |
|---|---|
| `Template.source_entity` (the cross-catalog anchor) | **"RESERVED for Phase 3B.3 — neither set nor read yet"** |
| multi-source assembly | shadow only; `multisource_assembly_shadow_candidate` = **0 rows**, has never produced a candidate |
| live cross-catalog grounding | gated by `is_live_cross_catalog_enabled`: flag ∧ configured deployment id ∧ non-superseded APPROVE ∧ PASS evaluation ∧ matching version vector. Fail-closed. |
| the VERIFIED bridge `cib.cust_num ↔ ftr.cif_id` | real, and consumed by `planner/plan.py`, `planner/multisource_reuse.py`, `planner/fingerprint.py` — **the PLANNER, not the router** |

Confirming the bridge made the link discoverable and traversable for planning. It did NOT make the
template router able to build a recipe across it.

### The blocker nobody can design around: no cardinality

```
entity_bridge_edge columns : fact_key, entity_id, left_*, right_*, confirmed_event_id, status, projected_at
                             ^ no cardinality
crosswalk_observation_revision : 0 rows
```

The projected bridge carries **no cardinality**, and none is observed. This is not an oversight —
the platform holds metadata only (`CanonicalRow` has no samples; its `cardinality` is a DECLARED
string), and the profiler was retired in the upload-catalog pivot. So fan-out **cannot be measured
here**.

That is load-bearing for correctness: if one `cust_num` maps to several `cif_id` values, a
`sum(txn_amt) per customer` over the bridge double-counts, and nothing in the current data model can
detect it. Any cross-catalog aggregation must therefore either (a) require a DECLARED and
human-confirmed cardinality on the bridge, or (b) restrict to aggregations that are safe under
fan-out. Shipping cross-catalog features without settling this trades a silent wrong number for
coverage — the worst trade this platform can make.

### What the work actually is

1. A bridge-authorised multi-catalog column universe for grounding (never a widened read scope —
   every column still passes `_load_columns`' `visible_requires` filter in its own catalog).
2. `GroundedNeedBinding` to record the catalog of each binding AND the bridge `fact_key` traversed,
   so the plan identity and the audit trail name the link. `fingerprint.py` already folds bridge
   fact_keys into plan identity — the precedent exists.
3. The gauntlet's join-authority check to require a VERIFIED bridge, and to refuse an aggregation
   whose safety depends on a cardinality nobody has established.
4. `Template.source_entity` / `source_entity_need_role` actually set and read (Phase 3B.3).
5. The live-activation gate deliberately approved for this deployment — a governance decision, not
   an engineering one.

### The argument that did NOT survive measurement

This section originally read "why it dwarfs the rest", on the reasoning that two catalogs describe
the same customers, a VERIFIED bridge joins them, and the highest-value retail features —
transaction behaviour segmented by tenure, KYC risk or product holding — are exactly the ones
neither catalog can express alone.

That reasoning is sound and the conclusion is still wrong: measured, it is +2 templates
(`clv_revenue_trajectory`, `next_best_product_propensity`). The features it describes need PRODUCT
and BALANCE data, and neither catalog holds any — so the bridge is not what is missing. Recorded
here rather than deleted, because the same intuition will recur the next time a bridge is confirmed,
and the counter-measurement is one query.

## Sequencing

AGREED ORDER (owner-endorsed 2026-08-10; Step 0 and acceptance criteria added in review):

0. **Funnel instrumentation** (pulled forward out of Task 6). Registry → grounded → gauntlet
   survivors, per-recipe reject codes, on both surfaces. The plan's own Open Questions said this
   must land before anything judged on survivor counts; the order now agrees. Also the cheapest
   item here, and it retires the embarrassment that 14 of `ftr`'s 23 grounded recipes vanish with
   no recorded reason.
1. **Task 2b — the content-addressed verdict store + ingest warming.** No new tables; reuses the
   critic's replay seam. What makes Task 2 affordable and replay-stable.
2. **Task 2 — tie-break on meaning (shadow → flag).** 19 of 53 live bindings are alphabetical coin
   flips. The honest flagship cases are `dormancy_days` (`tran_date` vs `tran_time`) and
   `product_gap_whitespace.segment` (`cust_priority_cd` vs `cust_seg_cd`) — the SME triage showed
   `tenure_days` is an AUTHORING defect (F1, right answer not in the tied set) and that
   reclassifying `tran_time` (F2) removes ~6 ties at the source, so the genuine adjudication
   population is markedly smaller than 19. Shadow verdicts are ordinary `structured_result` +
   `llm_call` rows; the disagreement report is a query over them, not a new store.
3. **Task 3 — near-label critic (flag-only).** Closes a control specified and never built, on 23
   templates.
4. **Task 4 — use-case ranking (log-and-compare → on).** Relevance, no new infrastructure. NOT
   vectors.
5. **Task 4b — hypothesis-chosen parameters.** The direct answer to "the same 23 features for every
   hypothesis": 147 authored parameterisations, 23 emitted, always the first-in-list.
6. **Task 4c — SME recipes against the 71 unused concepts** (parallel; SME-bound, not
   engineering-bound). **Task 7 phase 1** (acceptance telemetry) alongside — it is a query, and it
   creates the north-star metric everything else is judged by.

Task 1 (the alias hygiene) rides with whichever change first touches `templates.py`. It recovers
ZERO templates today (measured); it prevents silent template death tomorrow.

Task 5 / 5b (coverage and source onboarding) is continuous, not a milestone: it is the actual
ceiling on how many recipes can ever ground. The remaining Task 6 items fold into whichever change
touches their file.

ACCEPTANCE CRITERIA — a step is DONE when:

| step | done when |
|---|---|
| 0 | reject codes visible per recipe on both surfaces; `cib`'s 23→? survival finally measured |
| 1 | every GENUINE tie remaining after the F1/F2 corrections adjudicated at ingest-warm time; request path is cache-hit only; a re-run of the same catalog reuses every verdict |
| 2 | shadow disagreement report reviewed by an SME; flag on; the disagreement-fixture test proves meaning beats spelling; replay test green |
| 3 | every grounded near-label candidate carries `no_finding` / `too_close` / `abstain`; zero refusals in flag-only mode |
| 4 | two different hypotheses produce visibly different orders; an unmappable hypothesis provably falls back to today's order |
| 5 | the same recipe under two hypotheses emits two parameterisations with distinct identities; gauntlet cost measured BEFORE any top-K widening |
| 4c | first five SME cards ground and survive the gauntlet on the live catalogs |
| 7 | selection rate by template / use-case is a query anyone can run |

FOUR INVARIANTS TO ENCODE, not merely observe:

* **The Strategist may only remove.** Any selected template subset ⊆ `ALL_TEMPLATES`. Never widening
  — the constraint `_template_candidates` already documents.
* **The Critic may only tighten.** Flag, refuse, downrank — never clear. In this platform an LLM
  output may never clear a design check; `_governed_read` and the gauntlet own clearance, and that
  rule is what stops a plausible score from laundering an unverified fact.
* **Grounding stays deterministic.** A live model call in the binding path makes the same catalog
  yield different features run to run, which breaks sealed-plan replay and makes "why did this
  feature bind `tran_date`?" unanswerable. Every model decision is recorded as content-addressed
  evidence keyed on the tied-candidate hash, so a re-run reuses it.
* **Every new LLM call type rides the governed seam.** Registered prompt id + output schema,
  redacted input in `llm_call`, cost metadata, a ledger budget, content-addressed replay. This plan
  adds three call types (tie-break, use-case map, near-label critic); zero of them may bypass the
  seam that every existing call goes through. An unregistered prompt is a review-blocking defect.

**Task 0 sits outside this order.** With TODAY's two catalogs it is worth +2 templates, needs a
governance decision (the live-activation gate) and is blocked on the cardinality question — so it is
not the next thing to build. But its value SCALES with catalog count (Task 5b: roughly 40 → 49 with
four catalogs), so it should be sequenced ALONGSIDE data acquisition rather than dismissed. Deciding
it late is fine; deciding it never is not.

**Data acquisition is a parallel track, not a router task.** Measured (Task 5b): a lending export
grounds 9 recipes standalone and an accounts export 4 — worthwhile, but NOT "more than every task in
this plan combined", which an earlier draft claimed before the simulation was run. Most of the lift
from a new catalog needs Task 0 to be realised (accounts alone 4, accounts + ftr 29).

Keep the diagnostic regardless: report, per failing recipe, WHICH needs went unmet. That is what
turned "134 blocked" from a mystery into the histogram above, and it is what would have shown the
+2 answer without writing any cross-catalog code.

## How we will know any of this worked

The plan has one number: **how many of the 157 recipes ground, per catalog** (`ftr` 23, `cib` 10
today), and **how many survive the gauntlet** (`ftr` 9; `cib` not yet measured). Record both before
and after every task. Secondary measures, each already measurable with the queries used to write this
plan:

* ambiguous bindings as a share of total (`ftr` 13/26, `cib` 6/27) — Task 2 should drive this toward
  zero UNADJUDICATED, not toward zero ambiguous: the ties remain, they just stop being coin flips.
* distinct parameterisations emitted vs available (23 of 147) — Task 4b.
* concepts present but unused by any recipe (35 `ftr`, 36 `cib`) — Task 4c.
* near-label candidates flagged vs silently shown (0 of 23 templates today) — Task 3.

Product metrics (the review's addition — the engineering numbers above say the machine works; these
say the product does):

* **distinct feature sets across distinct hypotheses** — today a constant 23; the repetition
  complaint, made measurable. Tasks 4 + 4b move it.
* **share of AMBIGUOUS bindings carrying a recorded rationale** — today 0%. Tasks 2b + 2 move it.
* **Gate-1 selection rate of template-origin candidates, by use-case** — unmeasurable until Task 7
  phase 1, which is why Task 7 ships early. This is the north star: are the recommendations any
  good, as judged by the only judge that counts.

## Open questions this plan does NOT answer

* **`effective_date` blocks 6 `ftr` templates and `ftr` has `value_date`.** SME verdict (triage
  R3): do NOT alias — `value_date` has its own semantics and its own recipe home
  (`back_valuation_lag`, drafted). Whether any of the 6 blocked templates should ALSO accept
  `value_date` via ordered alternates remains per-template SME review, but the default is no.
* **Does the gauntlet scale to Task 4b?** Emitting more parameterisations means gauntlet-checking
  more candidates (potentially 147 rather than 23, ~6x), and `_validate_idea` does per-candidate DB
  reads. Measure before widening, or Task 4b trades repetition for latency.
* **`cib`'s gauntlet survival rate is unmeasured** — `ftr` drops 23 → 9 and nobody knows why 14 were
  rejected, because the v2 payload reports `omitted_counts: {}`. Task 6's instrumentation should land
  before any task whose success is judged on survivor counts.
* **Sizes are rough.** The S/M sizes in the map and sequencing are for ordering, not commitment; the
  first task to overrun them should trigger a re-look at the order, not a march through it.
* **Task 3's quality is unevaluated by design.** Flag-only mode exists precisely because nobody yet
  knows the critic's false-positive rate on real hypotheses. The flag-only period IS the evaluation;
  define the acceptable rate before deciding on any refusal mode.

## External architecture review — adjudicated against verified platform facts (2026-08-10)

Six proposals received; each checked before adoption. Two rest on false premises and are rejected
with evidence; the rest are adopted, two of them redesigned.

| # | proposal | verdict |
|---|---|---|
| 2 | structured target extraction at intake | **ADOPT — spec FIRST** |
| 4 | gauntlet-refusal feedback to the router | **ADOPT, redesigned deterministic** |
| 1 | two-phase / batched gauntlet for 4b | **ADOPT as 4b's mitigation design, behind the existing measurement gate** |
| 6 | composite keys on `Need` | **PARK — no measured demand; compound keys already live where joins execute** |
| 3 | HLL cardinality sketches at ingest | **REJECT as specified — no data exists at ingest** |
| 5 | blanket entity inheritance from table grain | **REJECT as specified — semantically wrong, and its premise is false** |

**#2 (adopt first).** Verified: `target_ref: str | None = None` (`contract.py:170`) — the target is
OPTIONAL at intake, so the `target_ref` veto and the near-label critic are both gated on an input
the user may simply not provide, and the reviewer's abstain-cascade concern is real: vague prose →
extraction failure → ABSTAIN → advisory chip only. Spec: an intake pre-pass extracting a strict
contract (`target_column`, `target_window_days`, `target_type`, domain) through the governed seam,
cached, with abstention REPORTED as a coverage metric. One product boundary the reviewer's version
crosses: hard fail-closed on low-confidence extraction would refuse the whole generation flow for a
merely informal hypothesis. Instead: extraction failure + a grounded NEAR-LABEL candidate in the
result = that candidate is withheld pending an explicit target declaration — fail-closed exactly
where the risk is, exploratory generation unblocked elsewhere. Abstention-rate monitoring (the
reviewer's Task-3-dead-weight concern) becomes a first-class metric of this pre-pass.

**Target resolution is SELECTION, not generation (owner question, 2026-08-10: "are we passing the
catalog with the prompt?").** Three stages, deterministic-first:

1. **Exact match, no LLM.** Normalise the hypothesis tokens and match against catalog column names
   (case/underscore-insensitive). A user who typed `cust_status_flg` never involves a model at all —
   most intents name their target literally.
2. **Closed choice from a passed shortlist.** Only when nothing matches exactly ("the churn flag"),
   the LLM picks from a READ-SCOPED candidate shortlist included in the prompt — names + concepts +
   one-line summaries, derived by searching the catalog with the hypothesis's own terms — or
   abstains. Selection from a provided list, never free generation of a name: the concept
   classifier's closed-vocabulary pattern, applied to columns. At today's 237 columns the whole
   name list fits trivially; at the 150K-column ambition the search-derived shortlist IS the
   scaling mechanism.
3. **Membership validation regardless** — defence in depth: even an answer copied from the provided
   list is re-checked against the catalog before it feeds the veto, because a model can miscopy.

Same shape as everywhere: deterministic first, LLM for the residual, code validates the landing.

**REVISED (owner decision, 2026-08-10): the intake READING is MANDATORY — one call per new
hypothesis.** The staged framing above governs the target COLUMN only, and the owner caught its
hole: an exact-name match short-circuited the model entirely, silently dropping the ticket's other
three fields — label window, target type, business domain — which only prose can yield. A user who
types the exact column name AND writes "churn means no activity in 90 days" would have had the
near-label critic run blind precisely because they were maximally cooperative. So: every new
hypothesis gets ONE intake call (cached by hypothesis text) that always fills the full ticket.
Three rules carry over unchanged:

* **An exactly-typed column name PINS the column** — the model can never override it. The mandatory
  read upgrades the old shortcut into a cross-check: if the typed name and the prose disagree ("you
  named `cust_susp_flg`; your description reads as churn"), the confirm screen surfaces the
  contradiction instead of silently trusting either side.
* **Human confirmation still gates** (the owner's UI requirement above) — the mandatory call drafts,
  the human decides.
* **Failure degrades, never blocks**: on model failure or timeout, an exact-named target proceeds
  code-resolved with the window absent (near-label candidates withheld per the abstain rule); a
  fuzzy target falls back to search results + human pick. Mandatory to attempt, never load-bearing.

Net cost is NEGATIVE: the `business_domain` field folds Task 4's hypothesis→use_cases mapping —
previously its own mandatory call — into this one. One read of the question, one cached ticket,
four consumers (veto, near-label critic, ordering, parameter choice).

**What the fuzzy path sends per candidate column (owner-confirmed 2026-08-10):** exactly three
fields — column ref, concept, and the one-line `ai_summary`. The summary, not the full definition:
definitions run to paragraphs (up to 32k chars) and 237 of them drown the signal; the summary is the
one-liner enrichment wrote for discovery, exists 237/237, and is the same field search ranks on. All
three fields are already egress-graded — nothing new crosses the boundary. The reply must be a ref
from the sent list or abstain; membership-validated regardless.

**Search is staged; the upgrade trigger is a metric, not a conviction (recommendation adopted
2026-08-10).** Stage 1 exact match (no model). Stage 2 NOW: one code-driven search builds the
candidate list — at 237 columns the list is the whole catalog. Stage 3 LATER: the model drives the
same two functions (`search_columns`, `inspect_column`) as bounded, audited tools — built as
internal functions today so promotion is a caller change, not a rework. Raw SQL access is
permanently off the table: read scope and egress grading live inside the curated functions, and the
database also holds decisions, audit and redacted inputs. Stage 3 builds only when one of three
named triggers fires: (a) the correction/abstention rate says the shortlist misses, (b) a catalog
too large for the shortlist to be the catalog, (c) cross-catalog target resolution. Every
unmeasured "this will be transformative" this week measured small — this decision is wired to a
counter instead.

**HUMAN CONFIRMATION OF THE TARGET, ON THE UI (owner requirement, 2026-08-10).** The extracted
target is the single most safety-critical value in the flow — it drives the leakage veto — and it
must not take effect as an unreviewed model pick. Before generation runs:

> "I understood your target as: `cust_status_flg` — *Current lifecycle status of the customer
> relationship.* (matched on: 'churn' ≈ relationship status)"
> **[ Yes, that's my target ]  [ Change it ]  [ No target — just exploring ]**

* **Change it** surfaces the model's ranked runners-up (one-click correction) plus a search box —
  correcting is never a restart.
* **Just exploring** is a legitimate answer, not a failure (the links-usable-before-confirmation
  steer): generation runs, near-label candidates are withheld with "declare a target to see this."
* **On confirm the target becomes a recorded HUMAN decision** — provenance flips from
  `llm/proposed` to `human/confirmed`, persisted with the intent, on the audit trail. The veto then
  runs on a value a human signed.
* **Exact-match path shows, doesn't gate** (default; uniform-click alternative offered and not
  taken): a user who literally typed the column name sees "Target: `cust_status_flg` ✓ (you named
  it)" with an edit affordance and no mandatory click. The confirmation GATE applies to the fuzzy
  path, where a model interpreted.
* **The confirm/correct clicks are the extractor's ground truth**: correction rate is the primary
  quality metric of this pre-pass and the stage-3 trigger (a) above — the approval UI is also the
  evaluation harness.

**#4 (adopt, redesigned).** The finding is real — nothing feeds gauntlet refusals back, so a refused
tie-break winner is re-proposed on every cache miss. But the LLM-prompt-injection fix adds
nondeterminism management for little gain. Deterministic version: when the adjudicated winner's
candidate is gauntlet-refused, RE-BIND to the next tied candidate and re-run — a retry loop in
grounding, no model in the loop — and record the refusal code beside the verdict in
`structured_result` so Step-0 instrumentation surfaces chronic refusals. Fold into Task 2's spec.

**#1 (adopt as design, keep the gate).** The decided 4b emission policy is already ONE card per
recipe (hypothesis-chosen parameters), so the 6× blast only exists if top-K widening is later
chosen. The two-phase gauntlet (in-memory static checks first, I/O checks on survivors) and batched
freshness/profile reads are the right mitigation WHEN that gate opens — spec then, not now.

**#3 (reject as specified).** HyperLogLog needs data; **ingest has none.** Verified this session:
`CanonicalRow` carries schema and glossary metadata only — no rows, no samples; its `cardinality` is
a DECLARED string; the profiler was retired in the upload-catalog pivot. There is nothing at ingest
to sketch. The real path to bridge cardinality already exists in the data model: the crosswalk
OBSERVATION store (migration 1057, `crosswalk_observation_revision`, with `source_to_target_max_matches`
/ `target_to_source_max_matches` / `row_coverage` fields) is designed for exactly these measurements
— produced by the data agent against the physical cluster, not by ingest. Until observations run:
declared + human-confirmed cardinality, and the gauntlet refuses additive aggregations across an
unmeasured bridge (already Task 0's rule). The reviewer's fan-out gating idea is right; the venue is
wrong.

**#5 (reject as specified).** Two defects. The premise — "Tier-1 recipes are blocked by 15% entity
coverage" — is false: R1/R2 ground by CONCEPT match (`cif_id` carries `customer_id`; `_candidate_score`
scores `col.concept == need.concept` at −4 with no graph `entity` read); the governed-entity branch
is an alternate path for `is_grain` columns, not a gate. And the mechanism — inherit the table
grain's entity across all columns — is semantically wrong in exactly the catalogs at hand:
`ftr.counter_party_cif_id` sits in a customer-grain table and describes the COUNTERPARTY; blanket
inheritance would relabel it `customer`, manufacturing the same class of error the BIC
misclassification just taught. The safe version already runs: `axis_projection` fills `entity` from
identifier concepts' `entity_link`, fill-only-NULL, skip-loud on governed facts. The genuine lever
is registry `entity_link` coverage plus the existing suggest→confirm loop — Task 5 as written.

**#6 (park).** Real capability, no measured demand: the blocked-concept histogram shows missing
CONCEPTS, not compound-key failures, and compound keys already exist where joins execute (crosswalk
definitions carry 1–16 member pairs, migration 1050 CHECK). Spec when a recipe or crosswalk
observation actually needs the router to see a tuple.

## What NOT to do

Do not let an LLM author templates. The 157 are SME-written and safe-by-construction, each carrying
its PIT declaration, additivity, leakage flag and eligibility note. That is the property that stops a
plausible-looking feature from predicting the answer from the answer. Keep the shape: the model may
propose and rank; the gauntlet disposes.

Do not let the Strategist widen. Any selected subset must be ⊆ `ALL_TEMPLATES` — the constraint
`_template_candidates` already documents.
