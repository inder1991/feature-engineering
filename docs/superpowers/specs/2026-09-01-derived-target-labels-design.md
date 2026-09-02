# Derived target labels — design

**Date:** 2026-09-01
**Status:** design, pending review. No code written.
**Scope decision (owner, this session):** the platform **specifies** the label rule; the customer's
own pipeline **executes** it. See §4.

---

## 1. The problem

The platform assumes a training label already exists as a column in the catalog. It usually does
not. A label is normally *constructed*:

> A customer has churned if they made no transaction for 90 days.

That rule is run over history to produce labels. Nothing in the platform can express it, and the
target-selection flow cannot represent a target that is not a column.

**Evidence from the deployed catalogs (`cib`, `ftr`, 2026-08-29/30):**

| | |
|---|---|
| Columns across both catalogs | 237 |
| Concept coverage | 100% |
| Columns in the **outcome** family (a certified label) | **0** |
| Columns in the **near_label** family | 8, all in `cib` |

Zero certified labels is not a tagging failure — it is the expected state, because the labels these
users need were never going to be stored. The consequence, observed live: the intake reading
proposed `cust_susp_flg` (a compliance *suspension* flag) as the target for an AML hypothesis,
because it was the nearest thing to an outcome the catalog held.

**The deeper symptom.** Even where a plausible column exists, reading it once is not a label.
`cust_perf_nonperf_flg` read on 1 January says *"this customer is performing"* — a present fact the
model can already see. The label is the **change**: performing at the as-of date, non-performing
within the window. The column is an ingredient, not the answer.

---

## 2. What exists today

Two half-representations, with no bridge between them.

**At intake** — a label is a *pointer to an existing column*. `contract_intent.target_ref`,
validated by `is_readable_column`; a ref that does not resolve is refused at confirm.

**In the model contract** (`overlay/upload/model_feature_contract.py`) — a label is a *prose
definition plus a window*:

```python
target_definition: str      # "the label, in reviewed words"
outcome_window_days: int
prediction_grain: str
prediction_timestamp_role: str
```

and the recipe registry already carries entries such as
`target_definition="customer closes their primary relationship within the window"`.

So the platform already models that a label **has** a definition, a horizon, a grain and an as-of
anchor. It holds the definition as English for a human to read, and never computes it. A search of
`main` finds no `target_formula`, no executable `label_definition`, and nothing that materialises a
label over history.

---

## 3. Why it has no home

The module header of the model contract states the design's line: *"a prediction is not a
formula."* The platform has a slot for **deterministic feature formulas** (Formula-v2 recipes, ~20
modules) and a slot for **model output declarations**. A training label is deterministic — it *is*
a formula — but it is an outcome, so it sits in neither.

The reason it cannot simply reuse the feature-formula lane is a single inverted property:

> **A feature must never read forward of the as-of date. A label must read exactly forward of it.**

Point-in-time correctness, the as-of basis, the leakage veto, the near-label critic and
`leakage_anchor` all exist to enforce the first. `"no transaction in the 90 days after the as-of
date"` is, to every one of those controls, the definition of leakage.

A label lane must therefore make the forward read **declared and bounded** rather than forbidden.
That declaration (`direction`, `window_days`) is the governed object.

---

## 4. Scope

**In scope.** Propose candidate label rules from a hypothesis; express a chosen rule as a
structured, versioned, reusable definition; record its lineage; expose it for a downstream pipeline
to execute.

**Out of scope (this design).** Executing the rule, materialising labels, backfill, replay, drift.
The owner's decision is that the customer's pipeline executes. This is not a deferral for later
convenience — it changes what the platform may *claim*, and §9 states the consequence.

**Non-goals.** A general expression language. A rule that reads three or more tables. Multi-class
labels beyond a declared value set. Executing anything.

---

## 5. Naming

Label names carry a `tgt_` prefix (owner's decision) and otherwise obey the existing feature-name
rule, `^[a-z][a-z0-9_]{0,127}$`, so `tgt_churned_90d`, `tgt_npe_90d`, `tgt_fx_active_90d`.

The window belongs in the name because a different window is a different label, not a variant of
one. `tgt_churned_60d` and `tgt_churned_90d` are two labels, both governed.

---

## 6. Registry shape

Mirrors the feature registry (`feature`, `feature_definition`, `feature_derives_from`,
`feature_consumer`) — the owner asked for reuse across models "similar to the feature registry",
and the pattern already carries every property needed.

| Table | Holds |
|---|---|
| `target` | the named label: unique name, description, entity, verification state, lifecycle |
| `target_definition` | the content-hashed rule (immutable; deduped by hash; unique name per entity) |
| `target_derives_from` | every column ref the rule reads — see §8 |
| `target_consumer` | which runs / model contracts use it |

Content-addressing gives the same property it gives features: an identical rule authored twice is
one row, and any edit is a new definition rather than a mutation of a definition other models are
already trained against.

---

## 7. The rule

Two typed shapes sharing one header. Chosen over a single object with a `shape` discriminator (half
its fields null at any time) and over an expression language (an interpreter nobody can audit, a
grammar that grows forever, and no static safety check). Two closed types match how the rest of the
platform works — `RecipeDefinitionV2` and `ModelFeatureSpecV1` both refuse at construction.

The decisive property: for any rule, **which columns it reads and over what window is answerable
without executing it**. Both the safety controls and the downstream pipeline need that.

### 7.1 Common header

```json
{
  "name": "tgt_npe_90d",
  "entity": "customer",
  "anchor_catalog": "cib",
  "grain_ref": "public.bo_cib_customer.cust_num",
  "as_of_ref": "public.bo_cib_customer.business_dt",
  "window_days": 90,
  "as_of_frequency": "monthly",
  "require_full_window": true,
  "direction": "forward",
  "label_type": "binary"
}
```

**`as_of_frequency` and `require_full_window` are what make this a training set rather than a
sentence.** Added 2026-09-02 after an architect/data-scientist review; both were missing and both
produce silently wrong labels by their absence.

*`as_of_frequency`* — WHICH as-of dates the rule is evaluated on. Mandatory and undefaulted: a rule
that omits it does not define a dataset, and two teams using "the same" label at different
frequencies get different training sets, which destroys the comparability this registry exists to
provide. A different frequency is a different dataset and earns its own name, exactly as a
different window does (§5).

*`require_full_window`* — **censoring**. A customer as-of 15 November with a 90-day window needs
history through 13 February to be observable. Where it is not, a rule that emits 0 says *"did not
happen"* when the truth is *"cannot see"*: every recent row becomes a false negative and the model
learns that recent customers are safe, which is exactly backwards. It is the most common labelling
error in the field. The default refuses; a survival design that handles censoring itself may switch
it off deliberately, and the exception is then on the record.

**`anchor_catalog` is not optional decoration.** `graph_node.object_ref` is only
`public.{table}.{column}`; the catalog is a separate column, so a bare ref does **not** identify a
column. `_column_meta` already scopes every lookup to an exact `(catalog_source, object_ref)` pair
for precisely this reason — its docstring cites finding **M3**: *"a same-named column in another
catalog cannot contaminate the reading"*. A rule carrying bare refs would reintroduce a defect this
codebase has already fixed once. Two catalogs each holding a `public.customers` table is all it
takes.

Per **side** rather than per **ref**: a `state_change` rule reads one catalog, and an
`event_window` rule reads exactly two. Qualifying every field would repeat the same value five
times and still not say, at a glance, that a rule spans `cib` → `ftr`.

`direction` is the governed field: the one declaration in the platform that reading ahead of the
as-of date is legitimate, and by exactly how much.

`label_type` decides whether the rule *thresholds* or *measures*, and the two are exclusive:

| `label_type` | `operator` / `threshold` | The label is |
|---|---|---|
| `binary` | **required** | 1 when the comparison holds, else 0 |
| `count` | **must be absent** | the row count itself |
| `amount` | **must be absent** | the aggregated value itself |

Supplying a threshold on a `count` label, or omitting one on a `binary` label, is refused at
construction. This is the field pair most likely to be filled in inconsistently by a proposer, so it
is checked rather than trusted.

### 7.2 Shape — `state_change`

A column's value at the as-of date compared against its value inside the window. Requires an
append-only snapshot source (§10).

```json
{
  "shape": "state_change",
  "column_ref": "public.bo_cib_customer.cust_perf_nonperf_flg",
  "from_values": ["Performing"],
  "to_values":   ["Non-performing"],
  "at_least_once": true,
  "population_filter": "from_values",
  "exclude_null_at_as_of": true
}
```

`exclude_null_at_as_of` — a NULL at the as-of date means the row's eligibility cannot be
determined. Including it silently invents an answer, so the default drops the row.

`population_filter: from_values` excludes rows that already have the outcome at the as-of date — a
customer already non-performing on 1 January is not a candidate. Omitting this is the most common
way to build a silently broken label, so it is an explicit field rather than an implicit default.

### 7.3 Shape — `event_window`

Rows in a second table, inside the window, joined to the grain.

```json
{
  "shape": "event_window",
  "event_catalog": "ftr",
  "event_table": "public.comp_financial_tran_repos_dly",
  "event_date_ref": "public.comp_financial_tran_repos_dly.pstd_date",
  "join": { "left":  "public.bo_cib_customer.cust_num",
            "right": "public.comp_financial_tran_repos_dly.cif_id" },
  "event_filters": [
    { "column_ref": "public.comp_financial_tran_repos_dly.tran_crncy",
      "op": "!=", "value": "AED" }
  ],
  "aggregate": "count",
  "operator": ">=",
  "threshold": 1,
  "population_filter": { "lookback_days": 180, "having": "none" }
}
```

**`event_filters` is a CLOSED structure, resolved from §12.1.** Each condition is
`{column_ref, op, value | values | value_ref}` with `op` drawn from a closed set; conditions are
ANDed. There is deliberately no `OR` — that is the stated cost — and `in` / `not_in` cover the case
that most often needs it ("currency in (USD, EUR)").

Three reasons, and the third is what forced it:

1. Free text in a stored, MODEL-AUTHORED definition is an injection surface.
2. A grammar nobody bounded grows forever and cannot be audited.
3. **It made lineage lie.** A filter written `"tran_crncy <> 'AED'"` reads a column `refs_read`
   cannot see, so `target_derives_from` would answer *"no label depends on `tran_crncy`"* about the
   very label defined by it. The closed structure puts both sides of every condition — including a
   `value_ref` comparing two columns — into the lineage.

`value_ref` also removes an unverifiable literal from the commonest case: *"a conversion actually
happened"* is `tran_crncy != counter_party_tran_crncy`, which needs no guessed currency code at
all (§11).

**`population_filter` decides which question is being asked**, and without it only one of two very
different labels is expressible. `having: "none"` restricts the population to rows with no matching
event in the `lookback_days` before the as-of date; `having: "any"` is the whole population.

On *"predict which customers will do FX"* that is the difference between:

- **who will do FX at all** — customers already trading FX weekly dominate, and the model largely
  restates last month. True, and commercially close to useless.
- **who will START** — the acquisition question, and almost certainly what is meant.

`state_change` has carried this control since its first draft (`population_filter: from_values`
excludes rows that already have the outcome). `event_window` lacked it, which made the second
question unaskable. Discovered by walking a real hypothesis through the design; the lookback is a
required conversation turn (§7.5) because no default is safe — `"any"` silently produces the
degenerate label.

**Cross-catalog by construction**, and now legibly so: `anchor_catalog: cib`,
`event_catalog: ftr`. This example is anchored in `cib` and counts events in `ftr`.
Labels naturally span catalogs, because the outcome lives where the events are. It needs none of
the live cross-catalog planner: the join is *declared in a reviewed definition*, not decided by a
planner at request time. (As of this date the entity-only cross-catalog route is still refused
unconditionally — `SEMANTIC_REQUIRES_CATALOG_SOURCE` — so this is a real advantage, not a
coincidence.)

### 7.4 Worked examples on the deployed catalogs

| Name | Shape | Rule |
|---|---|---|
| `tgt_npe_90d` | state_change | `cust_perf_nonperf_flg` Performing → Non-performing within 90d |
| `tgt_restricted_90d` | state_change | `cust_susp_flg` → suspended within 90d |
| `tgt_churned_90d` | event_window | zero `ftr` rows for the customer in 90d |
| `tgt_fx_active_90d` | event_window | ≥1 row with `tran_crncy <> 'AED'` in 90d |

`tgt_fx_active_90d` has a second defensible definition —
`event_filter: "tran_crncy <> counter_party_tran_crncy"`, i.e. a conversion actually occurred,
rather than merely a foreign-currency payment. Which is correct is a business question. **The tool
proposes both and the human chooses**; it must not silently pick one.

---

## 7.5 Authoring is a conversation that lands on structure

**Conversational interface, structured artifact.** The owner's steer is that the target is the
critical component and deserves a dialogue rather than a list to pick from. The dialogue is the
interface; the artifact it produces is still the rule in §7.1–7.3, content-hashed and signed.

The distinction is not stylistic. The platform already has the fully-conversational version —
`ModelFeatureSpecV1.target_definition` is a string, "the label, in reviewed words", agreed by
humans and executable by nothing. That is precisely why no label can be computed today. **A
conversation that ends in prose rebuilds the defect this design exists to remove.** Talk in
English; land on structure.

### What the conversation is for

To resolve what code cannot determine, **in context and one question at a time**. A general policy
("what should the tool do about literals?") is mostly unanswerable; a situated question ("does
*performing* mean the value `P` in this column?") is answerable in a word.

**The closed list of things the tool must ask about:**

| Unknown | Why it cannot be resolved | Example |
|---|---|---|
| Filter literals | no column-value profiling exists | is the code `AED`, `aed`, or `784`? |
| `state_change` values | same | does the flag hold `Performing`/`P`/`1`? |
| Population | a business decision, not a data fact | who will do FX *at all*, or who will *start*? |
| Competing business definitions | both defensible | foreign-currency payment, or an actual conversion? |
| The window, when the text states none | — | how far forward? |

**What it must NOT ask**, because it can determine them: whether a ref resolves; whether join keys
share an entity; the base currency where the catalog declares one (`graph_node.currency = 'AED'` on
`ftr.tran_amt_aed` — governed metadata, not a guess); the window when the goal states one.

Asking about something the platform already knows trains people to click through questions, which
is how the one question that mattered gets clicked through too.

### Registry reuse, in the dialogue

Reuse is raised **when it becomes relevant**, not as a ranked list before the person has said what
they mean:

> `tgt_fx_new_60d` already exists — same rule, 90-day lookback rather than 180. Use it, or is 180
> deliberate?

That is a far stronger nudge than a list at the start, because by then the tool knows enough to say
what actually differs. The reuse bias of the previous draft still holds: a label other models
already train on beats a marginally better new one, because comparability is the point.

### Termination

The conversation ends one of three ways: an **existing** label adopted; a **new** definition
registered; or **abandoned**, registering nothing. There is no fourth path where a label is adopted
by default — an unanswered question ends the conversation rather than being filled with a guess.

The human may also **author or override outright** at any turn, by editing the structure directly.
A human-authored rule is first-class and faces exactly the validation below — never weaker checks
because a person typed it.

### The transcript is provenance

The exchange is retained with the definition. A pick-from-list flow records *what* was chosen; a
conversation records *why* — that conversion was meant rather than foreign-currency, that 180 days
was deliberate. For a label feeding regulated models, that reasoning is worth as much as the rule,
and it is the one thing this design gets for free that the previous draft did not.

### Validation is never conversational

The dialogue decides what the rule *says*. Code decides whether it is *well-formed*, and the
conversation cannot soften it:

- every ref resolves in the read-scoped catalog; an invented ref is rejected, not repaired;
- join keys share an entity (`cust_num` ↔ `cif_id`, both `entity = customer`);
- `direction` is always `forward` — a backward "label" is a feature, and the confusion must surface
  rather than be corrected silently;
- `label_type` and `operator`/`threshold` pair per §7.1;
- no unresolved literal survives into a signed definition.

### Worked example

> **Analyst:** predict which customers will do FX transactions in the next 60 days
>
> **Tool:** What counts as FX here — a payment in a currency other than AED (your catalog declares
> AED as base), or one where the two legs differ, meaning a conversion actually happened?
>
> **Analyst:** conversion
>
> **Tool:** Predicting who will do FX *at all*, or who will *start*? With "at all", customers
> already trading FX weekly dominate and the model largely restates last month.
>
> **Analyst:** who will start
>
> **Tool:** How far back should "not currently doing FX" look — 90 days?
>
> **Analyst:** 180
>
> **Tool:** Registering `tgt_fx_new_60d` — one row per customer as of `business_dt`; 1 when the
> customer makes ≥1 transaction with `tran_crncy ≠ counter_party_tran_crncy` in the next 60 days,
> among customers with none in the prior 180. Reads `ftr.cif_id`, `ftr.pstd_date`,
> `ftr.tran_crncy`, `ftr.counter_party_tran_crncy`, `cib.cust_num`, `cib.business_dt`.

Four turns; the same structured object as a pick-from-list flow, reached by asking. Note the second
turn: the population question is the one a list would have silently decided.


## 7.5b Where `entity` comes from

Owner's decision, 2026-09-02: **the person chooses, from a closed catalog-derived list.**

`selectable_entities(conn, catalog_source)` returns only entities with a **keyed spine table** — an
`is_grain` column carrying that entity. On `cib` that is `customer`; on `ftr`, `transaction` and
`account`. One click, and it cannot be wrong in the way a guess can.

Two sources were rejected:

* **The recogniser's `target_entity`** — the field this whole engagement opened by calling
  inaccurate. Run live against an AML hypothesis beginning *"Customers whose…"* it returned
  **`None`**. It offers a bare 38-name vocabulary with no descriptions and no confidence band, and
  many of those entities are things nothing can key on. Unfit to decide a grain.
* **Deriving it from the target column's table** (what `/contract/uoa-proposal` does) is strictly
  better-grounded, but it needs the target column first — and here the entity is an *input to*
  proposing the target. Circular.

The derivation returns as a **check** rather than a source: `check_target_against_catalog` confirms
`grain_ref` really is that entity's key, and refuses otherwise — *"grain_ref keys 'account', but
the rule declares entity 'customer'"*. Choosing `customer` while anchoring on a column that is not
the customer key makes every row of the label the wrong shape, and nothing else would catch it.

**An empty list means this catalog cannot anchor a label at all.** The caller must say so rather
than render a blank dropdown, which reads as a bug. Live coverage is thin — 8 of `cib`'s 111
columns and 15 of `ftr`'s 126 carry an entity — so this degrades quietly if a future catalog has no
tagged grain.

---

## 7.6 The sentence a person gives concurrence to

`describe_target(rule)` renders the whole rule as one plain sentence, deterministically and with no
model call, so it can never drift from what was registered:

> `tgt_npe_90d`: one row per customer, as of `public.bo_cib_customer.business_dt`. The label is 1
> when `cust_perf_nonperf_flg` moves from `['Performing']` to `['Non-performing']` over the next 90
> days, sampled monthly, only where the full 90 days can be observed, among those starting in that
> state. Rows whose state cannot be read at the as-of date are excluded.

**A form of a dozen fields gets rubber-stamped; a statement of meaning gets read.** The
conversational draft of this design produced such a sentence as its final turn; moving to a form
lost it, and this restores it. It deliberately states the sampling frame and the censoring rule —
the two things a data scientist checks first and the two a field list buries.

---

## 8. Leakage — the correction

An earlier draft of this design proposed recording `target_derives_from` so the leakage check could
**block features that read the label's source columns**. That rule is wrong and would have made the
platform's own use cases impossible.

Take `tgt_fx_active_90d`. The natural features for "who will transact in FX, from past trend" are
FX count in the last 90 days, FX amount in the last 90 days, days since last FX transaction. Every
one reads `tran_crncy` and `pstd_date` — **exactly the columns the label reads**.

| | Columns | Window |
|---|---|---|
| Label | `tran_crncy`, `pstd_date` | next 90 days — forward |
| Feature | `tran_crncy`, `pstd_date` | last 90 days — backward |

Same columns, opposite directions. That is not leakage; it is the method. A column-overlap rule
would reject every useful feature for this hypothesis.

**The control is temporal, and it already exists.** Point-in-time enforcement constrains every
feature to the as-of date or earlier, which is precisely the line between the two rows above. This
design adds no blocklist and weakens nothing.

The residual risk is narrower and worth naming: a feature reading a **snapshot column overwritten
in place** would silently return a later value than its as-of implies. `cib` appends daily
snapshots, so the deployed case is sound — but the assumption must be *recorded* rather than
assumed, which is what `as_of_ref` does.

---

## 9. `target_derives_from` — what it is actually for

Not a blocklist. Two jobs:

1. **Impact.** *"`tran_crncy` is being retired / re-tagged — which labels break?"* No answer exists
   today.
2. **Reproducibility.** A label's meaning depends on the columns it reads. Recording them makes a
   definition auditable without re-deriving it.

---

## 10. The verification ladder under Option B

The feature ladder is `UNVERIFIED → DESIGN-CHECKED → DATA-CHECKED → USEFULNESS-CHECKED`.

Because the platform does not execute the rule, it can reach **`DESIGN-CHECKED` and no further**:
the rule is well-formed, every ref resolves in a read-scoped catalog, the join keys share an entity,
the as-of source is append-only, the window is positive.

It **cannot** claim `DATA-CHECKED`, because it never sees the labels produced — it cannot know the
class balance, whether the population is empty, or whether the rule matched nothing at all.

This must be stated in the ladder now rather than bolted on, because a label sitting at
`DESIGN-CHECKED` forever is easily misread as "checked". Reaching `DATA-CHECKED` requires the
executing pipeline to report an outcome back; that is a later design, and it is the single most
valuable extension.

---

## 11. Why the unknowns are asked rather than guessed

§7.5 lists what the conversation must ask about. This section records **why guessing is not an
option there** — the failure mode is silent in both directions, and opposite, so neither looks like
an error.

**There is no column-value profiling anywhere in the platform.** `catalog_profile_revision`
describes a *catalog* (display name, business context, domains), not the contents of a column.
`cust_perf_nonperf_flg` is a `varchar(20)` and nothing records whether it holds `Performing`, `P`
or `1`. Verified 2026-09-01.

| Wrong guess | Result | How it looks |
|---|---|---|
| `state_change` value never matches | label always **0** | a model that cannot learn — noticed |
| `event_filter` literal never matches | label always **1** | *"every customer does FX"* — a model that trains, scores, and is worthless |

The second is the dangerous one: it produces a plausible pipeline all the way to deployment.

**The catalog is used where it genuinely knows.** `graph_node.currency = 'AED'` on `ftr.tran_amt_aed`
means AED-as-base is governed metadata, and the tool must take it from there rather than ask. The
rule is: ask only what the platform cannot determine, because a question about something already
known teaches people to click through questions — including the one that mattered.

Should value profiling arrive later, these become pre-filled proposals rather than blanks. The
confirmation stays either way.

---

## 12. Open questions for review

1. ~~**`event_filter` is free text**~~ — **RESOLVED 2026-09-02, owner's decision: the closed
   structure.** Implemented as `event_filters` (§7.3), ANDed, no `OR`. The deciding argument was
   not the injection surface but the lineage defect: the free-text form made
   `target_derives_from` answer "nothing depends on this column" about a column a label was
   defined by. Zero labels were registered at the time, so the shape changed with no data
   migration.
2. **Who may change a definition** other people's models are trained against. Content-addressing
   makes an edit a new row, but the *name* must resolve somewhere — needs an active-revision
   pointer and a rule about moving it, mirroring `feature_active_revision`.
3. **Does a label supersede `target_ref`** on `contract_intent`, or sit beside it? A column target
   is the degenerate case of a rule; folding them is cleaner but touches the signed-reading path
   and the confirm gate.
4. **Population definition** beyond `population_filter` — entity-level exclusions (staff accounts,
   closed relationships) are real and currently unexpressible.

---

## 13. Deferred, deliberately

- Execution, materialisation, backfill, replay.
- `DATA-CHECKED` and above (needs §10's feedback path).
- Labels spanning three or more tables.
- Multi-class and survival-style targets.
