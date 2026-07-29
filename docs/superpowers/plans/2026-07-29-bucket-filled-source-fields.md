# Column Summaries, End to End — Plan (rev 3)

> **Rev 3 makes this plan SMALLER, not bigger.** Rev 1 would have blanked `domain`. Rev 2 fixed that
> but kept a `semantic_domain` lane whose ownership, inheritance, payload bound and vocabulary
> lifecycle were all unspecified — and whose "one-line" safety guard turned out to preserve the AI
> value rather than the bank's. Rev 3 ships the one thing that is unambiguously correct and already
> half-built, and moves the domain lane to a separate plan where it can be specified properly.
>
> Every claim below was read from the code and re-checked after writing. §5 records what rev 1 and
> rev 2 got wrong.

**Scope:** finish `ai_summary`. Nothing else.

**Explicitly out of scope, and why:** the `semantic_domain` lane — see §4.

---

## 1. Why this, and only this

The description problem is real: 47 distinct descriptions over 111 CIB columns, one sentence
covering 12, including the `cust_curr_ntb_flg` / `cust_prev_9mnth_ntb_flg` pair whose difference is
the whole signal.

It is already solved, safely, by a field built earlier this session. `ai_summary`:

* targets **every** column (`_summary_targets`), so a bucket-filled description cannot block it;
* writes to its **own** field, so the source's `definition` keeps its text and its
  `source/attested` authority — no competition, no conflict, nothing to blank;
* receives the full `_concept_metadata` payload — term_name, declared type, BIAN path, source
  attributes — where `draft_definitions` sends only `{table, column, type, concept}`, which by its
  own comment cannot separate the NTB pair;
* is cached (`enrichment_summary`, migration 1035) and indexed into `search_doc`.

It is **not delivered**. Four wires are missing, and the asset screen consequently declares the field
and renders it empty on every column:

| gap | evidence |
|---|---|
| the value never loads | `ai_summary` absent from `_ANCHOR_COLUMNS` (`asset_detail.py:88`) |
| not a canonical stage | absent from `CANONICAL_STAGES` (`stage_report.py:50`) |
| no-client run misreports | absent from the skip loop (`ingest.py:2012`) |
| the agent cannot see it | no reference in `feature_assist.py` |

I reported this slice complete. It is not. That is the debt this plan repays.

---

## 2. The work

### 2.1 Load and display the value

Add `ai_summary` to `_ANCHOR_COLUMNS` (`asset_detail.py:88`). It is already in `_METADATA_FIELDS`,
which is exactly why the field renders and is always empty — the display slot was added without
checking the data reached it.

### 2.2 Report the stage honestly

Add `enrich_summary` to `CANONICAL_STAGES` (`stage_report.py:50`) and to the no-client skip loop
(`ingest.py:2012`), so a run without an LLM reports `skipped_no_client` rather than the stage simply
being absent from the report.

### 2.3 Reach the feature agent — the part with real depth

Adding a SQL column is not enough. `_FEATURE_COLUMN_DEFINITION_KEYS` is
`frozenset({"definition", "semantic_terms"})` (`enrich_llm.py:203`), so an `ai_summary` key on a
column descriptor is a genuinely-unknown key and **blocks the item**. The full path:

1. `feature_assist._candidate_columns` — select it;
2. the candidate dict — carry it;
3. the menu's definition-kind field set — permit it;
4. `_FEATURE_COLUMN_DEFINITION_KEYS` — allow it as definition-like prose, so it is
   sample-stripped and PII-scanned like `definition` rather than rejected;
5. relevance tokenisation — include it, or a column findable by its summary in search stays
   unfindable to the agent;
6. the feature-context input schema version — bump, since the item shape changes.

Miss any one and the field is stored, displayed, and invisible where it matters.

### 2.4 What "the agent receives it" can and cannot mean

`data_agent/` contains physical bindings, observation plans, analysis IR, SQL compilation and
execution. There is **no natural-language catalog-grounding planner** — the roadmap places that in a
later release. So this plan scopes agent grounding to the **feature agent** (`feature_assist.py`),
which exists, and makes no claim about the data agent.

---

## 3. Acceptance

Against a committed fixture, not prose:

* `cust_curr_ntb_flg` and `cust_prev_9mnth_ntb_flg` carry **distinct** summaries, and a small
  human-reviewed sample confirms they describe the *current* versus *nine-month-prior* state;
* both are findable by words appearing only in the summary;
* the asset screen shows a non-empty `ai_summary` with `llm/proposed` provenance;
* the source `definition` is byte-identical before and after — asserted, because "we did not touch
  the bank's text" is the property that makes this safe;
* a no-LLM run reports `enrich_summary: skipped_no_client`;
* the feature agent's menu carries the summary through egress without the item being blocked;
* an unchanged re-upload makes no provider call (cache hit).

---

## 4. Why `semantic_domain` is NOT in this plan

The Domain facet genuinely offers two choices for 237 columns, and that is worth fixing. It is not
worth fixing badly, and rev 2 underestimated it in five ways — each verified:

**Source ownership is not true today.** `build_graph` receives the classifier's `domains`
(`ingest.py:2242`) and writes them to `graph_node.domain` (`graph.py:243`); source evidence is
written later (`ingest.py:2419`). So rev 2's "one-line guard" would have skipped the `None` update —
`decision_id` is NULL — leaving the **AI** value standing. It converted *blank on conflict* into
*AI wins by writing first*. Making source ownership true means changing ingest ordering, retiring
legacy LLM-domain evidence, and restoring source projections: not a line, and not safe to do
speculatively.

**Inheritance is undefined.** Today `build_graph` physically writes the table default into every
column row, which is why the facet works. Evidence-only table defaults would leave every
non-overridden column NULL and produce a large `(none)` bucket. Where the default is stored, when it
is materialised, and how `origin: direct|inherited` is computed all need deciding.

**The payload does not fit.** `_MAX_COLUMN_PROFILES = 64` (`enrich_llm.py:1009`); CIB is 111 columns
and FTR 126. A full-context single call per table is rejected before it reaches the provider. This
needs a bounded contract or a two-stage shape.

**Publishing before grounding is the wrong order.** Rev 2 exposed the facet in Task 4 and improved
the classifier in Task 5, which permits shipping domains derived from table and column names alone.

**`proposed_new_domain` has nowhere to live.** Resolution is scalar; storing the object makes its
JSON the field value, and storing only `"other"` loses the proposal. It needs the shared candidate
contract or an honest deferral.

There is also a design question rev 2 did not raise: the classifier would receive `ai_summary` and
`concept`, both potentially LLM proposals. Two AI outputs agreeing is not corroboration, and the
payload must carry provenance so the model treats an AI value as advisory rather than as a second
independent witness.

**No live bug today.** `resolve_and_project` currently overwrites `build_graph`'s value with the
resolved source value, and there is no conflict because there is essentially no LLM domain evidence.
The hazard becomes live only when AI domain writing is enabled — which is what the domain plan does.
So the ownership fix belongs **with** that plan, tested together, not shipped ahead of it as
speculative work against a hazard nothing triggers.

The domain lane gets its own plan. It is a vertical slice — ownership, inheritance, bounded payload,
vocabulary, grounding, publication — not a task.

---

## 5. What rev 1 and rev 2 got wrong

**Rev 1** claimed source outranks LLM for `domain`. False: `FTR_GLOSSARY_PROFILE` puts `domain` in
`proposed_fields`, so both sides are `proposed`, `PREFER_CONFIRMED` returns `_CONFLICT`
(`field_authority.py:239`), display resolves `None`, and `domain` is absent from
`_SOURCE_AUTHORED_DISPLAY_COLUMNS` (`field_resolution.py:121`) — the value would have been NULLed.
Rev 1 also named `draft_domains` and `_glossary_rows`, neither of which exists, and mistook a
persistence gate for LLM targeting.

**Rev 2** fixed the safety claim and then made a subtler version of the same mistake: it reasoned
about the projection guard without reading the ingest ORDER, so its guard preserved the AI value.
It also treated `semantic_domain` as one task when its inheritance, payload bound, vocabulary
lifecycle and consumer wiring were each unspecified — the same underestimation that left
`ai_summary` four wires short of working while being reported complete.

The through-line across all three revisions: **every error came from reasoning about a mechanism
instead of reading it.** Rev 3 is smaller because the parts that were not read have been moved to
where they can be read first.
