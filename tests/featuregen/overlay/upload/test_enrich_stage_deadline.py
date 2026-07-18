"""MF-4: `run_batched` must honor a stage-level wall-clock deadline so a slow provider can't hold the
source advisory lock across the whole catalog ingest. These tests are SDK-FREE and DETERMINISTIC —
a FakeLLM plus an INJECTED monotonic clock (never real sleep) that jumps past the deadline after the
first chunk, proving later chunks are never dispatched and the batch report is marked ``timed_out``,
while the run returns a PARTIAL dict WITHOUT raising (so the ingest stage degrades, facts still hold).
"""
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich_batch as eb
from featuregen.overlay.upload import enrich_config as cfg

_CTASK = "overlay.enrich.concept"


def _accept_known(raw):
    known = {"monetary_stock", "unclassified"}
    return (raw, "valid") if raw in known else (None, "invalid_value")


class _StepClock:
    """Deterministic monotonic clock: returns each queued value once, then repeats the last. No sleep,
    no real time — the test controls exactly when ``now() - start`` crosses the deadline."""

    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def __call__(self):
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


class _Counting:
    """Wraps an LLMClient and counts `.call` invocations — proves a later chunk's provider call is
    never issued once the deadline trips."""

    def __init__(self, inner):
        self.inner, self.n = inner, 0

    def call(self, request):
        self.n += 1
        return self.inner.call(request)


def _two_singleton_chunks(monkeypatch):
    # Two items, one item per chunk (max_items=1) → two top-level chunks. No retry/fallback so each
    # dispatched chunk costs EXACTLY one provider call — a clean way to detect a skipped chunk.
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "1")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")
    return [eb.BatchItem("h1", {"table": "t", "column": "a", "type": "text"}),
            eb.BatchItem("h2", {"table": "t", "column": "b", "type": "text"})]


def test_stage_deadline_stops_issuing_chunks_and_marks_timed_out(db, monkeypatch):
    items = _two_singleton_chunks(monkeypatch)
    client = _Counting(FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"}]})}))
    report: dict = {}
    # start=0.0; chunk-1 guard sees 0.0 (< 100 → dispatch); chunk-2 guard sees 1e9 (≥ 100 → break).
    clock = _StepClock([0.0, 0.0, 1e9])

    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None,
                         now=clock, deadline_s=100.0, report=report)

    assert client.n == 1                 # only chunk 1 dispatched; chunk 2 never issued past deadline
    assert got == {"h1": "monetary_stock"}   # partial result — h2 left unresolved (retried next ingest)
    assert "h2" not in got
    assert report.get("timed_out") is True   # the batch report carries the timed_out marker
    # No exception escaped: the stage degrades to partial and ingestion still commits (isolation #4).


def test_no_deadline_dispatches_all_chunks_byte_for_byte(db, monkeypatch):
    # deadline_s=None (today's default) is fully inert: BOTH chunks dispatch, no timed_out marker.
    items = _two_singleton_chunks(monkeypatch)
    client = _Counting(FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"},
        {"ref": "h2", "concept": "monetary_stock"}]})}))
    report: dict = {}

    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None, report=report)

    assert client.n == 2                 # both chunks dispatched — no deadline gate
    assert got == {"h1": "monetary_stock", "h2": "monetary_stock"}
    assert "timed_out" not in report


def test_stage_deadline_config_default_and_env(monkeypatch):
    # SDK-free config knob: default 240s stage ceiling; OVERLAY_ENRICH_STAGE_DEADLINE_S overrides.
    monkeypatch.delenv("OVERLAY_ENRICH_STAGE_DEADLINE_S", raising=False)
    assert cfg.stage_deadline_s() == 240.0
    monkeypatch.setenv("OVERLAY_ENRICH_STAGE_DEADLINE_S", "30")
    assert cfg.stage_deadline_s() == 30.0
