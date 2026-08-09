# Handover — LLM enrichment reaches feature generation

**Branch:** `worktree-llm-enrichment-featuregen` · **Head:** `27d834b3` · **49 commits**
**Cut from:** `origin/main` `061c1440` · **Worktree:** `.claude/worktrees/llm-enrichment-featuregen`
**State:** complete, reviewed, tree clean. **Not pushed. Not merged. Never deployed. No LLM budget spent.**

---

## 1. What this branch does

Enrichment produced by an LLM at catalog-upload time was silently failing to reach the second LLM
call that proposes features. Five fields never arrived at all, and nothing enforced the relationship
between "what enrichment produces" and "what generation receives" — so the gap was invisible.

Nineteen tasks widen that path end to end, add the human-curated context that was also being
dropped, surface it on the asset-detail screen, and instrument a single live run so its results can
be interpreted.

**Everything was individually reviewed.** Every task had an implementer and a separate reviewer, with
fix rounds until clean. A whole-branch review then judged the 49 commits as one change, and its fix
wave landed as `27d834b3`.

---

## 2. Do these before merging

### 2.1 Rebase — low risk, but do it

`origin/main` has moved from `061c1440` to `890d7643`. **Two commits**, and I verified **neither
touches** `enrich_llm.py`, `feature_assist.py`, `semantic_context.py`, `enrich.py` or `ingest.py` —
the five files this branch concentrates in. Textual conflict risk is low.

Rebase or merge main in, then **re-run the suite**. It is green at 4905 passed / 11 skipped, but
against the old base.

```
uv run pytest tests/featuregen/overlay/upload/ -q     # 4905 passed, 11 skipped
uv run pytest tests/featuregen/intake/ -q             # 150 passed, 4 skipped
uv run pytest tests/featuregen/analysis/ -q           # 430 passed
cd frontend && npm test && npx tsc --noEmit
```

Two `ruff I001` errors in `intake/llm.py` and `test_feature_context_v4.py` are **pre-existing at the
base** — verified by linting the base blobs. Not this branch's debt.

### 2.2 Read the durable record

`docs/DEFERRED-WORK.md` **A.51–A.57** is the branch's risk record. It survived only because the
whole-branch review caught that `.superpowers/` is gitignored — the working ledger with ~40 findings
and four human decisions would have vanished on merge. A.51–A.57 carries what matters:

| Entry | What |
|---|---|
| A.51 | The four human decisions, with reasoning |
| A.52 | Non-glossary synonyms deferral (the ledger cited "A.7", which pointed at unrelated July work) |
| A.53 | **The 9b re-critique warning** + the `CONCEPT_REGISTRY_VERSION` no-bump decision and its trigger |
| A.54 | The concept critic has no call ceiling |
| A.55 | Whole-payload egress refusal |
| A.56 | F4 / F7 / F9 / F10 with fix shapes and triggers |
| A.57 | ~25 load-bearing minors triaged from ~40 |

`docs/runbooks/enrichment-run-reading-guide.md` is the operator's guide for the live run.

---

## 3. Then run — and read Q1 first

The single live catalog run **has never happened**. The human approved it, conditional on the
observability task landing. It has landed.

**Before reading any results, read Q1 of the runbook.** Task 9b changed the concept registry, which
moves `concept_critic._registry_fingerprint()` — so the first run after merge **re-critiques every
identifier column**. A `REVISED` verdict changes a column's `namespace`, which is the join-candidacy
axis; a `REFUTED` one costs the column its bridge candidacy.

Without checking which verdicts moved, a different set of features coming out is **uninterpretable**
— new enrichment and re-rolled verdicts look identical in the output. This is why the observability
task existed.

**Verified narrower than first feared:** only the *critic* re-rolls. The classifier cache is
unaffected — `_vocab_fingerprint()` hashes `{name, group, hint}` and contains no `is_a`, confirmed by
a computed set-diff base↔head.

---

## 4. What must be fixed, in priority order

> **Readiness wave, 2026-08-09 — most of this section is now DONE.** Working toward "ingestion ready
> for testing" reordered the ledger: several items below deferred their fix until the first live run
> could measure them, but an unbounded loop and an unreadable spend account are exactly what make the
> run unsafe to start. Fixed: **F4, F7, F9, F10, A.54, A.55**, the failure-path token accounting, and
> the `FEATUREGEN_LLM_TIMEOUT` code default. Corrected rather than fixed: **`fibo_path` is NOT "one
> line"** — it needed a migration, and it is now CLOSED too (1058). The budget-fixture blindness is
> CLOSED (§4.2). **The only thing still open is the ~4502s worst-case lock hold above**, which needs
> the ability to interrupt a call already in flight. The durable account is **A.58**.

### 4.1 Before the live run

**~~The concept critic is uncapped~~ BOUNDED 2026-08-09 (A.54).** `critique_concept_batch` now takes
the concept stage's own `CallLedger` and `deadline_s`; `_drive` — the single dispatch point, so the
critique AND the revise both count — charges the ledger, and an item either bound skipped resolves
to ABSTAINED with a `skipped_reason`, with `not_attempted`/`stopped_by` on the stage report.
Fail-OPEN by decision: the classifier's concept stands un-refuted rather than being evicted for a
budget reason. See A.54 for the full reasoning and the `REVISED if revised else REFUTED` trap.

**A single logical call can still run ~2702s against an 1800s stage deadline that cannot interrupt it
mid-flight.** Worst-case advisory-lock hold ~4502s. Bounded, tested and documented; NOT fixed — the
deadline is checked before issuing each item, never mid-call. **This remains the standing risk for
the run.**

**~~F4 has a live-run deadline if Pass B is on.~~ FIXED 2026-08-09.** Pass B *is* on
(`OVERLAY_TABLE_SYNTH: "1"`), so the deadline had fired. The audit A.56 asked for found **three**
unscanned uploader-authored keys, not one: `term_type`, `domain` and `process_path`, all emitted by
`table_synth._descriptor`, whose docstring called them "bounded structural tokens (200 cap)" — the
misreading that let them ride. `_redact_free_text_meta` now routes every free-text descriptor key
through its declared grade (prose, not definition — see A.56 for why that choice is load-bearing)
and fails closed on an unclassified one, mirroring the top-level D10 gate. Six tests; suite 4911.

### 4.2 Before trusting the budget pins

**~~The budget fixture is blind to 8 of the 11 fields the branch added.~~ CLOSED 2026-08-09.** A
`saturated_catalogs` fixture now populates every added field through its REAL writer, and both pairs
are pinned:

| | payload | floor |
|---|---|---|
| sparse (`wide_catalogs`) | **268_902** | **223_930** |
| **saturated** | **405_863** | **360_891** |

The blindness was not theoretical — F7's `proposed_authority` grew the payload and the pinned test
passed **completely unchanged**, because the field it sits beside was empty in the fixture.

**Two corrections to what this section said.** The "measured if all populate" figures (313_197 /
268_225) were BOTH LOW — the real saturated pair is 29% and 34% higher — and, like `171_347`, they
were never pinned by anything. And two fields stay structurally low by DESIGN, not omission:
adjudication is capped at 12 columns per run (`adjudication_bounds()`), and
`relationships`/`outbound_cardinality` needs cross-catalog link rows the fixture does not stand up
(asserted as 0 rather than left unstated).

Saturated is **27.1% of budget, 3.7× under**; the rungs sit at ~876 and ~985 columns. **A refusal is
not reachable on any realistic catalog.**

**~~Stale prose in the dangerous direction~~ CORRECTED 2026-08-09.** `feature_assist.py` now carries
the pinned `259_405` / `~1_095 B/col` / `214_433`, with the two rungs re-derived (first shed ~1_370
mandatory columns, refusal ~1_658) and a pointer to
`test_the_floor_rose_by_exactly_what_the_payload_rose_by`, which asserts the pair — so the comment
cannot drift from the measurement again without a red test.

**One correction to §4.2 itself:** `171_347 B` for the 144-column shape appears **only in this
handover**. No test pins it, and the budget fixture is the 237-column pair — there is no 144-column
measurement in the repo. It was NOT copied into the source comment; a derived `~158_000 B`
(144 × ~1_095) is recorded instead, explicitly labelled derived-not-measured. Restating an
unreproducible number as fact is exactly how `203_629` got there.

### 4.3 Recorded, no deadline

- **~~F7~~ FIXED 2026-08-09** — `proposed_value` misattribution on the LLM seam. `proposed_value` is always the LLM's row,
  but `semantic_authority` names the *strongest* producer, so the payload can show the model's guess
  under another producer's name. Task 9 fixed exactly this on the UI seam and left the LLM seam.
- **~~F9~~ FIXED 2026-08-09** — `_fallback` discarded acceptor reasons, so 9c's per-item rejection accounting is closed on
  the batch path only.
- **~~F10~~ FIXED 2026-08-09** — the synonyms ask was widened ~5× against a 1000-char cap the model was never told; the
  `maxLength` is stripped from the wire and validated only on the response, so an over-run fails the
  whole chunk.
- **The column's `semantic_terms` still renders as a space-joined blob** under a single authority chip
  in "What the platform resolved", naming one producer as author of text with two. **Do not apply the
  obvious one-line filter** — the glossary's own synonyms are persisted nowhere else, so filtering
  would make the bank's own vocabulary invisible. Correct sequence: persist them attributably, then
  filter, then add `semantic_terms` to `asset_detail._METADATA_FIELDS` so it can ever carry a
  confirmed state.

---

## 5. What still does not arrive

Against the plan's goal — *"every field the LLM produces during ingestion reaches feature generation
and the asset-detail UI"*:

1. **On a glossary-less upload the drafted synonyms reach nothing.** `draft_synonyms` runs over every
   column; the writer is gated, the projection unreachable, the ride-along `not_applicable`. Task 4c
   fixed the *label* (the stage now reports `partial`, not a false `succeeded`) — not the waste. The
   clearest miss. **Does not affect the CIB catalog**, which is a glossary upload.
2. ~~`fibo_path`~~ **CLOSED 2026-08-09 (migration 1058).** The handover called this "one line to
   close" — it was six, because the column did not exist. `fibo_path` was in NO migration, not in
   `field_policies` and not in `field_resolution`; 1051 gave its two sidecar siblings their
   projection columns and skipped it, so its SOURCE evidence had nowhere to land. It was invisible
   precisely because those siblings worked. Closed with a migration, a policy, a projection, the
   `semantic_context` anchor + bundle emission and the prose egress grade — and the D7 reservation
   table appended in the same change (pool note now 1059+), which is the step A.22's 1032/1033
   double-allocation exists to enforce. **`UNCARRIED_GAPS` is now empty.**
3. `authority_role` / `temporal_storage_model` / `business_context` reach generation only under
   `FEATUREGEN_DATASET_PROFILES`, shipped `"0"` — so in the default config, nowhere.
4. The catalog narrative reaches Pass B and the feature seam, but not Pass A or adjudication.
5. `grounding` is returned and serialised but **no UI renders it**.
6. Observability gaps declared as scope calls: truncation old→new `max_tokens`; feature-context
   payload bytes / column count / trim level; term→column match attribution. Note `_failed()`
   hard-codes `cost_metadata={}`, so **the run's token spend under-counts by exactly its failures** —
   which is what this run is most likely to produce.

---

## 6. Behaviour that changes on merge, not on a flag flip

Wider than the plan's stated four. Tasks 1–3 are unconditional driver changes; the sharpest is that
`FEATUREGEN_LLM_TIMEOUT` still defaults to **60s** in code and only the kind ConfigMap raises it to
300 — so on the default, a slow call now fails its chunk outright instead of retrying. Add 4b, 4c
(plus a permanent user-visible "enrichment partial" WARN on glossary-less uploads), 6d, 9b, 2b and 9c.

`OVERLAY_ENRICH_MAX_PROVIDER_CALLS` defaulted to 32 in code while the deployment ships 512, and Task
4b doubled the definition stage's chunk count — the fix wave addressed this; verify it after rebase.

---

## 7. Two hard-won lessons worth carrying

**Ask what CONSUMES a value, not whether it is correct.** Every defect across nineteen tasks came from
that question. A truncation fix that broke an audit gate two modules away. A cap raise that was inert
because a producer cap still bound. A gate relaxation that silently collapsed three synonyms into one.
A stage reporting `succeeded` while storing nothing.

**Test the configuration nobody listed.** Two defects were introduced by the fix to the previous one,
both found by exercising a state nobody had considered — an ordinary business sentence, and the
default flag-off deployment. One directive sentence was wrong in a fourth state after three separate
walks of "every state".

---

## 8. Ground truth

- Working log (gitignored, do not rely on it):
  `.superpowers/sdd/2026-08-06-llm-enrichment-reaches-feature-generation/progress.md`
- Budget analysis: `.../budget-coherence-report.md`
- Per-task reports: `.../task-*-report.md`
- The plan: `docs/superpowers/plans/2026-08-06-llm-enrichment-reaches-feature-generation.md`
- **The durable record: `docs/DEFERRED-WORK.md` A.51–A.57**
- **The operator's guide: `docs/runbooks/enrichment-run-reading-guide.md`**
