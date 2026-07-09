"""Phase-1A Task 6 — the in-flow shadow recognizer hook (flag-gated, log-only, behaviour-neutral).

The shadow hook rides inside ``build_considered_set``. It is the ONLY change to existing generation
code, and it must be behaviour-neutral by default:

* Flag OFF (default) -> ``recognize`` is NEVER called (no extra LLM call, zero behaviour change).
* Flag ON -> grounding output is IDENTICAL to a flag-off run; the recognizer only *logs* its proposed
  scope; it NEVER filters the considered set; and any error is swallowed so shadow can't break
  generation.

See ``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 6.
"""
from datetime import UTC, datetime

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import build_considered_set
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK

NOW = datetime(2026, 7, 9, tzinfo=UTC)
SHADOW_FLAG = "FEATUREGEN_INTENT_RECOGNITION_SHADOW"

# A real selectable leaf the recognizer can propose (see use_cases.py / test_recognizer.py).
CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"


def _bank_churn(db):
    # A churn-shaped catalog carrying the concept-tagged columns the retail_churn templates need to
    # ground (mirrors tests/.../contract/test_gate1.py::_bank_churn), so the two-source model
    # (templates u LLM) is exercised and there is a non-trivial grounded "templates" lens for the
    # shadow log to compare against.
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(db, "bank", rows, concepts=concepts)
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (NOW, NOW))


def _gen_script() -> dict:
    """The generation tasks build_considered_set drives (no recognizer entry)."""
    return {
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    }


def _recognizer_response() -> FakeResponse:
    return FakeResponse(output={
        "status": "classified",
        "candidates": [{
            "use_case_id": CHURN, "relationship": "primary", "confidence": "high",
            "evidence_spans": ["customers churn"], "rationale": "clear attrition intent"}],
        "ambiguity_note": None,
    })


def _gen_only_client() -> FakeLLM:
    return FakeLLM(script=_gen_script())


def _gen_plus_recognizer_client() -> FakeLLM:
    return FakeLLM(script={**_gen_script(), RECOGNIZER_TASK: _recognizer_response()})


def _shape(cs) -> list[tuple[str, tuple[str, ...]]]:
    """The grounding output as a comparable shape: each lens + its ordered feature names. This is what
    must stay IDENTICAL between a flag-off and a flag-on run (grounding unchanged)."""
    return sorted((s.lens, tuple(f.name for f in s.features)) for s in cs.alternatives)


def _build(db, client, intent):
    return build_considered_set(db, intent, client, catalog_source="bank",
                                target_ref="public.accounts.churned", now=NOW)


def test_flag_on_leaves_grounding_output_identical(db, monkeypatch):
    # Behaviour-neutral: the alternatives (lens + feature ids) with the flag ON must equal the flag-OFF
    # run. The shadow recognizer logs but never filters `cs`.
    _bank_churn(db)

    monkeypatch.delenv(SHADOW_FLAG, raising=False)
    off = _build(db, _gen_only_client(),
                 submit_intent(hypothesis=HYPOTHESIS, actor="ds1"))

    monkeypatch.setenv(SHADOW_FLAG, "1")
    on = _build(db, _gen_plus_recognizer_client(),
                submit_intent(hypothesis=HYPOTHESIS, actor="ds1"))

    assert _shape(on) == _shape(off)
    # a real templates lens grounded (so the comparison is non-trivial), and both sources are present.
    assert any(lens == "templates" and names for lens, names in _shape(on))
    assert any("avg_balance_90d" in names for _lens, names in _shape(on))


def test_flag_on_emits_a_shadow_recognition_log(db, monkeypatch, caplog):
    _bank_churn(db)
    monkeypatch.setenv(SHADOW_FLAG, "1")
    with caplog.at_level("INFO", logger="featuregen.overlay.upload.contract.gate1"):
        _build(db, _gen_plus_recognizer_client(),
               submit_intent(hypothesis=HYPOTHESIS, actor="ds1"))

    shadow_lines = [r.getMessage() for r in caplog.records
                    if "intent-recognition shadow" in r.getMessage()]
    assert shadow_lines, "expected a shadow recognition log line when the flag is on"
    # the recognised status is recorded (the FakeLLM scripted a CLASSIFIED body).
    assert any("status=classified" in line for line in shadow_lines)


def test_flag_off_does_not_call_the_recognizer(db, monkeypatch, caplog):
    # With the flag OFF and a client that has NO script for RECOGNIZER_TASK, the run still succeeds —
    # proving `recognize` is not invoked when off (an invocation would KeyError inside the recognizer's
    # dispatch). No shadow log is emitted.
    _bank_churn(db)
    monkeypatch.delenv(SHADOW_FLAG, raising=False)
    with caplog.at_level("INFO", logger="featuregen.overlay.upload.contract.gate1"):
        cs = _build(db, _gen_only_client(),
                    submit_intent(hypothesis=HYPOTHESIS, actor="ds1"))

    assert cs.alternatives          # a normal considered set
    assert not any("intent-recognition shadow" in r.getMessage() for r in caplog.records)


def test_shadow_never_breaks_generation_when_recognizer_unscripted(db, monkeypatch):
    # Flag ON but the recognizer task is UNSCRIPTED, so its dispatch raises KeyError. The recognizer is
    # fail-open (and the hook additionally swallows), so build_considered_set must STILL return a normal
    # considered set — no exception propagates.
    _bank_churn(db)
    monkeypatch.setenv(SHADOW_FLAG, "1")
    cs = _build(db, _gen_only_client(),   # generation scripted; RECOGNIZER_TASK deliberately absent
                submit_intent(hypothesis=HYPOTHESIS, actor="ds1"))

    assert cs.alternatives
    assert any(f.name == "avg_balance_90d" for s in cs.alternatives for f in s.features)
