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
                 "table_synth": "single"}
# MF-8a — conservative ISOLATION boundaries, not throughput maxima. The old 40/12/20/8 were picked
# for throughput with NO accuracy evidence (the hermetic gold gate drives a scripted FakeLLM that
# echoes each column's expected answer, so it measures the harness, not the provider, and compares no
# batch sizes / no cross-item contamination). Until the key-gated real-provider sweep
# (tests/eval/test_batch_size_sweep.py) produces evidence a higher ceiling holds accuracy, keep these
# small so cross-item contamination has less room. The env override still raises a ceiling per task.
_DEFAULT_MAX_ITEMS = {"concept": 20, "definition": 8, "domain": 8, "table_synth": 4}
_DEFAULT_MAX_INPUT_TOKENS = {"concept": 14000, "definition": 8000, "domain": 8000,
                             "table_synth": 6000}


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
    (``FEATUREGEN_LLM_TIMEOUT``, default 60s) and the per-run wallclock budget."""
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
        wallclock_budget_ms=_int("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS", 20000),
        keep_threshold=float(os.environ.get("OVERLAY_ENRICH_KEEP_THRESHOLD", "0.75")),
        min_split=_int("OVERLAY_ENRICH_MIN_SPLIT", 4),
    )
