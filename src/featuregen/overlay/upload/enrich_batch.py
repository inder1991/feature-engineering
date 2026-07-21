"""Task-agnostic batching engine for advisory enrichment (spec C2/C4/C5).
Pure helpers here (validation, chunking); the governed provider call lives in enrich_llm.py and the
degradation ladder in run_batched (Task 6)."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from featuregen.overlay.upload import enrich_config
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.runtime.observability import counters

# NOTE: `audited_batch_call` / `audited_enrich_call` are imported LAZILY inside run_batched /
# _single_fallback (not at module top) to break the enrich_batch <-> enrich_llm import cycle:
# enrich_llm imports names from this module at its own top, so a module-level import back into
# enrich_llm here fails at collection (partially initialized module).

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
                           accept: Accept, *, extract=None, ref_aware: bool = False
                           ) -> list[BatchItemOutcome]:
    """Classify every returned entry against the expected ref-set (spec C2): valid / invalid_value /
    blank / duplicate / extra, and every unreturned ref as missing. Nothing is silently collapsed.

    ``extract(entry) -> str`` overrides scalar out-key extraction so a STRUCTURED per-item result
    (e.g. a nested ``synthesis`` object) can be serialized to a canonical string. When
    ``ref_aware`` is set, ``accept`` is called as ``accept(raw, ref)`` so per-item validation that
    depends on the item's identity (e.g. "grain columns must be columns OF THIS table") is done
    HERE and yields a proper ``INVALID`` outcome — never accepted-then-post-filtered. Defaults keep
    the scalar ``accept(raw)`` path byte-for-byte for Pass A."""
    expected = {it.ref for it in items}
    seen: set[str] = set()
    outcomes: list[BatchItemOutcome] = []
    for entry in results:
        ref = entry.get("ref")
        raw = (extract(entry) if extract is not None
               else str(entry.get(out_key, "")).strip())
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
        value, reason = accept(raw, ref) if ref_aware else accept(raw)
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


def _single_fallback(conn, client, *, task, out_key, instruction, item: BatchItem, shared_metadata,
                     accept, actor, ref_aware: bool = False,
                     prompt_version: int = 1, schema_version: int = 1,
                     dispatch_audit: DispatchAuditContext | None = None) -> tuple[str | None, str]:
    """One per-item fallback through the existing single seam. Returns (value|None, status).

    A ``ref_aware`` (structured) task has NO single-call fallback in Phase 2: the flat single schema
    carries no ``synthesis`` wrapper and the ref-aware ``accept`` needs ``(raw, ref)``, so the item is
    simply left unresolved (MISSING) — never re-sent through the mismatched flat seam.

    ``prompt_version``/``schema_version`` (default ``1``) thread through to the single seam so a
    versioned batch that degrades to per-item fallback runs under the SAME contract, never silently
    retrying under v1 — the prompt_id label is versioned to match (``_v1`` at the default).

    ``dispatch_audit`` (C5-T5): the run's per-ITEM context (this one item's subject), so a batch that
    degrades to the single seam stays run+subject attributed; ``None`` is byte-identical."""
    if ref_aware:
        return None, MISSING
    from featuregen.overlay.upload.enrich_llm import audited_enrich_call  # lazy (import cycle)
    single_prompt = task.rsplit(".", 1)[-1]   # concept|definition|domain
    raw = audited_enrich_call(
        conn, client, task=task, prompt_id=f"overlay_{single_prompt}_v{prompt_version}",
        schema_id=f"overlay_{single_prompt}", out_key=out_key,
        catalog_metadata={**shared_metadata, **item.metadata}, instruction=instruction, actor=actor,
        prompt_version=prompt_version, schema_version=schema_version,
        dispatch_audit=dispatch_audit,
        # perf (vocab-caching): a batch that degrades to per-item fallback still carries the static
        # shared_metadata (the concept vocabulary) on every item — mark it as the cached shared prefix
        # so those fallback calls reuse it too rather than re-billing it each time.
        cacheable_metadata_keys=tuple(shared_metadata))
    if raw is None:
        return None, FALLBACK_FAILED
    value, _reason = accept(raw)
    return (value, FALLBACK_VALID) if value is not None else (None, FALLBACK_FAILED)


def run_batched(conn, client, *, short: str, task: str, prompt_id: str, schema_id: str,
                shared_metadata: dict, items: list[BatchItem], out_key: str, instruction: str,
                accept: Accept, actor, extract=None, ref_aware: bool = False,
                prompt_version: int = 1, schema_version: int = 1,
                now: Callable[[], float] = time.monotonic, deadline_s: float | None = None,
                report: dict | None = None,
                ingestion_run_id: str | None = None, dispatch_stage: str | None = None,
                dispatch_subjects: Mapping[str, dict] | None = None) -> dict[str, str]:
    """Chunk `items`, call the governed batch seam, and walk the bounded degradation ladder
    (spec C4): salvage valid -> retry a failed chunk -> adaptive split -> capped single fallback ->
    leave remainder uncached. Returns {ref: accepted_value} for items resolved this run.

    ``prompt_version``/``schema_version`` (default ``1`` — byte-for-byte today) pin the enrichment
    contract and thread through BOTH the batch seam AND the single-fallback seam, so a versioned batch
    that degrades to per-item fallback can never silently retry under the v1 prompt/schema.

    MF-4 — stage deadline: when ``deadline_s`` is not None, before issuing each top-level chunk we
    check the INJECTED monotonic clock ``now`` (test-seam; real ``time.monotonic`` in production). If
    ``now() - start >= deadline_s`` we STOP issuing new chunks, mark ``report['timed_out']`` (when a
    ``report`` dict is supplied) and increment a counter, and break — the ALREADY-resolved items are
    returned as a PARTIAL result, no exception is raised, and the caller's ingest stage degrades to
    partial while the already-asserted facts still commit. ``deadline_s=None`` (the default) leaves
    the guard fully inert, preserving today's behavior byte-for-byte; the enrichment entry points
    pass ``enrich_config.stage_deadline_s()`` so production has a concrete ceiling.

    C5-T5 — dispatch attribution: with ``ingestion_run_id`` set, every PHYSICAL call this ladder
    issues (each chunk, retry, split, and single fallback) carries a ``DispatchAuditContext`` built
    for exactly the items IN that call — ``dispatch_stage`` (falling back to ``task``) as the stage,
    and ``dispatch_subjects`` (a ``{ref: subject}`` mapping) supplying each item's
    ``{catalog_source, object_ref, logical_ref, field_names}`` subject. ``ingestion_run_id=None``
    (every direct caller) threads ``dispatch_audit=None`` — byte-for-byte today's behavior."""
    from featuregen.overlay.upload.enrich_llm import audited_batch_call  # lazy (import cycle)
    b = enrich_config.budget(short)
    max_items = enrich_config.max_items(short)
    max_tokens = enrich_config.max_input_tokens(short)
    started = time.monotonic()
    calls = 0
    resolved: dict[str, str] = {}
    fallback_used = 0

    def _ctx_for(call_items: list[BatchItem]) -> DispatchAuditContext | None:
        # One context per PHYSICAL call, scoped to exactly the items it carries — a retried or
        # split chunk (or a single fallback) attributes its own subjects, never the whole run's.
        if ingestion_run_id is None:
            return None
        subs = dispatch_subjects or {}
        return DispatchAuditContext(
            ingestion_run_id=ingestion_run_id, stage=dispatch_stage or task,
            subjects=tuple(subs[it.ref] for it in call_items if it.ref in subs))

    def over_budget() -> bool:
        return (calls >= b.max_provider_calls
                or (time.monotonic() - started) * 1000 >= b.wallclock_budget_ms)

    def process(chunk: list[BatchItem], attempt: int) -> None:
        nonlocal calls, fallback_used
        if not chunk or over_budget():
            counters.incr(f"overlay.enrich.{short}.batch.budget_exhausted") if chunk else None
            return
        res = audited_batch_call(conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
                                 shared_metadata=shared_metadata, items=chunk, out_key=out_key,
                                 instruction=instruction, accept=accept, actor=actor,
                                 extract=extract, ref_aware=ref_aware,
                                 prompt_version=prompt_version, schema_version=schema_version,
                                 dispatch_audit=_ctx_for(chunk))
        calls += res.provider_calls
        counters.incr(f"overlay.enrich.{short}.batch.calls")
        for o in res.outcomes:
            if o.status in (VALID,) and o.value is not None:
                resolved[o.ref] = o.value
        # An EGRESS-excluded item (C9 per-item exclusion) is TERMINAL — it must never be retried,
        # split, or fallback-called this run (that would re-send its metadata through the single seam).
        # Drop it from `unresolved` so the ladder skips it; it stays uncached and is retried next ingest.
        egress_refs = {o.ref for o in res.outcomes if o.status == EGRESS}
        unresolved = [it for it in chunk if it.ref not in resolved and it.ref not in egress_refs]
        if not unresolved:
            return
        valid_ratio = 1 - len(unresolved) / len(chunk)
        if valid_ratio >= b.keep_threshold:
            _fallback(unresolved)                      # salvage the bulk; fallback only the few
            return
        if attempt < b.max_batch_attempts and not over_budget():
            counters.incr(f"overlay.enrich.{short}.batch.retry")
            process(unresolved, attempt + 1)           # retry the unresolved as a chunk
            return
        if len(unresolved) > b.min_split and not over_budget():
            counters.incr(f"overlay.enrich.{short}.batch.split")
            mid = len(unresolved) // 2
            process(unresolved[:mid], 0)
            process(unresolved[mid:], 0)
            return
        _fallback(unresolved)

    def _fallback(unresolved: list[BatchItem]) -> None:
        nonlocal calls, fallback_used
        # A ref_aware (structured) task has NO single-call fallback (see _single_fallback): each item
        # would resolve to (None, MISSING) WITHOUT a provider call. Skip the loop entirely so a no-op
        # fallback never inflates `calls`/`fallback_used` — on a >max_items multi-chunk Pass B run the
        # spurious increments could trip over_budget() early — nor emits a bogus single_fallback
        # counter. (Task 4 carry-forward: the increments previously ran before the ref_aware skip.)
        if ref_aware:
            return
        for it in unresolved:
            if fallback_used >= b.max_single_fallback or over_budget():
                counters.incr(f"overlay.enrich.{short}.batch.left_uncached")
                continue
            fallback_used += 1
            calls += 1
            counters.incr(f"overlay.enrich.{short}.batch.single_fallback")
            value, status = _single_fallback(conn, client, task=task, out_key=out_key,
                                              instruction=instruction, item=it,
                                              shared_metadata=shared_metadata, accept=accept,
                                              actor=actor, ref_aware=ref_aware,
                                              prompt_version=prompt_version,
                                              schema_version=schema_version,
                                              dispatch_audit=_ctx_for([it]))
            if value is not None:
                resolved[it.ref] = value

    deadline_start = now()
    for chunk in chunk_items(items, max_items=max_items, max_input_tokens=max_tokens):
        # MF-4 — stage deadline: stop ISSUING new chunks once the ceiling is crossed. Facts already
        # asserted and the rest of ingestion are unaffected — the run returns a partial result and the
        # source advisory lock is released rather than held by a hung provider call.
        if deadline_s is not None and now() - deadline_start >= deadline_s:
            counters.incr(f"overlay.enrich.{short}.batch.timed_out")
            if report is not None:
                report["timed_out"] = True
            break
        process(chunk, 0)
    return resolved
