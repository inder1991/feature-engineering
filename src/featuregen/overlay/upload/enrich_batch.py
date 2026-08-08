"""Task-agnostic batching engine for advisory enrichment (spec C2/C4/C5).
Pure helpers here (validation, chunking); the governed provider call lives in enrich_llm.py and the
degradation ladder in run_batched (Task 6)."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from featuregen.overlay.upload import enrich_config
from featuregen.overlay.upload.dispatch_audit import DispatchAuditContext
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

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


@dataclass
class CallLedger:
    """The run's SHARED physical-provider-call counter — the ONE accounting `run_batched` keeps.

    ``run_batched`` has always counted calls locally against ``budget(short).max_provider_calls``.
    That is correct only while every provider call of the run is issued BY the ladder. Pass B broke
    that: its ref-aware ``accept`` fires the dataset-profile critic from INSIDE the batch call, one
    extra physical call per criticised field, which the local counter never saw (a 12-table run
    spent 3 counted + 12 uncounted calls against a 32-call ceiling).

    Making the counter an OBJECT the caller can hold fixes it without a second budget: the caller
    builds one ledger, hands it to ``run_batched`` AND to the nested seam, and both spend from it.
    A seam that cannot get budget must NOT dispatch and must say so honestly (never a silent skip).
    """

    max_provider_calls: int
    calls: int = 0

    def exhausted(self) -> bool:
        return self.calls >= self.max_provider_calls

    def charge(self, n: int = 1) -> bool:
        """Reserve ``n`` physical calls BEFORE dispatching them. Returns False and charges nothing
        when the ceiling is already reached — the caller must then not dispatch."""
        if self.exhausted():
            return False
        self.calls += n
        return True

    def settle(self, *, reserved: int, actual: int) -> None:
        """Reconcile a reservation against what the provider actually cost (a driver may repair or
        retry, and a blocked call costs nothing), so the ledger always holds the PHYSICAL total."""
        self.calls += actual - reserved


@dataclass(frozen=True)
class BatchItemOutcome:
    ref: str
    status: str
    value: str | None
    reason_codes: tuple[str, ...]
    #: Task 9c — the LENGTH of the raw value the acceptor judged, never the value. Present on the
    #: dispositions where a length is the diagnosis (``invalid_value``/``blank``); ``None`` on
    #: ``missing`` (nothing came back to measure) and on the egress/valid paths. Defaulted so every
    #: existing 4-positional construction (``enrich_llm``'s egress outcomes) is unchanged.
    value_len: int | None = None


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
            outcomes.append(BatchItemOutcome(ref, BLANK, None, (BLANK,), 0))
            continue
        value, reason = accept(raw, ref) if ref_aware else accept(raw)
        if value is None:
            # Task 9c: the LENGTH of what the acceptor refused rides with the reason. A definition
            # discarded WHOLE for one newline and one discarded for being 40_000 chars are the same
            # `invalid_value` from outside; the length is what tells them apart without a re-run.
            outcomes.append(BatchItemOutcome(ref, INVALID, None, (reason,), len(raw)))
        else:
            outcomes.append(BatchItemOutcome(ref, VALID, value, (VALID,), len(raw)))
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
                dispatch_subjects: Mapping[str, dict] | None = None,
                call_ledger: CallLedger | None = None) -> dict[str, str]:
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

    Task 9c — what ``report`` carries when one is supplied, all counts/codes and never a value:

    * ``chunks_planned`` / ``chunks_issued`` / ``provider_calls`` / ``fallback_calls`` — the
      PHYSICAL cost, which was previously in-memory counters only and never persisted.
    * ``bounds`` (``{code: count}``) — EVERY time a bound withheld work, and which one:
      ``call_ceiling`` | ``wallclock_budget`` | ``stage_deadline`` | ``fallback_cap`` |
      ``fallback_schema_unregistered``. A dict, not a scalar, because a per-item ``fallback_cap``
      (a few leftovers not individually retried) and a ``call_ceiling`` (whole chunks never sent)
      have completely different severities and a run can hit both.
    * ``stopped_by`` — the FIRST code in ``bounds``, or ``exception`` when the ladder raised, or
      ``unattributed`` when items went undispatched and no guard claimed it. Read ``bounds`` when
      ``stopped_by`` looks benign; a scalar alone can mask the worse of two bounds.
    * ``outcomes`` (``{status: count}``) and ``rejects`` (``{reason code: count}``) — these count
      REJECTION EVENTS, not distinct items. The ladder re-accounts a chunk it retries or splits, so
      an item rejected twice counts twice and these can exceed the stage's item count. That is the
      honest physical reading; ``detail["unresolved"]`` is the per-ITEM one.

    The account is written AS IT HAPPENS and finalized in a ``finally``, so a ladder that raises
    still leaves the caller a truthful account rather than a happy-path zero.

    C5-T5 — dispatch attribution: with ``ingestion_run_id`` set, every PHYSICAL call this ladder
    issues (each chunk, retry, split, and single fallback) carries a ``DispatchAuditContext`` built
    for exactly the items IN that call — ``dispatch_stage`` (falling back to ``task``) as the stage,
    and ``dispatch_subjects`` (a ``{ref: subject}`` mapping) supplying each item's
    ``{catalog_source, object_ref, logical_ref, field_names}`` subject. ``ingestion_run_id=None``
    (every direct caller) threads ``dispatch_audit=None`` — byte-for-byte today's behavior.

    ``call_ledger`` — the run's SHARED :class:`CallLedger`, for a caller whose ``accept`` itself
    issues provider calls (Pass B's dataset-profile critic, which dispatches from INSIDE the batch
    call). Passing one makes those nested calls spend from the SAME ceiling this ladder spends
    from, so ``max_provider_calls`` bounds the run's PHYSICAL total rather than only its chunks.
    ``None`` (every other caller) builds a private ledger holding exactly today's local counter —
    byte-for-byte unchanged."""
    from featuregen.overlay.upload.enrich_llm import audited_batch_call  # lazy (import cycle)
    b = enrich_config.budget(short)
    max_items = enrich_config.max_items(short)
    max_tokens = enrich_config.max_input_tokens(short)
    started = time.monotonic()
    ledger = call_ledger if call_ledger is not None else CallLedger(b.max_provider_calls)
    resolved: dict[str, str] = {}
    fallback_used = 0
    # Refs actually SENT to the provider (any batch chunk this ladder issued). Its complement over
    # `items` is the honest budget/deadline `not_attempted` count — items the cutoff skipped WITHOUT
    # ever dispatching them, so they were never "failed", just never tried.
    dispatched: set[str] = set()

    def _ctx_for(call_items: list[BatchItem]) -> DispatchAuditContext | None:
        # One context per PHYSICAL call, scoped to exactly the items it carries — a retried or
        # split chunk (or a single fallback) attributes its own subjects, never the whole run's.
        if ingestion_run_id is None:
            return None
        subs = dispatch_subjects or {}
        return DispatchAuditContext(
            ingestion_run_id=ingestion_run_id, stage=dispatch_stage or task,
            subjects=tuple(subs[it.ref] for it in call_items if it.ref in subs))

    # Task 9c — the ledger reading at ENTRY, so `provider_calls` below reports what THIS ladder
    # spent even when the caller handed it a shared ledger another stage had already charged.
    calls_at_entry = ledger.calls

    def _bound_hit() -> str | None:
        """WHICH bound is currently blocking, as a closed code — the diagnostic `over_budget`'s
        bare bool threw away. `over_budget()` conflated the call ceiling with the ladder's
        wall-clock budget, and the stage deadline (checked separately, below) was a third. A run
        that reports `truncated` is uninterpretable without knowing which of the three did it: a
        too-low call ceiling does not slow enrichment, it silently STOPS enriching columns."""
        if ledger.exhausted():
            return "call_ceiling"
        if (time.monotonic() - started) * 1000 >= b.wallclock_budget_ms:
            return "wallclock_budget"
        return None

    def _note_stop(code: str) -> None:
        """Record a bound that actually withheld work. Written into the caller's `report` AS IT
        HAPPENS, never at the end: `run_batched` can raise (a provider fault escaping the seam),
        and the caller reads this same dict from its `except` handler — an account only written on
        the happy path would be absent from exactly the runs that need explaining.

        EVERY hit is counted in `bounds`, and only the first also sets the `stopped_by` headline.
        Recording only the first would let a benign per-item `fallback_cap` — which fires while
        chunks are still being issued normally — permanently mask a `call_ceiling` that later
        stopped whole chunks from going at all."""
        if report is None:
            return
        counts: dict[str, int] = report.setdefault("bounds", {})
        counts[code] = counts.get(code, 0) + 1
        report.setdefault("stopped_by", code)

    def over_budget() -> bool:
        return _bound_hit() is not None

    def _account(outcomes) -> None:
        """Task 9c — the per-item DRAFTING account, aggregated into the caller's report and, for a
        rejection, emitted as one greppable line.

        `_enrichment_outcome` can only say how many items went unresolved; it cannot say WHY, and
        the ways an item can fail (`missing` / `blank` / `duplicate` / `invalid_value` /
        `egress_rejected`) have completely different causes. `rejects` breaks the acceptor's own
        reason code out further — a definition discarded whole for one newline reads identically to
        a provider blip without it. NOTHING here is a value: a ref, a closed status/reason code,
        and a LENGTH.

        These are EVENT counts, not distinct-item counts: the ladder re-accounts a chunk it retries
        or splits, so an item rejected on two attempts contributes two. Deliberate — the physical
        question ("how often did the provider hand back an enumeration?") is the one the counts can
        answer honestly as-they-happen; the per-item question is answered by `detail["unresolved"]`,
        which is computed from the returned resolution map."""
        if report is None:
            return
        for o in outcomes:
            if o.status == VALID:
                continue
            # The keys are created LAZILY, on the first rejection. An empty `outcomes: {}` on a
            # clean run is not information — and it costs the reader the one cheap signal worth
            # having, that an ABSENT key means the thing never happened at all.
            statuses: dict[str, int] = report.setdefault("outcomes", {})
            rejects: dict[str, int] = report.setdefault("rejects", {})
            statuses[o.status] = statuses.get(o.status, 0) + 1
            for code in o.reason_codes:
                rejects[code] = rejects.get(code, 0) + 1
            # One line per rejected item, machine-parseable (`key=value`, no prose in the fields):
            # the stage detail can only carry counts, and "which column, which rule, how long" is
            # the question a re-run would otherwise be needed to answer.
            #
            # An EXTRA ref is the ONE field here that is not ours: it is a ref the model returned
            # that we never asked about, so it is unvalidated model output and could carry anything
            # — including content. Every other status keys on a ref THIS code minted (a content
            # hash or a table name). Suppressed rather than logged; the count in `outcomes` still
            # says a hallucinated ref came back, which is the whole diagnostic value of it.
            logger.info("enrich_reject stage=%s ref=%s status=%s reason=%s len=%s",
                        dispatch_stage or short,
                        "<unrecognized>" if o.status == EXTRA else o.ref, o.status,
                        "|".join(o.reason_codes) or "-",
                        "-" if o.value_len is None else o.value_len)

    def process(chunk: list[BatchItem], attempt: int) -> None:
        nonlocal fallback_used
        if not chunk or over_budget():
            if chunk:
                counters.incr(f"overlay.enrich.{short}.batch.budget_exhausted")
                _note_stop(_bound_hit() or "call_ceiling")
            return
        dispatched.update(it.ref for it in chunk)   # this chunk is now being sent to the provider
        # RESERVE this chunk's call BEFORE issuing it (the guard above already cleared it), so a
        # seam nested inside the call — Pass B's critic, which fires from `accept` — sees the
        # in-flight call in the ledger and cannot spend the ceiling out from under it. `settle`
        # then reconciles against what the driver actually cost (repairs/retries, or 0 for a
        # blocked call), leaving the ledger at the same total the old `calls += provider_calls`
        # produced.
        ledger.charge()
        res = audited_batch_call(conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
                                 shared_metadata=shared_metadata, items=chunk, out_key=out_key,
                                 instruction=instruction, accept=accept, actor=actor,
                                 extract=extract, ref_aware=ref_aware,
                                 prompt_version=prompt_version, schema_version=schema_version,
                                 dispatch_audit=_ctx_for(chunk))
        ledger.settle(reserved=1, actual=res.provider_calls)
        counters.incr(f"overlay.enrich.{short}.batch.calls")
        _account(res.outcomes)
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
        nonlocal fallback_used
        # A ref_aware (structured) task has NO single-call fallback (see _single_fallback): each item
        # would resolve to (None, MISSING) WITHOUT a provider call. Skip the loop entirely so a no-op
        # fallback never inflates `calls`/`fallback_used` — on a >max_items multi-chunk Pass B run the
        # spurious increments could trip over_budget() early — nor emits a bogus single_fallback
        # counter. (Task 4 carry-forward: the increments previously ran before the ref_aware skip.)
        if ref_aware:
            return
        from featuregen.overlay.upload.enrich_llm import SchemaUnregisteredError  # lazy (cycle)
        schema_unregistered = False
        for it in unresolved:
            if schema_unregistered or fallback_used >= b.max_single_fallback or over_budget():
                counters.incr(f"overlay.enrich.{short}.batch.left_uncached")
                # The per-item fallback has a FOURTH bound the chunk loop does not — its own cap.
                # Naming it separately is the point: `left_uncached` under `fallback_cap` is a
                # config ceiling, under `call_ceiling` it is the run's provider budget.
                _note_stop("fallback_schema_unregistered" if schema_unregistered
                           else ("fallback_cap" if fallback_used >= b.max_single_fallback
                                 else (_bound_hit() or "call_ceiling")))
                continue
            fallback_used += 1
            ledger.charge()          # one per-item call, reserved before it is issued (as before)
            counters.incr(f"overlay.enrich.{short}.batch.single_fallback")
            try:
                value, status = _single_fallback(conn, client, task=task, out_key=out_key,
                                                  instruction=instruction, item=it,
                                                  shared_metadata=shared_metadata, accept=accept,
                                                  actor=actor, ref_aware=ref_aware,
                                                  prompt_version=prompt_version,
                                                  schema_version=schema_version,
                                                  dispatch_audit=_ctx_for([it]))
            except SchemaUnregisteredError:
                # The FALLBACK's (schema_id, version) pair is unregistered (a version bumped
                # without a body) — raised at dispatch, BEFORE any provider call, and
                # deterministic for every remaining item this run. Contain exactly this
                # registration bug: the affected items take the same terminal left-uncached
                # outcome as a budget cutoff (retried next ingest), the batch-RESOLVED items
                # survive, and nothing re-raises. Any other exception propagates unchanged.
                logger.warning("single-fallback schema unregistered for task %r — leaving %r "
                               "and the remaining fallback items uncached",
                               task, it.ref, exc_info=True)
                counters.incr(f"overlay.enrich.{short}.batch.left_uncached")
                schema_unregistered = True
                continue
            if value is not None:
                resolved[it.ref] = value

    deadline_start = now()
    planned = chunk_items(items, max_items=max_items, max_input_tokens=max_tokens)
    if report is not None:
        # Written BEFORE the loop so a mid-ladder raise still leaves the plan on the record; the
        # item-count/token bound never STOPS the ladder, it only decides how many chunks the work
        # was split into, so `chunks_planned` vs `chunks_issued` is how it shows up.
        report["chunks_planned"] = len(planned)
        report["chunks_issued"] = 0
        report["provider_calls"] = 0
    try:
        for chunk in planned:
            # MF-4 — stage deadline: stop ISSUING new chunks once the ceiling is crossed. Facts
            # already asserted and the rest of ingestion are unaffected — the run returns a partial
            # result and the source advisory lock is released rather than held by a hung call.
            if deadline_s is not None and now() - deadline_start >= deadline_s:
                counters.incr(f"overlay.enrich.{short}.batch.timed_out")
                if report is not None:
                    report["timed_out"] = True
                _note_stop("stage_deadline")
                break
            sent_before = len(dispatched)
            process(chunk, 0)
            if report is not None and len(dispatched) > sent_before:
                # Counted from the DISPATCH, not from `process` returning. `process` returns
                # normally when its guard refused to dispatch at all (a bound was already hit), so
                # incrementing on return reported every planned chunk as issued on exactly the run
                # — a `call_ceiling` cutoff — this field exists to explain. `dispatched` growing is
                # the fact that the chunk's items actually went to the provider.
                #
                # Per chunk, not once at the end: an exception out of a LATER `process` must not
                # erase the account of the chunks that already ran.
                report["chunks_issued"] += 1
    except BaseException:
        # A fault ESCAPING the seam is not a bound — naming it `unattributed` below would read as
        # "items were skipped and we don't know why" when in fact we do. The caller still gets the
        # physical account, settled by the `finally`.
        #
        # The headline is OVERWRITTEN, not `setdefault`: a run that hit a benign `fallback_cap` and
        # then died would otherwise report `fallback_cap` with nothing anywhere saying it raised.
        # What stopped this ladder was the exception; the earlier bound is not lost — every bound
        # hit is still counted in `bounds`, which is now where the full picture lives.
        if report is not None:
            bounds: dict[str, int] = report.setdefault("bounds", {})
            bounds["exception"] = bounds.get("exception", 0) + 1
            report["stopped_by"] = "exception"
        raise
    finally:
        # Honest truncation signal (#22): items the budget/deadline cutoff skipped WITHOUT dispatch
        # — the complement of everything this ladder actually sent. The caller threads it into the
        # stage detail so a truncated run is labeled `truncated`, not `items_failed`. (The
        # in-memory `budget_exhausted` counter is metrics-only, never persisted.)
        #
        # In a `finally` because the failure path is the one that needs explaining: a raise out of
        # the first chunk previously left `provider_calls: 0` on the record though a call had
        # already been charged — a false zero, which is worse than an absent field.
        if report is not None:
            not_attempted = len({it.ref for it in items} - dispatched)
            if not_attempted:
                report["not_attempted"] = not_attempted
            report["provider_calls"] = ledger.calls - calls_at_entry
            report["fallback_calls"] = fallback_used
            if not_attempted:
                # Items were skipped without dispatch and no guard claimed it. That is a real hole
                # in the account, not a clean run — say so rather than leaving the field absent,
                # which reads as "nothing stopped it".
                report.setdefault("stopped_by", "unattributed")
    return resolved
