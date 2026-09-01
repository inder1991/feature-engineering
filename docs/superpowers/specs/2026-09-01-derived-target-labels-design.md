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
  "grain_ref": "public.bo_cib_customer.cust_num",
  "as_of_ref": "public.bo_cib_customer.business_dt",
  "window_days": 90,
  "direction": "forward",
  "label_type": "binary"
}
```

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
  "population_filter": "from_values"
}
```

`population_filter: from_values` excludes rows that already have the outcome at the as-of date — a
customer already non-performing on 1 January is not a candidate. Omitting this is the most common
way to build a silently broken label, so it is an explicit field rather than an implicit default.

### 7.3 Shape — `event_window`

Rows in a second table, inside the window, joined to the grain.

```json
{
  "shape": "event_window",
  "event_table": "public.comp_financial_tran_repos_dly",
  "event_date_ref": "public.comp_financial_tran_repos_dly.pstd_date",
  "join": { "left":  "public.bo_cib_customer.cust_num",
            "right": "public.comp_financial_tran_repos_dly.cif_id" },
  "event_filter": "tran_crncy <> 'AED'",
  "aggregate": "count",
  "operator": ">=",
  "threshold": 1
}
```

`event_filter` is the one free-text field and therefore the one that needs bounding — see §12.

**Cross-catalog by construction.** This example is anchored in `cib` and counts events in `ftr`.
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

## 7.5 The flow: search first, then propose, then confirm

Four steps, in this order. **The order is the design** — a registry that is written but never read
is a junkyard, and proposing before searching is how it becomes one.

### Step 1 — search the registry (no model call)

Before anything is generated, look for labels that already exist for this entity, ranked against
the hypothesis. `tgt_churned_90d` authored six months ago must surface for the next person who asks
about churn, or two teams end up with two quietly different churn definitions and no way to compare
the models trained on them. For `cust_perf_nonperf_flg` that is not merely untidy: *non-performing*
carries a supervisory meaning, and three private versions of it is an audit finding.

Each hit is shown with what makes reuse decidable: its rule in plain English, its window, its
`DESIGN-CHECKED` state, and **how many models already consume it**.

### Step 2 — propose new candidates only for the gap

The proposer emits **complete structured rules**, never prose for a human to formalise — a proposal
a person has to translate is not a proposal.

**Discipline: selection over a closed structure, not free generation.** This is the intake ticket's
rule (`intake_ticket.py`) applied to a richer object, and for the same reason — a model that may
invent a ref produces a rule that guards an empty room.

*Input:* the redacted hypothesis and prediction goal; the read-scoped column shortlist with each
column's ref, concept, `semantic_terms` and `declared_type`; the confirmed entity; the horizon.

*Output:* 2–4 candidate rules, each a complete header + shape, each naming its own source columns
and a one-line plain-English reading.

**Validation, code-side, on every candidate:**

- every ref must appear in the shortlist — an invented ref invalidates that candidate, it is not
  repaired;
- join keys must share an entity (`cust_num` ↔ `cif_id`, both `entity = customer`);
- `direction` is always `forward`; a proposal that says otherwise is rejected outright rather than
  corrected, because a backward "label" is a feature and the confusion must surface;
- `window_days` comes from the stated horizon when the text gives one. Since 2026-08-30 the
  prediction goal reaches this read, so *"in the next 90 days"* written in the goal is now visible
  where it previously was not.

**The options must differ in substance, not in wording.** For an FX hypothesis the two definitions
in §7.4 — currency ≠ AED, versus currency ≠ counterparty currency — are a genuine business choice
and both belong on screen. Three windows over one rule is one option presented three times, and is
the failure mode to design against.

**What the proposer may not decide.** The `state_change` values (§11), and which of two defensible
business definitions is correct. Those are put to the human.

### Step 3 — present both together, distinguished

Existing labels and fresh proposals appear in one list, never merged into an undifferentiated set:
an existing governed label is a **decision the organisation already made**, a proposal is a draft.
Existing ones rank first.

**The bias is toward reuse, and it is deliberate.** A governed label three models already train on
beats a marginally better new one, because comparability across models is the point of a registry.
The tool says so rather than leaving the person to infer it.

**Near-duplicates must be named, not silently minted.** Content-hashing catches an exact repeat, but
`tgt_churned_60d` proposed while `tgt_churned_90d` exists is the case that quietly fills a registry
with twins. When a proposal differs from an existing label only in its window or its threshold, the
tool says which one, and how they differ, before the person picks.

### Step 4 — the human decides, and may author their own

Four available acts, not one:

- **reuse** an existing label unchanged;
- **adapt** one — a new definition, a new name, and the ancestor recorded, so "we changed churn from
  90 to 60 days" is a visible fact rather than an archaeology exercise;
- **accept** a proposal, confirming anything the proposer could not know (§11);
- **author** a rule outright. The owner's requirement is that a person may *give a suggestion on the
  target*, so the structure is directly editable and a human-authored rule is a first-class
  definition — validated by exactly the checks in Step 2, and never weaker ones because a person
  typed it.

Whichever act is taken, the outcome is one named, content-hashed definition signed by a person. A
label is never adopted by default and never inherited silently from a proposal nobody accepted.

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

## 11. What the tool cannot know

`state_change` needs the column's actual values (`from_values` / `to_values`).
`cust_perf_nonperf_flg` is a `varchar(20)` and nothing in the catalog records what it contains.

The tool therefore proposes the **shape** and asks the human to confirm the values. It must not
guess them. If value profiling later exists, this becomes a proposal rather than a blank — but the
human confirmation stays, because a wrong value here produces a label that is silently always 0.

---

## 12. Open questions for review

1. **`event_filter` is free text**, and free text is how a definition language becomes an
   injection surface and an un-auditable grammar. Options: a closed predicate structure
   (`{column, op, value}`), an allow-list of operators over catalog-resolved refs, or accepting
   text with the pipeline owning validation. Recommend the closed structure; it costs expressiveness
   for `OR` chains.
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
