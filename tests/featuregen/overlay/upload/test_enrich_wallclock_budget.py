"""The per-RUN wallclock budget (``over_budget()``) must be COHERENT with the stage deadline.

``over_budget()`` (enrich_batch) trips ``run_batched`` on ``b.wallclock_budget_ms``; a run budget
SMALLER than a single call's own timeout (``FEATUREGEN_LLM_TIMEOUT`` default 60s) or the stage
deadline (``stage_deadline_s()`` default 240s) is incoherent: the first slow chunk (a wide table can
take ~37s) blows the 20s run budget, so ``process()`` returns at its ``over_budget()`` guard and every
REMAINING chunk is skipped — only the first ~20 columns ever enrich. The fix raises the default so the
stage deadline is the single coherent ceiling. These tests are SDK-free and deterministic: a FakeLLM
plus a monotonic stub tied to the OBSERVED dispatch count (never real sleep) so a slow first chunk's
effect on the guard math is exact and robust to any incidental ``time.monotonic`` call.
"""
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich_batch as eb
from featuregen.overlay.upload import enrich_config as cfg

_CTASK = "overlay.enrich.concept"


def _accept_known(raw):
    known = {"monetary_stock", "unclassified"}
    return (raw, "valid") if raw in known else (None, "invalid_value")


class _Counting:
    """Wraps an LLMClient and counts `.call` invocations — one call per dispatched chunk here (no
    retry/fallback), so ``n`` is exactly the number of chunks that reached the provider."""

    def __init__(self, inner):
        self.inner, self.n = inner, 0

    def call(self, request):
        self.n += 1
        return self.inner.call(request)


def _two_singleton_chunks(monkeypatch):
    # Two items, one per chunk (max_items=1) -> two top-level chunks; no retry/fallback so a
    # dispatched chunk costs EXACTLY one provider call. A skipped 2nd chunk is thus visible as n==1.
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "1")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")
    monkeypatch.delenv("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS", raising=False)
    return [eb.BatchItem("h1", {"table": "t", "column": "a", "type": "text"}),
            eb.BatchItem("h2", {"table": "t", "column": "b", "type": "text"})]


def _slow_first_chunk_clock(client, *, first_chunk_seconds):
    """A monotonic stub tied to OBSERVED dispatch: 0.0 until the first chunk has been dispatched,
    then ``first_chunk_seconds`` thereafter. ``started`` (before any dispatch) and the 1st chunk's
    ``over_budget()`` guard both read 0.0; the 2nd chunk's guard reads the elapsed first-chunk time.
    Independent of the exact number of ``time.monotonic`` calls."""
    def _clock():
        return 0.0 if client.n == 0 else float(first_chunk_seconds)
    return _clock


def test_wallclock_budget_default_is_coherent_with_stage_deadline(monkeypatch):
    # The per-run budget floor is the stage deadline: a run budget below a single call's own timeout
    # (60s) OR the stage deadline (240s) guarantees a slow chunk kills the rest of the run.
    monkeypatch.delenv("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS", raising=False)
    monkeypatch.delenv("OVERLAY_ENRICH_STAGE_DEADLINE_S", raising=False)
    for short in ("concept", "definition", "domain", "table_synth"):
        assert cfg.budget(short).wallclock_budget_ms >= cfg.stage_deadline_s() * 1000
    assert cfg.budget("concept").wallclock_budget_ms == 240000


def test_slow_first_chunk_still_dispatches_remaining_chunks(db, monkeypatch):
    # With the coherent default budget, a first chunk that burns ~40s (< 240s ceiling) must NOT abort
    # the remaining chunks — the whole reason a wide table was only enriching its first ~20 columns.
    items = _two_singleton_chunks(monkeypatch)
    client = _Counting(FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"},
        {"ref": "h2", "concept": "monetary_stock"}]})}))
    monkeypatch.setattr(eb.time, "monotonic",
                        _slow_first_chunk_clock(client, first_chunk_seconds=40.0))

    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None)

    assert client.n == 2                     # BOTH chunks dispatched — the 40s first chunk didn't abort
    assert got == {"h1": "monetary_stock", "h2": "monetary_stock"}


def test_over_budget_still_honors_max_provider_calls(db, monkeypatch):
    # The coherent (huge) time budget must NOT weaken the call ceiling: with max_provider_calls=1 the
    # 2nd chunk is over budget on the CALL count and is skipped, even though ~no wall time has elapsed.
    items = _two_singleton_chunks(monkeypatch)
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_PROVIDER_CALLS", "1")
    client = _Counting(FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"}]})}))

    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None)

    assert client.n == 1                     # call ceiling trips over_budget -> 2nd chunk never issued
    assert got == {"h1": "monetary_stock"}
