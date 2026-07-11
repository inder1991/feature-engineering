"""Task-agnostic batching engine for advisory enrichment (spec C2/C4/C5).
Pure helpers here (validation, chunking); the governed provider call lives in enrich_llm.py and the
degradation ladder in run_batched (Task 6)."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

VALID = "valid"
MISSING = "missing"
EXTRA = "extra"
DUPLICATE = "duplicate"
BLANK = "blank"
INVALID = "invalid_value"
EGRESS = "egress_rejected"
FALLBACK_VALID = "fallback_valid"
FALLBACK_FAILED = "fallback_failed"

Accept = Callable[[str], "tuple[str | None, str]"]   # raw -> (value_to_cache | None, reason_code)


@dataclass(frozen=True)
class BatchItem:
    ref: str          # stable per-item id = the cache/return key (content hash, or table name)
    metadata: dict    # metadata-only fields for the prompt (table/column/type/columns/concept)


@dataclass(frozen=True)
class BatchItemOutcome:
    ref: str
    status: str
    value: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class BatchCallResult:
    outcomes: tuple[BatchItemOutcome, ...]
    provider_calls: int
    input_tokens: int
    output_tokens: int


def validate_batch_results(items: list[BatchItem], results: list[dict], out_key: str,
                           accept: Accept) -> list[BatchItemOutcome]:
    """Classify every returned entry against the expected ref-set (spec C2): valid / invalid_value /
    blank / duplicate / extra, and every unreturned ref as missing. Nothing is silently collapsed."""
    expected = {it.ref for it in items}
    seen: set[str] = set()
    outcomes: list[BatchItemOutcome] = []
    for entry in results:
        ref = entry.get("ref")
        raw = str(entry.get(out_key, "")).strip()
        if ref not in expected:
            outcomes.append(BatchItemOutcome(str(ref), EXTRA, None, (EXTRA,)))
            continue
        if ref in seen:
            outcomes.append(BatchItemOutcome(ref, DUPLICATE, None, (DUPLICATE,)))
            continue
        seen.add(ref)
        if not raw:
            outcomes.append(BatchItemOutcome(ref, BLANK, None, (BLANK,)))
            continue
        value, reason = accept(raw)
        if value is None:
            outcomes.append(BatchItemOutcome(ref, INVALID, None, (reason,)))
        else:
            outcomes.append(BatchItemOutcome(ref, VALID, value, (VALID,)))
    for ref in expected - seen:
        outcomes.append(BatchItemOutcome(ref, MISSING, None, (MISSING,)))
    return outcomes


def estimate_tokens(item: BatchItem) -> int:
    """Cheap upper-ish estimate: ~4 chars/token over the item's metadata JSON, floor 8."""
    return max(8, len(json.dumps(item.metadata, default=str)) // 4)


def chunk_items(items: list[BatchItem], *, max_items: int,
                max_input_tokens: int) -> list[list[BatchItem]]:
    """Split into chunks bounded by BOTH item count and estimated input tokens (spec C5). A single
    item that alone exceeds the token budget still forms its own chunk (never dropped)."""
    chunks: list[list[BatchItem]] = []
    cur: list[BatchItem] = []
    tok = 0
    for it in items:
        t = estimate_tokens(it)
        if cur and (len(cur) >= max_items or tok + t > max_input_tokens):
            chunks.append(cur)
            cur, tok = [], 0
        cur.append(it)
        tok += t
    if cur:
        chunks.append(cur)
    return chunks
