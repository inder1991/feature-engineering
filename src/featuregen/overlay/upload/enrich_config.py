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
_DEFAULT_MAX_ITEMS = {"concept": 20, "definition": 8, "domain": 8, "synonyms": 8, "unit": 8,
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
# `_DEFAULT_MAX_ITEMS` is deliberately UNCHANGED everywhere. Those are cross-item CONTAMINATION
# isolation boundaries (MF-8a above), not size limits; shrinking them to pay for bigger payloads
# would trade an accuracy control for a byte budget and multiply the call count at the same time.
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
# on the way to the model (`enrich_llm.MAX_DEFINITION_LEN` 600 -> 4000, `_MAX_LEN_DEFAULT` 200 ->
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
# `_DEFAULT_MAX_ITEMS` is STILL unchanged, for the reason stated above: those are contamination
# boundaries, not size limits. Raising the token budgets is exactly how you avoid having to touch
# them. Packing at the new caps is pinned by
# `test_enrichment_context_wiring.py::test_the_concept_stage_still_packs_multiple_items_per_chunk_at_the_new_caps`.
_DEFAULT_MAX_INPUT_TOKENS = {"concept": 200_000, "definition": 60_000, "domain": 200_000,
                             "synonyms": 60_000, "unit": 60_000, "summary": 100_000,
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


def budget(short: str) -> Budget:
    return Budget(
        max_batch_attempts=_int("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", 2),
        max_single_fallback=_int("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", 8),
        max_provider_calls=_int("OVERLAY_ENRICH_MAX_PROVIDER_CALLS", 32),
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
