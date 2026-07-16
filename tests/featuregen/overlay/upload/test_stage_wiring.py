"""Per-stage status wiring through ``ingest_upload`` (first-release hardening #22).

The recorder is an OPTIONAL parameter: ``stage_recorder=None`` (every pre-existing caller) is a
no-op and byte-for-byte unchanged. With a recorder, each stage records its HONEST outcome — in
particular the stages that catch per-item failures internally (concept enrichment misses, the
concept-evidence writes at enrich.py, batch discards, Pass B) must surface ``partial``/``failed``,
because the outer "ingested" was never evidence that every item succeeded. Reports are buffered
in memory only — these tests read ``recorder.reports``; the flush/GET surface is covered by the
API suites."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.stage_report import StageRecorder

_NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _rows(source: str) -> list[CanonicalRow]:
    return [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
    ]


def _states(recorder: StageRecorder) -> dict[str, str]:
    return {r.stage: r.state for r in recorder.reports}


def _report(recorder: StageRecorder, stage: str):
    return next(r for r in recorder.reports if r.stage == stage)


# ── the happy path records every stage honestly ───────────────────────────────────────────────────


def test_successful_upload_records_all_stages_in_order(db):
    _seal_config()
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW,
                        stage_recorder=rec)
    assert res.status == "ingested"
    assert [r.stage for r in rec.reports] == [
        "validation", "brake", "fact_assertion", "drift", "glossary_classification",
        "enrich_concept", "enrich_definition", "enrich_domain", "graph_persistence",
        "governed_joins", "pass_c", "pass_b", "glossary_evidence", "projection_drain",
        "table_fact_projection", "join_projection", "join_drift", "quarantine"]
    assert _states(rec) == {
        "validation": "succeeded", "brake": "succeeded", "fact_assertion": "succeeded",
        "drift": "succeeded",
        "glossary_classification": "not_applicable",           # a technical, not glossary, upload
        "enrich_concept": "skipped_no_client",                 # no LLM provider configured
        "enrich_definition": "skipped_no_client", "enrich_domain": "skipped_no_client",
        "graph_persistence": "succeeded",
        "governed_joins": "disabled", "pass_c": "disabled", "pass_b": "disabled",  # flags off
        "glossary_evidence": "not_applicable",
        "projection_drain": "succeeded", "table_fact_projection": "succeeded",
        "join_projection": "succeeded", "join_drift": "disabled",
        "quarantine": "succeeded"}
    assert _report(rec, "fact_assertion").detail == {"asserted": 2}   # grain + availability_time
    assert _report(rec, "drift").detail == {"changed_objects": 0}
    assert _report(rec, "quarantine").detail == {"rows": 0}


def test_stages_that_ran_carry_started_at(db):
    """Depth review #13 gap A: every stage that actually EXECUTED records when it began (so a
    reader can see where time went); marker records (disabled / not_applicable / skipped_no_client)
    never started and carry no start instant."""
    _seal_config()
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW,
                        stage_recorder=rec)
    assert res.status == "ingested"
    ran = {"validation", "brake", "fact_assertion", "drift", "graph_persistence",
           "projection_drain", "table_fact_projection", "join_projection", "quarantine"}
    for r in rec.reports:
        if r.stage in ran:
            assert r.started_at is not None, r.stage
            assert r.started_at <= r.completed_at, r.stage
        else:
            assert r.started_at is None, r.stage


def test_none_recorder_result_identical(db):
    """The no-op contract: a caller that passes no recorder gets EXACTLY the same IngestResult."""
    _seal_config()
    bare = ingest_upload(db, "src_a", _rows("src_a"), actor=_actor(), now=_NOW)
    rec = StageRecorder()
    recorded = ingest_upload(db, "src_b", _rows("src_b"), actor=_actor(), now=_NOW,
                             stage_recorder=rec)
    assert (bare.status, bare.reason, bare.asserted, bare.changed_objects, bare.quarantined) == \
           (recorded.status, recorded.reason, recorded.asserted, recorded.changed_objects,
            recorded.quarantined)
    assert bare.flagged.replace("src_a", "SRC") == recorded.flagged.replace("src_b", "SRC")
    assert len(rec.reports) == 18


# ── the KEY #22 case: internal per-item failures surface as partial, never "succeeded" ───────────


def test_partial_enrichment_records_partial_not_succeeded(db):
    """Two columns, ONE concept classification fails inside the stage (the empty response is
    swallowed by enrich_concepts and simply omitted from its result — today's behaviour). The
    outer ingest still succeeds; the stage report must say PARTIAL with the unresolved count."""
    _seal_config()

    class _SecondConceptFails:
        """FakeLLM whose SECOND concept call returns an empty classification (a per-item failure
        the stage swallows). FakeLLM sequences are keyed per input, so a cross-item failure needs
        this thin call counter."""

        def __init__(self):
            self._ok = FakeLLM(script={
                "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_stock"}),
                "overlay.enrich.definition": FakeResponse(output={"definition": "drafted"}),
                "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"})})
            self._bad = FakeLLM(script={
                "overlay.enrich.concept": FakeResponse(output={"concept": ""})})
            self._concept_calls = 0

        def call(self, request):
            if request.task == "overlay.enrich.concept":
                self._concept_calls += 1
                if self._concept_calls > 1:
                    return self._bad.call(request)
            return self._ok.call(request)

    client = _SecondConceptFails()
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric"),
            CanonicalRow("deposits", "accounts", "opened_on", "date")]
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", rows, actor=_actor(), now=_NOW, client=client,
                        stage_recorder=rec)
    assert res.status == "ingested"
    concept = _report(rec, "enrich_concept")
    assert concept.state == "partial"                        # NOT succeeded — 1 of 2 items failed
    assert concept.detail == {"resolved": 1, "expected": 2, "unresolved": 1}
    assert _report(rec, "enrich_definition").state == "succeeded"
    assert _report(rec, "enrich_domain").state == "succeeded"


def test_all_items_failing_records_failed(db):
    """A provider that blows up on every call: the ingest is unaffected (advisory), but the
    enrichment stages must record FAILED — not the old silent 'ingested'."""
    _seal_config()

    class _Boom:
        def call(self, request):
            raise RuntimeError("provider down")

    rec = StageRecorder()
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW,
                        client=_Boom(), stage_recorder=rec)
    assert res.status == "ingested"
    assert _report(rec, "enrich_concept").state == "failed"
    assert _report(rec, "enrich_domain").state == "failed"


def test_concept_evidence_write_failure_records_partial(db, monkeypatch):
    """The enrich.py concept-evidence seam catches per-item DB failures internally (savepoint +
    warning). That contained failure must surface: the stage reports PARTIAL with the count."""
    from featuregen.overlay.upload import enrich as enrich_mod
    from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
    from featuregen.overlay.upload.object_ref import normalize_ref
    _seal_config()

    def _broken_write(*a, **k):
        raise RuntimeError("evidence write broke")

    monkeypatch.setattr(enrich_mod, "record_field_evidence", _broken_write)
    rows = [CanonicalRow("gloss", "accounts", "balance", "unknown",
                         definition="End of day balance")]
    glossary = GlossaryUpload(rows=rows, records=[GlossaryRecord(
        logical_ref=normalize_ref("gloss", "fin", "accounts", "balance"),
        term_name="Account Balance", definition="End of day balance")])
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_stock"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Finance"})})
    rec = StageRecorder()
    res = ingest_upload(db, "gloss", rows, actor=_actor(), now=_NOW, client=client,
                        glossary=glossary, stage_recorder=rec)
    assert res.status == "ingested"                    # the contained failure never aborts ingest
    concept = _report(rec, "enrich_concept")
    assert concept.state == "partial"                  # classified, but the evidence write failed
    assert concept.detail["internal_failures"] == 1
    assert concept.detail["resolved"] == 1


def test_durable_llm_audit_degradation_flags_the_enrich_stage(db, monkeypatch):
    """#13 gap D: when the durable llm_call audit write degrades to the request connection
    (production DSN set, fresh connection refused — the enrich_llm fallback path), the enrichment
    stage that carried the call reports it: an ``audit_degraded`` count rides the stage detail
    instead of living only in a log line. The stage OUTCOME itself is untouched (the enrichment
    succeeded; only its audit durability degraded)."""
    import psycopg
    _seal_config()
    monkeypatch.setenv("FEATUREGEN_DSN", "host=degrade-marker dbname=x user=y password=z")

    real_connect = psycopg.connect

    def refuse(conninfo, *args, **kwargs):
        if "degrade-marker" in str(conninfo):
            raise RuntimeError("durable audit connection refused")
        return real_connect(conninfo, *args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", refuse)
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_stock"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "drafted"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"})})
    rec = StageRecorder()
    res = ingest_upload(db, "deposits",
                        [CanonicalRow("deposits", "accounts", "balance", "numeric")],
                        actor=_actor(), now=_NOW, client=client, stage_recorder=rec)
    assert res.status == "ingested"
    concept = _report(rec, "enrich_concept")
    assert concept.state == "succeeded"
    assert concept.detail["audit_degraded"] >= 1
    domain = _report(rec, "enrich_domain")
    assert domain.detail["audit_degraded"] >= 1


# ── early-exit paths: the stage account stays COMPLETE (#13 gap B) ────────────────────────────────

# Every stage ingest_upload owns, in execution order — what a COMPLETE run account contains.
_ALL_INGEST_STAGES = [
    "validation", "brake", "fact_assertion", "drift", "glossary_classification",
    "enrich_concept", "enrich_definition", "enrich_domain", "graph_persistence",
    "governed_joins", "pass_c", "pass_b", "glossary_evidence", "projection_drain",
    "table_fact_projection", "join_projection", "join_drift", "quarantine"]


def test_held_upload_records_not_run_for_downstream_stages(db):
    """#13 gap B: a brake-HELD upload no longer truncates the stage account — every downstream
    stage is reported ``not_run`` (reason ``skipped_upload_held``) so a reader sees "enrichment:
    not_run" instead of a missing row. Glossary stages stay ``not_applicable`` (non-glossary
    upload — never invented as not_run)."""
    _seal_config()
    ingest_upload(db, "deposits", [
        CanonicalRow("deposits", "accounts", c, "integer") for c in "abcdefgh"],
        actor=_actor(), now=_NOW)
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", [CanonicalRow("deposits", "accounts", "a", "integer")],
                        actor=_actor(), now=_NOW, stage_recorder=rec)
    assert res.status == "held"
    assert [r.stage for r in rec.reports] == _ALL_INGEST_STAGES     # complete, in canonical order
    assert [(r.stage, r.state) for r in rec.reports[:2]] == [
        ("validation", "succeeded"), ("brake", "deferred")]
    assert _report(rec, "brake").reason_code == "held"
    for r in rec.reports[2:]:
        if r.stage in ("glossary_classification", "glossary_evidence"):
            assert r.state == "not_applicable", r.stage
        else:
            assert r.state == "not_run", r.stage
            assert r.reason_code == "skipped_upload_held", r.stage
        assert r.started_at is None, r.stage                        # a not_run never started


def test_structural_error_records_validation_failed_and_not_run_downstream(db):
    _seal_config()
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", [], actor=_actor(), now=_NOW, stage_recorder=rec)
    assert res.status == "rejected"
    assert [r.stage for r in rec.reports] == _ALL_INGEST_STAGES
    assert _report(rec, "validation").state == "failed"
    assert _report(rec, "validation").reason_code == "structural_error"
    assert _report(rec, "brake").state == "not_run"
    assert _report(rec, "brake").reason_code == "skipped_rejected"
    assert _report(rec, "quarantine").state == "not_run"            # nothing was persisted


def test_all_quarantined_records_quarantine_then_not_run_downstream(db):
    _seal_config()
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", [CanonicalRow("deposits", "accounts", "id", "")],
                        actor=_actor(), now=_NOW, stage_recorder=rec)   # no type -> quarantined
    assert res.status == "rejected"
    assert [(r.stage, r.state) for r in rec.reports[:3]] == [
        ("validation", "partial"), ("brake", "succeeded"), ("quarantine", "succeeded")]
    assert _report(rec, "quarantine").detail == {"rows": 1}
    assert sorted(r.stage for r in rec.reports) == sorted(_ALL_INGEST_STAGES)
    assert _report(rec, "enrich_concept").state == "not_run"
    assert _report(rec, "enrich_concept").reason_code == "skipped_rejected"
    assert _report(rec, "graph_persistence").state == "not_run"     # the graph was NOT rebuilt


def test_glossary_early_exit_marks_glossary_stages_not_run(db):
    """A GLOSSARY upload's early exit marks the glossary stages ``not_run`` (they would have run),
    never ``not_applicable``."""
    from featuregen.overlay.upload.glossary_reader import GlossaryUpload
    _seal_config()
    rec = StageRecorder()
    res = ingest_upload(db, "gloss", [], actor=_actor(), now=_NOW,
                        glossary=GlossaryUpload(rows=[], records=[]), stage_recorder=rec)
    assert res.status == "rejected"
    assert _report(rec, "glossary_classification").state == "not_run"
    assert _report(rec, "glossary_evidence").state == "not_run"


# ── flag- and lag-dependent stages ────────────────────────────────────────────────────────────────


def test_projection_lag_records_lagged_stages(db, monkeypatch):
    from featuregen.overlay.upload import ingest as ingest_mod
    _seal_config()
    monkeypatch.setattr(ingest_mod, "projection_lag", lambda conn, name: 1)   # pretend halted
    monkeypatch.setattr(ingest_mod, "detect_catalog_changes",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW,
                        stage_recorder=rec)
    assert res.status == "ingested"
    assert _report(rec, "drift").state == "lagged"
    assert _report(rec, "drift").reason_code == "projection_lag"
    assert _report(rec, "projection_drain").state == "lagged"
    assert _report(rec, "table_fact_projection").state == "lagged"
    assert _report(rec, "join_projection").state == "lagged"


def test_pass_c_flag_on_records_succeeded(db, monkeypatch):
    _seal_config()
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW,
                        stage_recorder=rec)
    assert res.status == "ingested"
    assert _report(rec, "pass_c").state == "succeeded"
    assert _report(rec, "governed_joins").state == "succeeded"   # Pass C implies the seam
    assert _report(rec, "join_drift").state == "succeeded"


def test_pass_b_flag_on_without_client_records_skipped_no_client(db, monkeypatch):
    _seal_config()
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    rec = StageRecorder()
    res = ingest_upload(db, "deposits", _rows("deposits"), actor=_actor(), now=_NOW,
                        stage_recorder=rec)
    assert res.status == "ingested"
    assert _report(rec, "pass_b").state == "skipped_no_client"
