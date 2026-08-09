"""Rollout knobs for batched enrichment (spec C10). Budgets stay conservative; batch stays a
per-task env override — the kill switch. Pass A enrichment (concept/definition/domain) now DEFAULTS
to batch: a wide file was 1 LLM call per column (126 cols -> 126 sync round-trips) all under the
same-source advisory lock (ingest.py); batch cuts concept to ~ceil(cols/40) calls, shrinking the
lock hold (#4). Set ``OVERLAY_ENRICH_<TASK>_MODE=single`` to fall back to the per-item path.
``table_synth`` keeps the ``single`` generic default (Pass B is batch-only and never consults
``mode`` — its default is inert, so leaving it single avoids re-pinning the Pass B switch tests)."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Per-task default execution mode. Pass A stages batch by default (#4); table_synth's entry is inert
# (synthesize_tables never reads mode()) and stays single so the config-namespace tests are unmoved.
_DEFAULT_MODE = {"concept": "batch", "definition": "batch", "domain": "batch",
                 "synonyms": "batch", "unit": "batch", "summary": "batch",
                 "table_synth": "single"}
# MF-8a — conservative ISOLATION boundaries, not throughput maxima. The old 40/12/20/8 were picked
# for throughput with NO accuracy evidence (the hermetic gold gate drives a scripted FakeLLM that
# echoes each column's expected answer, so it measures the harness, not the provider, and compares no
# batch sizes / no cross-item contamination). Until the key-gated real-provider sweep
# (tests/eval/test_batch_size_sweep.py) produces evidence a higher ceiling holds accuracy, keep these
# small so cross-item contamination has less room. The env override still raises a ceiling per task.
# `summary` sits with the other prose tasks at 8: it runs over EVERY column (not just the blank
# ones), so it is the widest fan-out of any task, and a small chunk keeps cross-item contamination —
# one column's summary borrowing another's facts — to the same bound the siblings hold.
#
# ── `definition` 8 -> 4 (2026-08-06) ─────────────────────────────────────────────────────────────
#
# LOWERED, which is the only direction this map may move without accuracy evidence, and the binding
# reason is the OUTPUT-token ceiling rather than contamination. Every other bound in this task's
# work was about INPUT (`chunk_items` packs on `estimate_tokens`, which measures the request); this
# one is about the RESPONSE, which nothing in `enrich_config` measures.
#
# `MAX_DEFINITION_LEN` went 600 -> 32_000, so ONE drafted definition may now be ~8_000 output tokens
# (the estimator's own 4-chars-per-token unit; real tokenisation of English prose is slightly worse,
# so this UNDERSTATES). Against the deployed `FEATUREGEN_LLM_MAX_TOKENS = 32000` and the driver's
# `_MAX_TOKENS_CEILING = 64_000` / `_TRUNCATION_ESCALATION = 2.0`:
#
#   items   chunk output at the cap   vs 32_000 initial   after ONE escalation to 64_000
#   8       ~64_000 tok               truncates           EXACTLY AT the ceiling -> unservable
#   4       ~32_000 tok               marginal            fits with 2x headroom
#
# At 8 the escalated ceiling IS the wall: a chunk of full-length definitions cannot be served even
# after the retry budget is spent, and the chunk fails. At 4 the escalation actually rescues it. So
# this is not "8 costs more calls than 4" — it is "8 has a shape it can never complete".
#
# HONEST SCOPE: that is the worst case, not the common one. The prompt asks for a ONE-LINE
# definition and the widest real value measured is 960 chars, so a realistic chunk is ~2_000 output
# tokens at either setting and never escalates. The change buys the tail, not the median.
#
# Contamination points the same way (an item is now up to 53x larger, so a chunk of 8 is a far wider
# surface than when this was set), and the human asked for it. Three independent grounds agree.
#
# `summary` was checked against the same arithmetic and deliberately LEFT at 8: its accept bound is
# `enrich._MAX_SUMMARY_LEN` = 1000 chars = ~250 output tokens, so 8 items is ~2_000 tokens — 6% of
# the 32_000 ceiling. It has no output-token problem to fix.
#
# A FAILURE-MODE CONSEQUENCE, decided rather than discovered: at 4 items the ADAPTIVE-SPLIT tier of
# `enrich_batch`'s degradation ladder becomes unreachable for this stage. The tier fires on
# `len(unresolved) > b.min_split` and `min_split` is 4, so a definition chunk can never satisfy it;
# a chunk failing past its batch retries now goes straight to per-item single fallback.
#
# PER-STAGE LOWERING OF `min_split` WAS NEVER AN OPTION. `budget(short)` takes `short` and NEVER
# READS IT — every field is a global env lookup — so `OVERLAY_ENRICH_MIN_SPLIT` moves the tier for
# EVERY stage or none. (An earlier revision of this note said min_split was "deliberately not
# lowered for this stage", which implied a choice that does not exist without new plumbing.) The
# decision is therefore to accept the tier's loss here, and it is the right one:
#   * the split tier exists to avoid paying PER-ITEM cost on a LARGE chunk — splitting 20 concept
#     items to 10+10 to 5+5 is far cheaper than 20 single calls. At 4 items there is no such saving.
#   * single fallback resolves each of the 4 items INDEPENDENTLY, which is strictly better isolation
#     than a 2+2 re-batch, where one poisoned item can still take its partner down.
#   * the size-driven failure the tier existed to recover from is DESIGNED OUT by moving to 4: an
#     over-long chunk is exactly what 4 items prevents.
#
# BUT THE FAILURE LADDER IS GENUINELY SHORTER, and that is the honest cost. An 8-item chunk failing
# `keep_threshold` used to split into 4+4, each half re-entering `process` with a FRESH
# `max_batch_attempts` — a recovery rung that cost ZERO single-fallback budget. A 4-item chunk now
# spends that budget immediately, so `left_uncached` arrives EARLIER on a run with several failing
# chunks. Worth stating: the tier was not only a cost optimisation.
#
# NOT a compensating gain, and previously miscounted here: `max_single_fallback` (8) is a PER-RUN
# cap, and halving `max_items` DOUBLES the chunk count, so 8 fallback calls rescue exactly 8 items
# either way. That is a wash, not a saving. The real offsetting fact is the worst-case CALL count:
# two failing 4-item chunks cost ~6 batch + 8 fallback, against ~3 batch + 6 split-batch + 8
# fallback for one failing 8-item chunk.
#
# Pinned by `test_enrich_batch.py::test_the_split_tier_is_intentionally_unreachable_for_definitions`,
# so if `max_items` is ever raised back above `min_split` the tier silently reactivates and that
# test is what says so.
_DEFAULT_MAX_ITEMS = {"concept": 20, "definition": 4, "domain": 8, "synonyms": 8, "unit": 8,
                      "summary": 8, "table_synth": 4}

# ── RE-BUDGET, joint Task 4 (measured, not guessed) ──────────────────────────────────────────────
#
# The Pass-A payloads became the `semantic_context` purpose adapters, so every item carries more
# bytes. "Preserve the batch bounds" and "triple the item bytes" cannot both be true: with a fixed
# token cap a bigger item means fewer items per chunk, which means MORE provider calls against an
# unchanged 32-call / 240s ceiling — i.e. SILENT truncation, reported as a normal partial run.
#
# Measured on the 126-column FTR fixture (`fixtures/ftr_sample_synthetic.csv`, one wide table),
# `estimate_tokens` per item, old payload -> new payload:
#
#   task                       median      max      chunk cost at its max_items
#   concept                    85 -> 373   194 -> 538   20 x 538  =  10,760   (was 20 x 194 = 3,880)
#   definition/synonyms/unit    23 ->  93    25 -> 270    8 x 270  =   2,160   (was  8 x  25 =   200)
#   summary                    92 -> 108   201 -> 272    8 x 272  =   2,176   (was  8 x 201 = 1,608)
#   domain (per TABLE item)   428 -> 2993  428 -> 2993   8 x 2993 =  23,944   (was  8 x 428 = 3,424)
#
# Two bounds actually moved; the rest are recorded as MEASURED-SUFFICIENT rather than churned:
#
# * `concept` 14000 -> 24000. The measured max chunk (10,760) still fits 14000, but only because
#   this fixture's roster entries are mostly bare names: on a RE-upload every sibling carries a
#   resolved concept + party role. RE-MEASURED (review correction — the original "~750 tok/item,
#   ~60% headroom" here was wrong): a golden classifier payload carrying a FULL 40-entry roster of
#   resolved entries (column name + concept + party role) is 971 tok/item on the fixture's own names
#   (mean 9.3 chars) and 1,105-1,144 tok/item on longer bank-style names — 20 x that is
#   19,420-22,880 against the 24000 cap, i.e. 81-95% of it USED and ~5-19% headroom, not 60%. The
#   bound is kept: crossing it is not a cliff (see the chunking note below), and the headroom that
#   remains is real.
# * `domain` 8000 -> 26000. This one is not marginal: a domain ITEM is a whole TABLE, and it now
#   carries that table's complete column roster (name + resolved concept + party role) because the
#   D13.2 per-column sub-domain question is unanswerable from a bare name list. 8 x 2,993 = 23,944
#   against an 8000 cap would have cut chunks from 8 tables to 2 — a 4x call-count increase on the
#   domain stage, which on a 100-table catalog crosses the 32-call ceiling and truncates.
#   RE-MEASURED at the same resolved-roster fill: the 127-column fixture table is 3,019 tok/item
#   (3,146 on the review's run), against a break-even of 26000/8 = 3,250 tok/item — 8 x 3,019 =
#   24,152, i.e. ~7% headroom (~3% on the review's number). This bound is genuinely tight, and it is
#   still kept deliberately: `_DEFAULT_MAX_ITEMS` is a CONTAMINATION boundary (MF-8a) that must not
#   be traded for bytes, and a wider table simply chunks smaller (below).
#
# WHAT CROSSING A TOKEN BOUND ACTUALLY COSTS (why none of these is "unsafe"): `chunk_items` packs by
# BOTH item count and estimated tokens, and an item that alone exceeds the token budget still forms
# its own chunk — nothing is ever dropped for size. So an under-budgeted bound degrades
# PROPORTIONALLY (fewer items per chunk -> more provider calls), never into a lost item. The real
# backstop is `budget(short).max_provider_calls` (code default 32; the kind deployment ships 512 —
# see the zero-truncation note below), which — since the Pass-B critic fix — counts every PHYSICAL
# call of the run, nested seams included. It is PER `run_batched` INVOCATION, i.e. per stage, not
# per upload (`enrich_batch.py` builds the ledger inside `run_batched`).
#
# NOT BOUNDED, and deliberately recorded here: the domain item's `columns` list has NO COUNT CAP in
# `enrich_llm._item_shape_ok` (it falls to the generic list-of-strings branch; only `column_profiles`
# / `chunk_summaries` / `column_roster` / `source_attributes` carry count caps). Per-VALUE length is
# capped at `_MAX_LEN_DEFAULT` (1000 since the zero-truncation raise), so a pathological table
# bounds each name but not how many ride: a 5,000-column
# table would egress as one ~150K-token item in its own chunk. No such table exists in any catalog
# on this platform today; capping it is a deliberate egress-shape change (an over-count item would
# be EXCLUDED + audited), not a comment fix, so it is documented rather than silently done here.
# * definition/synonyms/unit (2,160 = 27% of 8000) and summary (2,176 = 16% of 14000) keep their
#   bounds: the margin is measured, not assumed.
#
# `_DEFAULT_MAX_ITEMS` was deliberately UNCHANGED by THIS raise. Those are cross-item CONTAMINATION
# isolation boundaries (MF-8a above), not size limits; shrinking them to pay for bigger payloads
# would trade an accuracy control for a byte budget and multiply the call count at the same time.
# (`definition` was LOWERED 8 -> 4 later, for an OUTPUT-token reason no budget in this file models —
# see the note on `_DEFAULT_MAX_ITEMS` itself. Lowering moves WITH the MF-8a rationale; the rule
# these paragraphs state is that nothing here may be RAISED to pay for bytes.)
#
# NOTE on `estimate_tokens` and `shared_metadata` (the honest accounting): the estimator measures
# ITEM metadata only. That is CORRECT for the one shared block that exists — the ~276-concept
# classification vocabulary — because it is dispatched as a `cache_control` shared prefix
# (`cacheable_metadata_keys`), sent once and reused by chunks 2..N rather than re-billed per chunk;
# folding it into a per-chunk INPUT budget would make every chunk look ~23K tokens over and collapse
# every batch to one item. The sibling roster and table context therefore ride PER-ITEM metadata by
# design, where the estimator sees them honestly — pinned by a test, so nobody can "optimize" the
# roster into `shared_metadata` and make the budget blind to it again.
#
# ── ZERO-TRUNCATION RAISE (2026-08-06) ───────────────────────────────────────────────────────────
#
# Every prose and item cap on the enrichment path went up so that nothing the platform knows is cut
# on the way to the model (`enrich_llm.MAX_DEFINITION_LEN` 600 -> 4000 — and 4000 -> 32_000 in the
# second raise below, `_MAX_LEN_DEFAULT` 200 ->
# 1000, `_MAX_COLUMN_PROFILES` 64 -> 512, `semantic_context.ADAPTER_LIST_LIMIT` 40 -> 256,
# `NEIGHBOUR_LIMIT` 64 -> 512). Read the block above for WHY that forces this map up with it: a
# bigger item against a fixed token cap means fewer items per chunk, which means more provider
# calls, which at a tight ceiling means SILENT truncation rather than a bigger bill.
#
# The roster is the multiplier that matters — it went from 40 entries to 256, so a classifier item
# grew roughly 6x (the measured 971-1,144 tok/item above scales with roster fill). These bounds are
# raised well past that growth rather than trimmed to it: they sit far below Opus's 1M-token window,
# and the binding constraint on a call is COST, not the model. Deliberately generous, because the
# failure mode of a tight bound here is a shattered chunking and a truncated stage, while the
# failure mode of a loose one is a chunk that packs its full `_DEFAULT_MAX_ITEMS`.
#
# `_DEFAULT_MAX_ITEMS` was not RAISED here either, for the reason stated above: those are
# contamination boundaries, not size limits. Raising the token budgets is exactly how you avoid
# having to touch them. (`definition` was later LOWERED to 4 — a different question, decided by the
# response ceiling rather than by this input budget.) Packing at the new caps is pinned by
# `test_enrichment_context_wiring.py::test_the_concept_stage_still_packs_multiple_items_per_chunk_at_the_new_caps`.
#
# ── MAX_DEFINITION_LEN 4000 -> 32_000 (2026-08-06, second raise) ──────────────────────────────────
#
# `enrich_llm.MAX_DEFINITION_LEN` went 4000 -> 32_000, and it is the per-value egress cap on
# `business_definition`. Every Pass-A stage whose ITEM metadata carries that key therefore gained up
# to 8_000 estimated tokens PER ITEM, which is a chunking change, not a prose change.
#
# THE UNIT OF THE DERIVATION IS EXACT, not an approximation. `enrich_batch.estimate_tokens` IS
# `len(json.dumps(metadata)) // 4` and `chunk_items` packs on nothing else, so "4 chars per token" is
# the packer's own definition, and a cap of 32_000 chars is 8_000 estimated tokens by construction.
# No real tokenizer is involved on this path.
#
# MEASURED against the REAL builders (`_drafting_payload`, `summary_payload`, `_classifier_payload`)
# with a definition AT the 32_000 cap, a fully-populated FTR-shaped sidecar (term/domain/BIAN/FIBO/
# process path/synonyms/related terms + 40 `source_attributes`) and, for the classifier, a saturated
# 199-entry roster at RESOLVED fill — i.e. the widest table ingestion admits
# (`MAX_COLUMNS_PER_TABLE`), every sibling carrying a concept and a party role:
#
#   stage                  tok/item   of which definition   chunk cost at max_items   packed BEFORE
#   definition/synonyms/unit  8_308        8_000               8 x  8_308 =  66_464    7 of 8  (was 8)
#   summary                   8_322        8_000               8 x  8_322 =  66_576    8 of 8
#   concept                  13_230        8_000              20 x 13_230 = 264_600   15 of 20 (was 20)
#   domain                    6_355            0               8 x  6_355 =  50_840    8 of 8
#
# So the raise cost the three `_drafting_payload` stages ~14% more provider calls and the concept
# stage ~33% more — a PROPORTIONAL degradation (see the chunking note above), never a lost item, but
# a real move toward the `max_provider_calls` ceiling on exactly the stages that fan out per column.
# `domain` is untouched because a domain item is a whole TABLE and carries no `business_definition`
# at all; its 200_000 stands as MEASURED-SUFFICIENT and is not churned.
#
# THE TWO RAISES, derived rather than guessed:
#
# * definition / synonyms / unit / summary 60_000 (100_000 for summary) -> 200_000. All four are the
#   same item shape (`summary_payload` is `_drafting_payload`'s superset — 322 tok vs 308 — which is
#   why they get ONE number rather than three; leaving summary at 100_000 would have given the
#   LARGER item the SMALLER budget). Required = `max_items` x (8_000 + other). At the measured
#   `other` (308-322 tok) that is 66_464-66_576 at 8 items, i.e. 33% of 200_000.
#
#   `max_items` IS NO LONGER 8 FOR ALL FOUR (2026-08-06): `definition` dropped to 4 for an
#   OUTPUT-token reason this INPUT budget does not model — see the `_DEFAULT_MAX_ITEMS` note above.
#   So 200_000 is now held by `synonyms` / `unit` / `summary` at 8, and `definition` sits at half
#   that requirement inside the same number. The shared budget is deliberately NOT re-cut to 4
#   items: an input budget larger than the requirement costs nothing (packing is bound by
#   `max_items` first), while a per-stage split would be a fourth number to keep in step for no
#   gain. What the budget must hold is asserted by measurement, not by this paragraph — see
#   `test_the_drafting_stages_still_pack_a_full_chunk_at_the_definition_cap`.
#
#   RE-DERIVED (2026-08-06, Task 4b review): the paragraph that used to sit here computed the
#   pathological fill at `ftr_adapter._MAX_SOURCE_ATTRIBUTES` = 40 and concluded "8 x 22_000 =
#   176_000, i.e. 88% of 200_000 — the bound therefore holds BOTH the measured and the pathological
#   item at the full contamination bound of 8". That producer cap was raised to 256 in the SAME
#   round, so the conclusion was false the moment it was written. The real arithmetic:
#
#     source_attribute fill          tok/item   chunk cost (x8)    packed
#     17 x 1000 (the REALISTIC max — the FTR export has 17 headers in total)   12_392    99_136   8/8
#     256 x 1000 (the new theoretical maximum)                                 72_381   579_048   2/8
#
#   (Both rows are at `max_items` = 8, which is still `synonyms` / `unit` / `summary`. `definition`
#   at 4 is strictly inside them: 4 x 12_392 = 49_568, and the degenerate fill packs 2 of 4.)
#
#   So 200_000 holds the measured item and the realistic-maximum item at the full contamination
#   bound of 8, with ~2x headroom. It does NOT hold the degenerate 256-attribute item, and that is
#   the DOCUMENTED OUTCOME rather than an oversight: packing degrades to 2 of 8 (~4x the calls),
#   `chunk_items` still never drops an item for size, and the degraded chunk count for a 237-column
#   catalog (ceil(237/2) x 2 + 8 = 246) still clears the deployed 512 per-stage ceiling. Both rows
#   are pinned by `test_enrichment_context_wiring.py`
#   (`test_a_realistic_source_attribute_fill_still_packs_a_full_chunk` and
#   `test_a_DEGENERATE_source_attribute_fill_degrades_proportionally_and_never_truncates`), and the
#   trigger for revisiting it is recorded in DEFERRED-WORK A.50.
# * concept 200_000 -> 400_000. `max_items` is 20 and the item also carries the roster, so required =
#   20 x (8_000 + roster). At the 199-entry resolved roster measured above, 20 x 13_230 = 264_600; at
#   the 256-entry / 30-char saturated roster the previous block recorded (6_862 tok), 20 x 14_862 =
#   297_240 — 74% of 400_000. NOT budgeted for, deliberately: that saturated roster AND 20 columns
#   each carrying a 32_000-char curated definition AND every scalar at its own cap, all at once,
#   is not a shape any catalog produces, and crossing it degrades to ~13 items per chunk rather than
#   to a lost item.
#
# `table_synth` STAYS at 60_000, and the reason is that raising it would be theatre. Its item is a
# whole table's `column_profiles`, so its size is driven by COLUMN COUNT, not by the definition cap:
# a 200-column table measures 41_814 tok/item at the ORIGINAL 600 cap, 212_664 at 4000 and 1_619_664
# at 32_000 — it has packed 1 item per chunk at EVERY value this constant has ever held, including
# before any of these raises. Making 4 such tables share a chunk would need a ~6.5M-token budget,
# which is past any model's window; and `table_synth` fans out per TABLE (tens of items), not per
# column (thousands), so one call per table is a cost the call ceiling absorbs. Recorded here so the
# next reader does not "discover" it as a regression of this change. It is not one.
#
# `_DEFAULT_MAX_ITEMS` was, once again, not RAISED by any of the above. The ONE movement it has had
# is `definition` 8 -> 4 (a LOWERING, decided by the response ceiling — see its own note), so the
# invariant these blocks defend still holds: no isolation boundary has ever been widened to buy
# bytes.
_DEFAULT_MAX_INPUT_TOKENS = {"concept": 400_000, "definition": 200_000, "domain": 200_000,
                             "synonyms": 200_000, "unit": 200_000, "summary": 200_000,
                             "table_synth": 60_000}


def mode(short: str) -> str:
    """'single' (today's per-item path) or 'batch'. Pass A tasks default to batch; ``table_synth``
    defaults to single (see module docstring). Any task defaults to single if unlisted."""
    return os.environ.get(f"OVERLAY_ENRICH_{short.upper()}_MODE",
                          _DEFAULT_MODE.get(short, "single")).strip().lower()


def max_items(short: str) -> int:
    return int(os.environ.get(f"OVERLAY_ENRICH_BATCH_{short.upper()}_MAX_ITEMS",
                              _DEFAULT_MAX_ITEMS[short]))


def max_input_tokens(short: str) -> int:
    return int(os.environ.get(f"OVERLAY_ENRICH_BATCH_{short.upper()}_MAX_INPUT_TOKENS",
                              _DEFAULT_MAX_INPUT_TOKENS[short]))


def stage_deadline_s() -> float:
    """MF-4 — wall-clock ceiling (seconds) for an enrichment stage's batching run. Past it,
    ``run_batched`` STOPS issuing new chunks and reports ``timed_out`` (partial), so a slow provider
    can't hold the source advisory lock across the whole ingest. Default 240s
    (env ``OVERLAY_ENRICH_STAGE_DEADLINE_S``). This is a STAGE ceiling above the per-call timeout
    (``FEATUREGEN_LLM_TIMEOUT``, default 60s); the per-run wallclock budget defaults to this SAME 240s
    so a single slow chunk can't trip ``over_budget()`` and abort the rest of the run before the stage
    deadline is even reached — the two are one coherent ceiling."""
    return float(os.environ.get("OVERLAY_ENRICH_STAGE_DEADLINE_S", "240"))


@dataclass(frozen=True)
class Budget:
    max_batch_attempts: int    # retries of a failed chunk before splitting
    max_single_fallback: int   # cap on per-item fallback calls per task run
    max_provider_calls: int    # hard ceiling on provider calls per task run
    wallclock_budget_ms: int   # stop enriching past this; leave remainder uncached
    keep_threshold: float      # salvage-and-stop when valid ratio >= this
    min_split: int             # do not split a chunk below this size; go to fallback


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


#: The per-stage provider-call CEILING, and the ONE bound whose overrun costs COLUMNS rather than
#: money: an item that alone exceeds the token budget still forms its own chunk, so every other
#: bound degrades proportionally (more calls) while this one converts "more calls" into "stopped
#: enriching".
#:
#: 512, DERIVED (Task 4b, offline — no live run was authorised): the widest catalog this repo
#: exercises is 237 items, the ladder issues at most `max_batch_attempts` (2) per item plus
#: `max_single_fallback` (8) per-item calls, so the degenerate one-item-per-chunk worst case is
#: 237 x 2 + 8 = 482, and 512 is the next power of two above it.
#:
#: WHY THE CODE DEFAULT AND NOT ONLY THE MANIFEST (2026-08-09, final branch review). This was 32
#: while `deploy/kind/k8s/20-backend.yaml` shipped 512, so the number that bound a run depended on
#: whether an env var was set. That gap became a defect on this branch: Task 4b lowered
#: `_DEFAULT_MAX_ITEMS["definition"]` 8 -> 4, doubling that stage's chunk count, so a 237-column
#: catalog now needs ~60 definition chunks (a 144-column one ~36) where 237/8 ~ 30 used to fit
#: under 32. Any deployment that did not set the variable — a dev box, a test harness, a second
#: manifest — silently stopped enriching columns partway through. Raising the code default closes
#: it at the source, and `test_deployment_llm_bounds.py` now pins default == manifest so the two
#: cannot drift apart again.
#:
#: This is a COUNT bound, not a time bound. Raising it does not lengthen how long a stage may hold
#: the source advisory lock — `stage_deadline_s()` and the derived wall-clock budget are what stop
#: a runaway, and both are unchanged.
DEFAULT_MAX_PROVIDER_CALLS = 512


def budget(short: str) -> Budget:
    return Budget(
        max_batch_attempts=_int("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", 2),
        max_single_fallback=_int("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", 8),
        max_provider_calls=_int("OVERLAY_ENRICH_MAX_PROVIDER_CALLS",
                                DEFAULT_MAX_PROVIDER_CALLS),
        # DERIVED from the stage deadline, not a second hard-coded 240s. The docstring on
        # `stage_deadline_s` calls these "one coherent ceiling", and they were two independent env
        # vars that merely shared a default — so raising the deadline alone left this binding at
        # 240s and the change did nothing. An explicit override still wins: deriving is a default,
        # not a coupling.
        wallclock_budget_ms=_int("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS",
                                 int(stage_deadline_s() * 1000)),
        keep_threshold=float(os.environ.get("OVERLAY_ENRICH_KEEP_THRESHOLD", "0.75")),
        min_split=_int("OVERLAY_ENRICH_MIN_SPLIT", 4),
    )


# ── Delivery D3 — the semantic-binding LLM SELECTION task (overlay.semantic_bindings) bounds. ─────
# A SEPARATE audited failure domain from Pass A/B: exceeding ANY of these makes the semantic stage
# partial/failed (truthful counts), NEVER a crash, and NEVER touches core ingestion / Pass B. Kept
# conservative (isolation boundaries, not throughput maxima) — the LLM only re-ranks the deterministic
# D2 shortlist, so a small candidate/byte cap loses no real signal while bounding what egresses.
@dataclass(frozen=True)
class SemanticBindingBounds:
    max_candidates_per_table: int   # how many D2 candidates a table may present to the model
    max_provider_calls: int         # run-level ceiling on semantic-binding provider calls
    max_input_bytes: int            # cap on the serialized model-facing payload (pre-dispatch gate)
    deadline_s: float               # wall-clock ceiling; a call attempted past it is a no-dispatch


def semantic_binding_bounds() -> SemanticBindingBounds:
    """The D3 bounds, env-overridable. Each is a fail-soft ISOLATION boundary: crossing it yields a
    ``partial`` (candidate cap — the capped subset is still ranked) or ``failed`` (byte / call / dead-
    line — no dispatch) semantic set with truthful counts, never an exception into ingestion."""
    return SemanticBindingBounds(
        max_candidates_per_table=_int("OVERLAY_SEMBIND_MAX_CANDIDATES", 40),
        max_provider_calls=_int("OVERLAY_SEMBIND_MAX_PROVIDER_CALLS", 8),
        max_input_bytes=_int("OVERLAY_SEMBIND_MAX_INPUT_BYTES", 16000),
        deadline_s=float(os.environ.get("OVERLAY_SEMBIND_DEADLINE_S", "60")),
    )
