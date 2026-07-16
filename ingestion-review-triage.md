# Ingestion review — triage (revised after meta-review)

**Reviewed:** 32 external findings vs `main` @ `5c20de7`, independently verified + adversarial refute pass. **Fix branch:** `fix/ingestion-review-hardening` (baseline `dbd2d40`).

> **Correction (after your meta-review):** my first summary under-scoped five findings and had wording errors. Re-verified against the baseline code, **#3, #4, #9, #12 are real** (I had deferred/refuted them) and **#19's fix needed strengthening**. All five are now in the fix scope. Corrected below. Net: **0 of 6 claimed Criticals still hold**, but the real-defect count rose from 24 to **28**.

## What changed from the first pass
| # | First call | Corrected | Why (verified) |
|---|---|---|---|
| 3 | deferred | **fix** | No source-level lock; `build_graph` deletes all source nodes then inserts only this upload's → concurrent same-source uploads clobber each other |
| 9 | refuted | **fix** | `_column_ref` hardcodes `public.` and drops schema → two schemas' `orders.id` collapse to one node (glossary *reading* keeps schema; the *graph* doesn't) |
| 4 | refuted | **fix** | `resolve_quarantine_row` adds rows with no brake re-eval / no snapshot (docstring admits it) → row-by-row brake bypass |
| 12 | by-design | **fix** | `find_join_path` left-joins the target; the sensitivity filter passes when it's absent → paths traverse not-loaded tables |
| 19 | weak fix | **stronger fix** | `redaction.py` documents NER name-detection as *deferred* → re-running the scanner can't catch names; fix must omit/redact free-text or require a NER redactor |
| 15 | (dropped from summary) | **fix** | composite/duplicate FK loss with no diagnostic — was always in scope, I forgot to list it |

---

## Being fixed (28)

### Concurrency & identity (re-verified)
| # | What it is | Sev | Fix |
|---|---|---|---|
| 3 | No same-source ingest serialization → concurrent uploads clobber the graph | High | source-scoped advisory lock at ingest entry |
| 9 | Graph node ref drops schema → two schemas collapse into one node | High | fail-closed: quarantine schema collisions |
| 4 | Quarantine resolution bypasses the large-change brake | Medium | re-eval brake on resolution |
| 1 | Raw vs normalized identity across stages → transient false drift, cache misses | Low | normalize once, key consistently |

### High / Medium — always-on base pipeline
| # | What it is | Sev | Fix |
|---|---|---|---|
| 14 | OpenMetadata default naming drops schema → distinct tables collapse | High | detect FQN collisions on fold |
| 2 | Dotted table/column names collide on the graph key / mis-parse lineage | Medium | quarantine dotted names |
| 11 | `column_joins` leaks a sensitive source column's join endpoints to a non-`pii_reader` caller | Medium | filter source sensitivity too |
| 12 | `find_join_path` traverses through not-loaded target tables | Medium | require both endpoints to exist |
| 16 | Whitespace in a source id → separate catalogs | Medium | strip ids at the API boundary |
| 17 | Readers accept structural corruption (dup headers, conflicting aliases) | Medium | reject, not last-write-wins |
| 7 | Quarantine resolution skips some re-checks (narrowed residual) | Medium | fix the real residual |
| 26 | Malformed glossary FQN `schema..column` silently coerced | Low–Med | reject empty FQN components |
| 5, 18, 29, 30, 27, 28, 10 | joins_to format; canonical under-validation; wide-table column bound; `staled` mislabel; opaque error mapping; FE/BE upload contract; join authority state | Low | as noted in the plan |

### Only with governed flags / the LLM provider on
| # | What it is | Sev | Fix |
|---|---|---|---|
| 19 | Glossary LLM egress hardcodes "clean"; scanner can't detect names | Low (security) | don't egress raw free-text absent a NER redactor |
| 20 | LLM egress audit rolls back with the upload transaction | Med–Low | write on a separate connection |
| 21, 22, 24 | provider-call budget undercount; unknown-concept cache poisoning; audit omits settings/usage | Low | as noted |

### Connector
| # | What it is | Sev | Fix |
|---|---|---|---|
| 15 | Composite/duplicate FKs silently dropped, no diagnostic | Low | surface a diagnostic (keep v1 skip) |
| 25, 32, 13 | "pending" count with no records; non-canonical snapshot hash; approval TOCTOU vs local baseline | Low | as noted |

---

## Deliberately NOT fixed (4)

| # | Why |
|---|---|
| 6 | Governed-join drift on a `joins_to` change — the stale relationship is **real**, but whether human-verified authority should override the source indefinitely is a **policy decision**. Flagged for you, not auto-fixed. |
| 23 | Wiring `find_llm_call` is a false premise (enrichment uses its own content cache); the residual concurrent duplicate-egress race is a real **Low** — documented, not fixed this pass. |
| 8 | **Refuted** — ingestion uses a source-scoped local catalog for drift; governed authority uses the global sentinel compensated by graph-backed referent validation (my earlier one-liner was imprecise but the finding doesn't stand). |
| 31 | Already fixed by the e2e `_drain_projection` change (`768c60e`). |

*Full per-finding evidence is the gitignored scratch `.superpowers/sdd/ingestion-review-triage.md`.*
