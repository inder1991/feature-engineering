# Bucket-Filled Source Fields — Plan

> **Status:** proposed, not started. Written 2026-07-29 against verified code paths — every function
> and line reference below was read from the file, then re-checked after writing. That second pass
> caught three references that had drifted while `enrich.py` was edited earlier the same day, which
> is exactly how a plan comes to describe an API that no longer exists.

**Goal:** stop treating a source-declared value that is *the same for every column* as an answered
field, so AI enrichment runs where it is needed — without ever overruling a genuinely curated value.

---

## 1. The issue

Two enrichment stages report success and produce nothing, for the same reason.

**`enrich_definition` — expected: 0, resolved: 0.** It only targets columns with NO declared
definition. `enrich.py:1188`:

```python
return ({content_hash(r) for r in rows if not r.definition}
        - suppressed_definition_hashes(rows, glossary))
```

Every CIB column has a description, so nothing is ever targeted. But those descriptions are filled by
*bucket*, not by column:

| measure | value |
|---|---|
| rows | 111 |
| distinct descriptions | 47 |
| rows sharing a description with another row | **89 (80%)** |
| most-reused sentence | **12 columns** |

Those twelve include `cust_curr_ntb_flg` (new to bank now) and `cust_prev_9mnth_ntb_flg` (new to bank
nine months ago) — a before-and-after pair whose difference is the whole signal — described
identically.

**`enrich_domain` — 238 source-declared, 1 LLM.** `enrich.py:834` states the rule:

> *"THE AI FILLS A BLANK, IT NEVER CONTESTS A CURATED VALUE (R3's rule for definitions, applied to
> domain): a table or column whose glossary sidecar DECLARES a `data_domain` is NOT an AI target at
> either level."*

Enforced at `enrich.py:872` (`if rec is None or rec.domain: return None`). Every column declares one,
so the stage never runs. The result:

```
cib   Customer     111 columns
ftr   Compliance   126 columns
```

The Domain facet offers two real choices for a 237-column catalog.

### Why the rule is right and the outcome is wrong

The rule exists so an AI cannot overrule a bank's curated taxonomy. That is correct and must survive.

The gap is that it cannot distinguish **a curated value** from **a bucket label**. `Customer`
repeated 111 times is not a classification of 111 columns; it is one fact about the file. The system
sees a non-empty string and concludes the question is answered.

This is the fourth instance today of one shape — a stage runs, reports success, and produces nothing
because a gate upstream is closed. The others: `derive_bridge_candidates` with no callers, the
candidate ledger with no readers, `_entity_candidates` gated on a never-populated `graph_node.entity`.

---

## 2. The fix

**Measure specificity; do not assume it.** One deterministic pass, no LLM.

A declared value is *specific* when it distinguishes the column from its siblings, and *inherited*
when it does not. Counting is exact, instant, and works on any source forever without a list of
known-bad phrasings.

Deliberately narrow: this changes only **whether the AI is invited to write**. It never edits,
overrides, or lowers the authority of the source's value.

### 2.1 The measurement (new)

`src/featuregen/overlay/upload/field_specificity.py`

```python
#: A value shared by this many columns or more is a bucket label, not a column's own answer.
#: 2 is deliberate: in a metadata file, two columns with a byte-identical description are already
#: making the same statement about different things.
SHARED_THRESHOLD = 2

def inherited_values(values: Iterable[str]) -> frozenset[str]:
    """The values that describe a GROUP rather than a column — those appearing on 2+ columns.

    Blank is never returned: an absent value is already handled by the existing blank rule, and
    conflating the two would report "12 columns share a description" for 12 empty ones.
    """
    counts = Counter(v.strip() for v in values if v and v.strip())
    return frozenset(v for v, n in counts.items() if n >= SHARED_THRESHOLD)
```

Pure, no DB, no I/O — unit-testable in isolation.

### 2.2 Definitions (`_definition_targets`, `enrich.py:1178`)

```python
def _definition_targets(rows, glossary=None) -> set[str]:
    shared = inherited_values(r.definition for r in _glossary_rows(rows, glossary))
    return ({content_hash(r) for r in rows
             if not r.definition or r.definition.strip() in shared}
            - suppressed_definition_hashes(rows, glossary))
```

One clause added. The suppressed-blank exclusion (R5-3) is untouched: a sanitiser-blanked definition
stays withheld, because suppressed is not missing.

**Consequence:** ~89 CIB columns become AI targets instead of 0.

### 2.3 Domain (`column_ref_of` inside `draft_domains`, `enrich.py:872`)

```python
def column_ref_of(key: str):
    ...
    if rec is None or (rec.domain and rec.domain.strip() not in shared_domains):
        return None
```

`shared_domains` computed once per run from the glossary's column records. `Customer` appears 111
times, so it is inherited and every column becomes a target.

The table-level rule (`_declared_domain_tables`, `enrich.py:800`) is **unchanged** — a table's
declared domain is legitimately one value for one table, and the two-level design already models
column domain as an override of a table default.

### 2.4 Keep the source's value, honestly

Nothing is discarded or overwritten. The declared value stays in `field_evidence` at
`source/proposed` exactly as today. What changes is that the column also gets an AI proposal, and the
existing authority rules decide what displays — source still outranks LLM.

`domain` already carries `origin: "direct" | "inherited"` in the asset payload
(`asset_detail.py:195`). The same distinction now becomes *true of the data* rather than inferred
from the presence of column evidence.

### 2.5 Tell the uploader (the part that actually fixes the file)

A `parse`-stage detail, so it appears in the run report rather than only in a query:

```python
{"rows": 111, "shared_descriptions": 89, "largest_shared_group": 12,
 "distinct_domains": 1}
```

Everything above compensates for the file. This is the only part that improves it — the people who
produced the mapping cannot fix what nobody told them about.

---

## 3. Files touched

| File | Change |
|---|---|
| `overlay/upload/field_specificity.py` | **new** — `inherited_values`, `SHARED_THRESHOLD` |
| `overlay/upload/enrich.py` | `_definition_targets` (1178); `column_ref_of` in `draft_domains` (872) |
| `overlay/upload/ingest.py` | parse-stage detail carrying the specificity counts |
| `tests/.../test_field_specificity.py` | **new** — the measurement in isolation |
| `tests/.../test_bucket_filled_fields.py` | **new** — targeting behaviour, both fields |

No migration. No contract change. No API change.

---

## 4. Tests

**The measurement.** A value on 2+ columns is inherited; a unique one is not; blank is never
returned; whitespace variants are one value.

**Definitions.** A column whose description is shared by 12 becomes a target. A column with a unique
description does NOT (the curated case must stay untouched). A blank one still does. A
sanitiser-suppressed blank still does not.

**Domain.** With one distinct domain across 111 columns, every column becomes a target. With
genuinely varied domains, none does. The table-level rule is unaffected.

**Authority.** After enrichment, the source's declared value is still present at `source/proposed`
and still outranks the LLM's — asserted directly, because "we did not overrule the bank" is the
property that makes this safe.

**Mutation check.** Set `SHARED_THRESHOLD = 10_000` (nothing is ever inherited) and confirm the
targeting tests fail. A threshold that no longer bites must not pass silently.

---

## 5. Risks

**Cost.** ~89 definition targets and ~237 domain targets per upload where there were 0 and 1. Both
are cached (`enrichment_definition`, `enrichment_domain`), so it is a one-time charge per column, and
the 30-minute stage ceiling has headroom. Verify against the real files before calling it done.

**Threshold of 2 is a judgement.** Two columns legitimately sharing a description is possible. The
cost of being wrong is asymmetric and cheap: an unnecessary AI proposal that a human can ignore,
versus a column with no usable description at all. Revisit if it produces noise.

**Not fixed by this.** Concept assignment quality; a file whose descriptions are all unique but all
vacuous (needs judgement, not counting); and the fact that none of it applies to existing rows
without a re-upload.

---

## 6. Sequence

1. `field_specificity.py` + its unit tests — no other code touched, provably correct alone.
2. Wire into `_definition_targets` + tests. Verify against the real CIB file: 0 → ~89 targets.
3. Wire into `draft_domains` + tests. Verify: 1 → ~237 targets.
4. Parse-stage counts + test.
5. Full suite, then a real re-upload to confirm cost and quality against live data.

Steps 1–4 are independently committable. Step 5 is where the claim gets tested, and where every
previous "done" in this session turned out to be premature.
