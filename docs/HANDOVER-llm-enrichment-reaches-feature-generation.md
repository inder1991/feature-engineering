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

### 4.1 Before the live run

**The concept critic is uncapped and, until the fix wave, was unmeasured.** `critique_concept_batch`
(`concept_critic.py:472-491`) is a plain per-item loop with **no call ceiling and no deadline** —
`OVERLAY_ENRICH_MAX_PROVIDER_CALLS` does not bound it. On a 144-column catalog that is ~70–100
sequential calls at up to 300s each, on exactly the run 9b forces. The fix wave added `started_at` so
it now has a duration, and a runbook query for its cost. **The ceiling itself is still missing** —
filed as A.54 with the fail-open reasoning.

**A single logical call can run ~2702s against an 1800s stage deadline that cannot interrupt it
mid-flight.** Worst-case advisory-lock hold ~4502s. Bounded, tested and documented; not fixed. This is
the standing risk for the run.

**F4 has a live-run deadline if Pass B is on.** `domain` is redacted on the feature seam (the human's
2026-08-07 decision) but **not** on the Pass-B `column_profiles` seam, where `_redact_free_text_meta`
re-sanitises only `business_definition`. Compounding: Task 4b raised `_MAX_COLUMN_PROFILES` 64 → 512,
which is Pass B's narrow/wide router — so an unredacted uploader field went from ≤64 summarised values
to ≤512 verbatim values per call. Recorded in A.56.

### 4.2 Before trusting the budget pins

**The budget fixture is blind to 8 of the 11 fields the branch added.** `confidence_band`,
`concept_alternatives`, `proposed_value`, `outbound_cardinality`, `sub_domain`, `table_role` and
`event_or_snapshot` all measure **exactly 0**; `related_terms` appears on 1 of 237 columns. So the
pinned `+17_914` rise is essentially two fields.

Nothing breaks — measured headroom is large — but these can populate in production and move the floor
with **no test noticing**. Measured if all populate: payload **313_197**, floor **268_225**.

**Measured at `f1eb1e1b`:** mandatory **259_405** B (17.3% of the 1_500_000 budget), un-sheddable floor
**214_433** B. The user's 144-column shape ≈ **171_347 B, 8.8× under budget**. Refusal needs ~1_657
mandatory columns. **A refusal is not reachable on any realistic catalog.**

**Stale prose in the dangerous direction:** `feature_assist.py:1078-1114` records `241_491`,
`~1_019 B/col`, `203_629` and `~1_470 columns` — all understating cost or overstating headroom, and
`203_629` matches no rung of any ladder and was never true.

### 4.3 Recorded, no deadline

- **F7** — `proposed_value` misattribution on the LLM seam. `proposed_value` is always the LLM's row,
  but `semantic_authority` names the *strongest* producer, so the payload can show the model's guess
  under another producer's name. Task 9 fixed exactly this on the UI seam and left the LLM seam.
- **F9** — `_fallback` discards acceptor reasons, so 9c's per-item rejection accounting is closed on
  the batch path only.
- **F10** — the synonyms ask was widened ~5× against a 1000-char cap the model is never told; the
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
2. `fibo_path` — the sole remaining `UNCARRIED_GAPS` entry; one line to close.
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
