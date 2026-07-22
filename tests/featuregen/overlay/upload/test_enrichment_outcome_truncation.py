"""enrich-fix honest-labeling: budget/deadline-TRUNCATED items (never dispatched) must NOT be branded
``items_failed``. Two labeling lies were fixed:

  1. ``_enrichment_outcome`` marked a stage ``partial``/``items_failed`` whenever ``unresolved > 0`` —
     but items the budget/deadline cutoff skipped were NEVER ATTEMPTED, not failed. It now reports a
     DISTINCT reason ``truncated`` with the count in ``detail['not_attempted']``; ``items_failed``
     stays reserved for items actually dispatched-and-rejected.
  2. ``run_batched`` only bumped an in-memory ``budget_exhausted`` counter (never persisted). It now
     sources the never-dispatched count into the caller's ``report['not_attempted']`` so the count
     reaches the persisted stage detail.

These follow the existing ``_enrichment_outcome`` unit tests (test_passb_abstention.py) plus the
SDK-free deadline harness (test_enrich_stage_deadline.py).
"""
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich_batch as eb
from featuregen.overlay.upload.ingest import _enrichment_outcome

_CTASK = "overlay.enrich.concept"


# ---- _enrichment_outcome: the honest label -----------------------------------------------------

def test_truncated_items_report_truncated_not_items_failed():
    # expected 3, one resolved; two skipped by the budget/deadline cutoff (never dispatched).
    state, reason, detail = _enrichment_outcome({"h1": "money"}, 3, not_attempted=2)
    assert state == "partial"
    assert reason == "truncated"              # NOT items_failed — these were never tried
    assert detail["not_attempted"] == 2
    assert detail["unresolved"] == 2


def test_genuinely_rejected_item_still_reports_items_failed():
    # expected 2, one resolved, one dispatched-and-rejected (no truncation): the honest failure label.
    state, reason, detail = _enrichment_outcome({"h1": "money"}, 2)
    assert state == "partial"
    assert reason == "items_failed"
    assert "not_attempted" not in detail


def test_mixed_truncated_and_rejected_reports_items_failed_but_records_not_attempted():
    # expected 4, one resolved, one truncated, two dispatched-and-rejected → a REAL failure exists,
    # so the reason stays items_failed, but the truncation is still recorded honestly in the detail.
    state, reason, detail = _enrichment_outcome({"h1": "money"}, 4, not_attempted=1)
    assert state == "partial"
    assert reason == "items_failed"
    assert detail["not_attempted"] == 1


def test_fully_resolved_run_still_succeeds():
    state, reason, detail = _enrichment_outcome({"h1": "money", "h2": "date"}, 2)
    assert state == "succeeded" and reason is None
    assert "not_attempted" not in detail


def test_all_truncated_none_resolved_is_truncated_not_no_items_resolved():
    # nothing resolved, everything skipped by the cutoff → truncated, never the no_items_resolved lie.
    state, reason, detail = _enrichment_outcome({}, 3, not_attempted=3)
    assert state == "partial" and reason == "truncated"
    assert detail["not_attempted"] == 3


def test_not_attempted_is_clamped_to_unresolved():
    # a reused cache HIT resolves even when later chunks were cut off; not_attempted can never exceed
    # the unresolved count (defensive clamp).
    state, reason, detail = _enrichment_outcome({"h1": "money", "h2": "date"}, 3, not_attempted=5)
    assert detail["not_attempted"] == 1       # clamped to unresolved (3 - 2)
    assert reason == "truncated"


def test_internal_failures_still_items_failed_without_truncation():
    # internal (contained) failures with no truncation keep the items_failed label unchanged.
    state, reason, detail = _enrichment_outcome({"h1": "money"}, 1, internal_failures=1)
    assert state == "partial" and reason == "items_failed"
    assert "not_attempted" not in detail


# ---- run_batched: the count's source -----------------------------------------------------------

class _StepClock:
    """Deterministic monotonic clock (no sleep): returns each queued value once, then repeats the
    last — the test controls exactly when ``now() - start`` crosses the deadline."""

    def __init__(self, values):
        self._values, self._i = list(values), 0

    def __call__(self):
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


def _accept_known(raw):
    return (raw, "valid") if raw == "monetary_stock" else (None, "invalid_value")


def test_run_batched_reports_not_attempted_for_a_deadline_truncated_chunk(db, monkeypatch):
    # Two items, one per chunk → two top-level chunks; the deadline trips after chunk 1 so chunk 2
    # (h2) is never dispatched. run_batched must surface that as report['not_attempted'] == 1.
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "1")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")
    items = [eb.BatchItem("h1", {"table": "t", "column": "a", "type": "text"}),
             eb.BatchItem("h2", {"table": "t", "column": "b", "type": "text"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"}]})})
    report: dict = {}
    clock = _StepClock([0.0, 0.0, 1e9])   # chunk-2 guard sees 1e9 (≥ 100) → break before dispatch

    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None,
                         now=clock, deadline_s=100.0, report=report)

    assert got == {"h1": "monetary_stock"}    # partial result — h2 unresolved (retried next ingest)
    assert report["not_attempted"] == 1       # h2's chunk was never dispatched


def test_run_batched_all_dispatched_leaves_not_attempted_absent(db, monkeypatch):
    # No deadline: both chunks dispatch, so nothing is skipped-without-dispatch — the report carries
    # no not_attempted key (present only when non-zero).
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "1")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")
    items = [eb.BatchItem("h1", {"table": "t", "column": "a", "type": "text"}),
             eb.BatchItem("h2", {"table": "t", "column": "b", "type": "text"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"},
        {"ref": "h2", "concept": "monetary_stock"}]})})
    report: dict = {}

    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None, report=report)

    assert got == {"h1": "monetary_stock", "h2": "monetary_stock"}
    assert "not_attempted" not in report
