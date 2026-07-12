"""Rollout knobs for batched enrichment (spec C10). All default so production is unchanged:
mode=single, conservative budgets. Batch is opt-in per task via env — the kill switch."""
from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_MAX_ITEMS = {"concept": 40, "definition": 12, "domain": 20, "table_synth": 8}
_DEFAULT_MAX_INPUT_TOKENS = {"concept": 14000, "definition": 8000, "domain": 8000,
                             "table_synth": 6000}


def mode(short: str) -> str:
    """'single' (default, today's exact path) or 'batch'."""
    return os.environ.get(f"OVERLAY_ENRICH_{short.upper()}_MODE", "single").strip().lower()


def max_items(short: str) -> int:
    return int(os.environ.get(f"OVERLAY_ENRICH_BATCH_{short.upper()}_MAX_ITEMS",
                              _DEFAULT_MAX_ITEMS[short]))


def max_input_tokens(short: str) -> int:
    return int(os.environ.get(f"OVERLAY_ENRICH_BATCH_{short.upper()}_MAX_INPUT_TOKENS",
                              _DEFAULT_MAX_INPUT_TOKENS[short]))


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
