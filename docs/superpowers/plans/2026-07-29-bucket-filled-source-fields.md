# Source Metadata vs Platform Enrichment — Plan (rev 2)

> **Supersedes rev 1 entirely.** Rev 1 proposed treating any repeated source value as unanswered so
> the AI could compete with it. Architectural review found that would have **blanked** the field it
> aimed to enrich. Every claim below was verified against the code, and the three errors rev 1 made
> are recorded in §6 rather than quietly dropped.

**Goal:** make the catalog's metadata specific and searchable, without a platform value ever
overwriting, blanking, or outranking what the bank declared.

**Principle:** two lanes. The **source lane** is what the file said. The **enrichment lane** is what
the platform learned. They sit beside each other; they never compete for one slot.

---

## 1. What is actually wrong

### 1.1 The Domain facet offers two choices for 237 columns

```
cib   Customer     111 columns
ftr   Compliance   126 columns
```

Both source-declared. 238 source evidence rows, 1 LLM. `_write_domain_evidence`'s `column_ref_of`
(`enrich.py:872`) refuses to persist an override for any column whose sidecar declares a domain, and
every column declares one.

### 1.2 Descriptions are filled by bucket

47 distinct descriptions over 111 CIB columns; 89 rows (80%) share theirs; one sentence covers 12,
including the `cust_curr_ntb_flg` / `cust_prev_9mnth_ntb_flg` pair whose difference is the entire
signal.

### 1.3 `ai_summary` already solves 1.2 — and is only half-wired

Built earlier this session, and the safer design: it targets **every** column
(`_summary_targets`), writes to its **own** field, and receives the full `_concept_metadata`
payload — term_name, declared type, BIAN path, source attributes — where `draft_definitions` sends
only `{table, column, type, concept}`, which by its own code comment cannot separate the NTB pair.

Three wires are missing:

| gap | evidence |
|---|---|
| the value never loads | `ai_summary` absent from `_ANCHOR_COLUMNS` (`asset_detail.py:88`) |
| the stage is not canonical | `enrich_summary` absent from `CANONICAL_STAGES` (`stage_report.py:50`) |
| a no-client run misreports | absent from the skip loop (`ingest.py:2012`) |
| the agent cannot see it | no reference in `feature_assist.py` |

The asset screen declares the field and always renders it empty. **This was reported complete. It is
not.**

---

## 2. The hazard rev 1 would have triggered

Verified chain:

1. `FTR_GLOSSARY_PROFILE` (`source_profile.py`) puts `domain` in **`proposed_fields`**, not
   `attested_fields`. Source domain is `source/proposed`.
2. The LLM's is `llm/proposed` — **equal strength**.
3. `PREFER_CONFIRMED` (`field_authority.py:239`) returns `_CONFLICT` when the top strength holds more
   than one distinct value.
4. A display conflict resolves to `None`.
5. `_SOURCE_AUTHORED_DISPLAY_COLUMNS = frozenset({"unit", "currency"})`
   (`field_resolution.py:121`) — **`domain` is not in it**, yet `build_graph` co-authors
   `graph_node.domain`. So a `None` display projects unconditionally and **NULLs the column**.

`Customer` + AI `Payments` → **blank**. Rev 1's central safety claim was false, and its "authority"
test asserted a property the resolver cannot satisfy.

This hazard exists **today**, independent of any plan, for any future disagreement on `domain`.

---

## 3. The design

### 3.1 Guard first (independent of everything else)

Add `domain` to `_SOURCE_AUTHORED_DISPLAY_COLUMNS`. `build_graph` is a co-author, so a `None`
resolution means *"I have nothing to say"*, not *"there is nothing here"* — exactly the reasoning
already written above that constant for `unit`/`currency`. One line; removes a live hazard.

### 3.2 Two lanes, a pattern this codebase already runs

`definition` (source) / `ai_summary` (platform) is the shape. Repeat it:

| concern | source lane | enrichment lane |
|---|---|---|
| meaning | `definition` — source/attested | `ai_summary` — llm/proposed |
| grouping | `domain` — source/proposed | `semantic_domain` — llm/proposed |

Nothing competes. The source keeps its field, its text and its authority. The platform's value is
usable immediately — searchable, facetable, fed to the agents — without human approval, per the
standing rule that AI-proposed metadata is usable before review. Provenance stays visible.

### 3.3 Repetition is a DIAGNOSTIC, never a verdict

Rev 1's error. A domain is a *category*; repeating is its purpose. Fifty payment columns reading
`Payments` is the field working correctly.

So compute reuse statistics and **report** them. Change no authority and no targeting.

Scope per `(source, schema, table, field)` over **distinct logical columns** — not the whole upload,
and not raw CSV rows, so an unrelated table cannot reclassify this one and duplicate rows cannot
manufacture a group. Name it `reused_values`, not `inherited_values`: repetition is evidence of
possible genericness, not proof of it.

### 3.4 Ground the domain classifier, and fix its cache identity

It receives the table name and column names. From `amt`, `code`, `flag`, `dt` it cannot tell a
transaction amount from a balance. Send what already exists per column: term_name, sanitized
definition, `ai_summary`, concept, BIAN/FIBO path, declared type.

Keep the **one call per table** shape — a table default plus sparse overrides. Rev 1 wrongly claimed
~237 column classifications; `column_ref_of` runs *after* the call and only gates persistence.

The cache keys on source + table + sorted column names, so correcting `amt` from "Transaction
amount" to "Current account balance" serves the stale answer. Key on the full model input plus
prompt/schema/vocabulary versions.

### 3.5 A controlled, extendable domain vocabulary

Free-text output fragments the facet (`Customer`, `Customers`, `Customer Management`, `Party`…).
Offer a versioned list — `customer_identity`, `customer_lifecycle`, `customer_segmentation`,
`payments`, `accounts`, `balances`, `compliance`, `fraud_risk`, `credit_risk`, `merchant`,
`product`, `operations` — and allow `{"semantic_domain": "other", "proposed_new_domain": "…"}` so a
genuine gap surfaces instead of being silently invented.

---

## 4. Tasks, in order

**Task 1 — Guard `domain` against projection-wipe.** One line + a test that a `None` resolution
leaves a source-declared domain standing. *Ships alone.*

**Task 2 — Finish `ai_summary`.** Anchor query, `CANONICAL_STAGES`, the no-client skip loop,
feature-agent grounding. Acceptance: the two NTB columns show **distinct** summaries in asset
detail, are findable by them, and reach the agent. *Repays a delivery gap.*

**Task 3 — Reuse diagnostics.** Per-table statistics on the parse stage. Reports only; no authority
or targeting change. Note this IS externally observable via ingestion-run reporting — rev 1's "no API
change" was wrong.

**Task 4 — `semantic_domain` lane.** Migration, evidence under its own field name, read model, facet.
Source domain untouched throughout.

**Task 5 — Ground + re-key the classifier.** Full per-column context, controlled vocabulary, cache
identity over the real input.

---

## 5. Acceptance (Task 5 is where claims get tested)

- No source `definition` or `domain` is ever blanked — including on an AI disagreement.
- The NTB pair carries two distinct summaries, human-reviewed on a small sample.
- The enriched-domain facet holds useful categories beyond `Customer` and `Compliance`.
- Overrides are sparse: no fabricated per-column row where the table default applies.
- Duplicate input rows count once; an identical value in another table changes nothing here.
- Corrected metadata re-classifies; unchanged metadata makes no provider call.
- The data/feature agent receives `ai_summary` and `semantic_domain`.

---

## 6. What rev 1 got wrong

Recorded because the errors are instructive, not to be tidied away.

1. **"Source outranks LLM" — false for `domain`.** True for `definition` (attested), false for
   `domain` (proposed vs proposed). The plan would have blanked the field it set out to enrich.
2. **`draft_domains` does not exist.** The function is `_write_domain_evidence` (`enrich.py:814`). I
   verified every line number and never verified the enclosing function name — which then produced
   the cost error, since `column_ref_of` gates *persistence*, after the call, not targeting.
3. **Repetition treated as inheritance.** Sound reasoning for descriptions, transferred to a field
   where repetition is the point.
4. **`_glossary_rows` does not exist**; parse-stage reporting is owned by the upload route
   (`api/routes/uploads.py`), not `ingest.py`. Rev 1 was not literally buildable.
5. **Empirical claims unreproducible from the repo.** The real CIB/FTR files are untracked. The
   111/47/89/12 figures should be pinned by a committed deterministic script stating its
   normalization and grouping rules, or they cannot be re-derived by anyone else.

The common thread: rev 1 reasoned about authority from memory instead of reading
`source_profile.py`. Every other error followed from that one.
