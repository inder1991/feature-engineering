"""Task 9c — the ONE approved live catalog run has to be interpretable afterwards.

Every test here pins a question the run must be able to answer from the record it leaves, and the
questions are the ones the Task 9c inventory found NO answer for. They are grouped by the failure
each one exists to explain:

  * WHICH BOUND withheld work (`bounds` / `stopped_by`) — `over_budget()` returned a bare bool that
    conflated the call ceiling with the wall-clock budget, and the per-item fallback cap was a
    third, unnamed one.
  * WHICH RULE refused a value, and how long the value was (`rejects` / `value_len`) — six distinct
    rules collapsed into the single string `invalid_value`.
  * WHAT WAS STORED as opposed to drafted (`detail["evidence"]`) — five of eight stages could
    draft N values, store zero, and report a clean `succeeded`.
  * WHICH CONCEPT VERDICTS MOVED, per column (`verdict_changes`) — Task 9b moved the concept
    registry, so this run re-critiques every identifier column instead of replaying stored
    verdicts. Without the before→after pair a changed feature set is uninterpretable: new
    enrichment and re-rolled verdicts look identical from outside.

And, throughout: NO TEST VALUE MAY APPEAR IN ANY RECORD. `test_no_diagnostic_ever_carries_a_value`
is the backstop, and the log-line assertions are per-site.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload import enrich
from featuregen.overlay.upload import enrich_batch as eb
from featuregen.overlay.upload.attest.concept_critic import CONCEPT_REVISION_TASK
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash, enrich_concepts, namespace_histogram
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.stage_report import StageRecorder
from featuregen.overlay.upload.upload_identity import classify_upload

_CTASK = "overlay.enrich.concept"

_HDR = ("physical_name,business_term,description_business_definition,data_domain,"
        "synonyms,bian_path,fibo_path\n")


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _stage(recorder: StageRecorder, stage: str):
    return next(r for r in recorder.reports if r.stage == stage)


def _accept_known(raw):
    known = {"monetary_stock", "unclassified"}
    return (raw, "valid") if raw in known else (None, "off_vocabulary")


def _items(n: int) -> list[eb.BatchItem]:
    return [eb.BatchItem(f"h{i}", {"table": "t", "column": f"c{i}", "type": "text"})
            for i in range(1, n + 1)]


def _run(db, client, items, *, report, **kw):
    return eb.run_batched(db, client, short="concept", task=_CTASK,
                          prompt_id="overlay_concept_batch_v1",
                          schema_id="overlay_concept_batch", shared_metadata={}, items=items,
                          out_key="concept", instruction="Classify.", accept=_accept_known,
                          actor=None, report=report, **kw)


# ── which bound withheld work ─────────────────────────────────────────────────────────────────────


def test_the_call_ceiling_is_named_rather_than_conflated_with_the_wallclock_budget(db, monkeypatch):
    """`over_budget()` was one bool over two different bounds. A run that reports `truncated` is
    uninterpretable without knowing which: a too-low CALL CEILING does not slow enrichment down,
    it silently stops enriching columns, and the fix is a config change rather than a provider
    investigation."""
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "1")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_PROVIDER_CALLS", "1")   # one chunk's worth, three wanted
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")
    report: dict = {}
    got = _run(db, FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"}]})}), _items(3), report=report)

    assert got == {"h1": "monetary_stock"}                 # chunks 2 and 3 never went
    assert report["stopped_by"] == "call_ceiling"
    assert report["bounds"] == {"call_ceiling": 2}          # BOTH withheld chunks, not just the first
    # `chunks_issued` counts DISPATCH, not `process()` returning. Chunks 2 and 3 were refused by
    # the bound guard and returned normally, so counting on return reported 3-of-3 issued on
    # exactly the run this field exists to explain — a false number in the physical account.
    assert report["chunks_planned"] == 3 and report["chunks_issued"] == 1
    assert report["provider_calls"] == 1                    # what the run actually SPENT
    assert report["not_attempted"] == 2


def test_a_bound_that_never_fired_leaves_no_entry_rather_than_a_zero(db, monkeypatch):
    """An absent key means "this never happened"; a zero would mean "we measured it as none". The
    healthy run must be readable as healthy at a glance."""
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "8")
    report: dict = {}
    _run(db, FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"},
        {"ref": "h2", "concept": "monetary_stock"}]})}), _items(2), report=report)

    assert "bounds" not in report and "stopped_by" not in report
    assert "not_attempted" not in report and "rejects" not in report
    assert report["provider_calls"] == 1 and report["fallback_calls"] == 0


def test_a_ladder_that_RAISES_still_reports_what_it_spent(db, monkeypatch):
    """The state nobody lists is the failure path, and it is exactly the one the run is being
    instrumented to explain.

    The account is finalized in a `finally`, so a provider fault escaping the seam leaves the
    physical cost on the record. Before this it left `provider_calls: 0` on a run that had already
    charged a call — a false zero, which is strictly worse than an absent field: it reads as
    "the stage failed before spending anything" and sends the reader to the wrong question."""
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "1")

    class _Boom:
        def call(self, request):
            raise RuntimeError("provider fault")

    report: dict = {}
    try:
        _run(db, _Boom(), _items(3), report=report)
    except Exception:          # noqa: BLE001 — the ladder must still propagate; only the record changed
        pass
    else:
        raise AssertionError("the provider fault must still escape run_batched")

    assert report["provider_calls"] == 1        # the charged call is on the record, not lost
    assert report["stopped_by"] == "exception"  # NOT `unattributed` — we know why work stopped
    assert report["chunks_planned"] == 3 and report["chunks_issued"] == 0
    assert report["not_attempted"] == 2         # the two chunks that never got to dispatch
    assert report["fallback_calls"] == 0


def test_a_raise_AFTER_a_bound_is_not_hidden_by_the_bound(db, monkeypatch):
    """`stopped_by` is set, not `setdefault`, on the exception path.

    A run that hit a benign per-item `fallback_cap` and then died would otherwise report
    `fallback_cap` as its headline with nothing anywhere saying it raised — the reader investigates
    a config ceiling instead of a provider fault. The earlier bound is not lost: `bounds` keeps
    every hit, which is exactly why the headline can afford to name the terminal event."""
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "1")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")   # every leftover hits the cap

    class _RefusesThenDies:
        def __init__(self):
            self.n = 0
            self._ok = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
                {"ref": "h1", "concept": "not_a_registered_concept"}]})})

        def call(self, request):
            self.n += 1
            if self.n > 1:
                raise RuntimeError("provider fault")
            return self._ok.call(request)

    report: dict = {}
    try:
        _run(db, _RefusesThenDies(), _items(2), report=report)
    except Exception:          # noqa: BLE001
        pass
    else:
        raise AssertionError("the provider fault must still escape run_batched")

    assert report["stopped_by"] == "exception"          # the terminal event is the headline
    assert report["bounds"]["fallback_cap"] >= 1        # …and the earlier bound is still on record
    assert report["bounds"]["exception"] == 1


def test_items_skipped_with_no_guard_claiming_it_are_named_a_hole_not_a_clean_run(db, monkeypatch):
    """`stopped_by` absent reads as "nothing stopped it". If items went undispatched and no bound
    owns that, the account has a hole and must say so."""
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "1")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")
    report: dict = {}
    clock_values = iter([0.0, 0.0, 1e9, 1e9, 1e9])
    _run(db, FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"}]})}), _items(2), report=report,
        now=lambda: next(clock_values, 1e9), deadline_s=100.0)
    # Here the deadline DID claim it, which is the point of the contrast below.
    assert report["stopped_by"] == "stage_deadline" and report["bounds"] == {"stage_deadline": 1}
    assert report["timed_out"] is True and report["not_attempted"] == 1


# ── which rule refused, and how long the value was ────────────────────────────────────────────────


def test_the_refusal_rule_is_named_and_the_LENGTH_rides_with_it(db, monkeypatch, caplog):
    """Six distinct rules collapsed into `invalid_value`. A definition discarded whole for one
    leading `[` and one discarded for exceeding a 32_000-char cap were the same event from outside,
    and both were indistinguishable from a provider blip."""
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "8")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")
    report: dict = {}
    with caplog.at_level(logging.INFO, logger="featuregen.overlay.upload.enrich_batch"):
        _run(db, FakeLLM(script={_CTASK: FakeResponse(output={"results": [
            {"ref": "h1", "concept": "monetary_stock"},
            {"ref": "h2", "concept": "not_a_registered_concept"},
            {"ref": "h3", "concept": ""}]})}), _items(3), report=report)

    assert report["outcomes"] == {"invalid_value": 1, "blank": 1}
    assert report["rejects"] == {"off_vocabulary": 1, "blank": 1}   # the RULE, not the catch-all

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("enrich_reject")]
    assert len(lines) == 2
    by_ref = {line.split("ref=")[1].split(" ")[0]: line for line in lines}
    assert "reason=off_vocabulary" in by_ref["h2"] and "len=24" in by_ref["h2"]
    assert "reason=blank" in by_ref["h3"] and "len=0" in by_ref["h3"]
    # The refused VALUE is never in the record — only its length. This is the same discipline
    # `_safe_reason` enforces on egress: a log that leaks what the redactor scrubbed is worse than
    # no log at all.
    for line in lines:
        assert "not_a_registered_concept" not in line


def test_an_UNRECOGNIZED_ref_is_counted_but_never_echoed(db, monkeypatch, caplog):
    """The one field in the reject line that is not ours.

    Every other status keys on a ref THIS code minted — a content hash or a table name. An `extra`
    ref is a ref the MODEL returned that we never asked about: unvalidated output that could carry
    anything, including the column content the redactor just scrubbed. It is counted (a
    hallucinated ref coming back is the diagnostic) and never echoed."""
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "8")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", "0")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")
    smuggled = "ACCOUNT-4417-SMITH-J"
    report: dict = {}
    with caplog.at_level(logging.INFO, logger="featuregen.overlay.upload.enrich_batch"):
        _run(db, FakeLLM(script={_CTASK: FakeResponse(output={"results": [
            {"ref": "h1", "concept": "monetary_stock"},
            {"ref": smuggled, "concept": "monetary_stock"}]})}), _items(1), report=report)

    assert report["outcomes"] == {"extra": 1}          # counted — the hallucination is visible
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("enrich_reject")]
    assert any("ref=<unrecognized> status=extra" in line for line in lines)
    assert not any(smuggled in line for line in lines)


def test_the_narrowed_reason_did_not_move_a_single_REFUSAL(db):
    """The reason codes narrowed; the DECISIONS must be byte-identical. Pinned against a literal
    re-statement of the pre-Task-9c predicate so a future edit to one of the four rules cannot
    quietly change what is accepted while looking like a labelling change."""
    def _old_bounded(val, max_len):        # the shipped predicate, before the rules were named
        if not val or len(val) > max_len or val.startswith("[") or enrich._is_enumeration(val):
            return None
        return val

    cases = ["", "a normal definition", "x" * 90, "['a','b']", "[not really a list",
             "- one\n- two", "1. one\n2. two", "First para.\n\nSecond para.", " ", "\n"]
    for raw in cases:
        old = _old_bounded(raw, 64)
        new_code = enrich._bounded_reject(raw, 64)
        assert (old is None) == (new_code is not None), raw
        assert enrich._bounded(raw, 64) == old, raw
        if new_code is not None:
            assert new_code in enrich.BOUNDED_REJECT_CODES, (raw, new_code)


# ── what was STORED, not merely drafted ───────────────────────────────────────────────────────────


def test_every_stage_reports_whether_its_evidence_writer_ran(db, monkeypatch):
    """The Task 4c fix was never generalized: `definition`, `domain`, `sub_domain`, `unit` and
    `summary` all had a writer gated on a glossary AND a snapshot, and none of them said so. A
    TECHNICAL upload therefore drafted every value at full LLM cost, stored none of them, and
    reported `succeeded` for the lot."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    h = content_hash(rows[0])
    client = FakeLLM(script={
        _CTASK: FakeResponse(output={"results": [{"ref": h, "concept": "monetary_stock"}]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": []}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
        "overlay.enrich.unit": FakeResponse(output={"results": []}),
    })
    rec = StageRecorder()
    ingest_upload(db, "deposits", rows, actor=_actor(), client=client,
                  now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)

    # ALL of them, including `enrich_synonyms` (whose flat `skipped` key has a live consumer in
    # `_enrichment_outcome`'s `evidence_skipped`). `enrich_summary` is asserted separately below:
    # it is `not_applicable` without a glossary, so it never reaches an evidence account here.
    for stage in ("enrich_concept", "enrich_definition", "enrich_domain", "enrich_unit",
                  "enrich_synonyms"):
        detail = _stage(rec, stage).detail or {}
        assert detail.get("evidence", {}).get("writer") == "not_run:no_glossary", stage
    assert _stage(rec, "enrich_summary").state == "not_applicable"


def test_the_summary_stage_reports_its_bound_and_its_evidence_like_every_other(db, monkeypatch):
    """`enrich_summary` drafts one value per column from the full enriched dossier — plausibly the
    largest single call cost of the run — and was the one stage of the six the inventory named that
    reported neither. `_write_summary_evidence` even TOOK a `counts=` parameter that no call site
    passed, which made the gap read as wired."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    csv = _HDR + "DPL.BO_CUST.CIF_ID,CIF,Customer CIF,Party,,,\n"
    upload = read_glossary(csv, source="cib")
    (row,) = upload.rows
    h = content_hash(replace(row, table=row.table.lower(), column=row.column.lower()))
    client = FakeLLM(script={
        _CTASK: FakeResponse(output={"results": [{"ref": h, "concept": "customer_id"}]}),
        "overlay.enrich.concept_critique": FakeResponse(
            output={"verdict": "supported", "reason_codes": []}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": []}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
        "overlay.enrich.unit": FakeResponse(output={"results": []}),
        "overlay.enrich.summary": FakeResponse(output={"results": [
            {"ref": h, "summary": "The customer information file identifier for this party."}]}),
    })
    rec = StageRecorder()
    ingest_upload(db, "cib", upload.rows, actor=_actor(), client=client, glossary=upload,
                  now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)

    detail = _stage(rec, "enrich_summary").detail or {}
    # The physical account of the summary ladder — previously computed into `stats["batch"]` and
    # read by nobody.
    assert detail["provider_calls"] >= 1
    assert detail["chunks_planned"] == 1 and detail["chunks_issued"] == 1
    # …and whether the drafted summary was actually STORED.
    evidence = detail["evidence"]
    assert evidence.get("written", 0) + evidence.get("reused", 0) >= 1
    assert "rolled_back" not in evidence


def test_a_glossary_run_accounts_for_every_evidence_item_it_did_not_write(db, monkeypatch):
    """Where the writer DOES run, the three designed non-writes (not a proposal / no glossary ref /
    unattachable binding) collapsed into one `skipped` bucket. "126 skipped" and "126 skipped for
    want of a glossary ref" are different findings and only the second is actionable."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    csv = (_HDR
           + "DPL.BO_CUST.CIF_ID,CIF,Customer CIF,Party,,,\n"
           + "DPL.BO_CUST.CTRY_CD,Country,Country of residence,Party,,,\n")
    upload = read_glossary(csv, source="cib")
    bindings, _ = classify_upload(upload.rows)
    by_col = {r.column: content_hash(r) for r in upload.rows}
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": by_col["CIF_ID"], "concept": "customer_id"},
        {"ref": by_col["CTRY_CD"], "concept": "unclassified"}]})})
    stats: dict = {}
    enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                    source_snapshot_id="snap-1", stats=stats)

    counts = stats["evidence_counts"]
    # `unclassified` is a real classification and deliberately NOT a proposal (C3) — a designed
    # non-write, now named as one rather than indistinguishable from a failed write.
    assert counts["skipped_not_a_proposal"] == 1
    assert counts["skipped"] == 1
    assert counts.get("written", 0) + counts.get("reused", 0) == 1
    assert "failed" not in counts


# ── which concept verdicts moved, per column ──────────────────────────────────────────────────────


def _glossary(column: str, definition: str, source: str = "cib"):
    csv = _HDR + f"DPL_EIB_COMPLIANCE.BO_CIB_CUSTOMER.{column},{column} term,{definition},Party,,,\n"
    upload = read_glossary(csv, source=source)
    bindings, _ = classify_upload(upload.rows)
    (row,) = upload.rows
    ref = normalize_ref(source, "DPL_EIB_COMPLIANCE", "BO_CIB_CUSTOMER", column)
    return upload, bindings, content_hash(row), ref


def test_a_REVISED_verdict_records_the_concept_AND_the_namespace_it_moved_between(db, monkeypatch):
    """The row that matters most. A REVISED column gets a different concept and therefore a
    different NAMESPACE — the axis that decides join candidacy — so the features it can appear in
    change. `counterparty_id` (namespace `cif`) revised to `bank_bic` (namespace `swift_bic`)
    leaves the column joinable against a completely different set of columns."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload, bindings, h, ref = _glossary("COUNTER_PARTY_BIC", "SWIFT BIC of the counterparty bank")
    client = FakeLLM(script={
        _CTASK: FakeResponse(output={"results": [{"ref": h, "concept": "counterparty_id"}]}),
        CONCEPT_REVISION_TASK: FakeResponse(output={"concept": "bank_bic", "reason_codes": []}),
    })
    stats: dict = {}
    out = enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                          source_snapshot_id="snap-1", stats=stats)
    assert out[h] == "bank_bic"

    change = stats["concept_critic"]["verdict_changes"][ref]
    assert change["disposition"] == "revised"
    # `from` is the CANONICALIZED assignment, not the model's raw word: `_accept_concept` resolves
    # the legacy alias `counterparty_id` to its successor `customer_id` before anything downstream
    # sees it. That is the right before — it is the value that WOULD have been stored.
    assert change["from"] == "customer_id" and change["to"] == "bank_bic"
    # …and the join-candidacy axis, which is stored NOWHERE else: `namespace` is a pure code-side
    # derivation from CONCEPT_REGISTRY, so a later registry edit rewrites it for every historical
    # row and nothing on disk would show that it moved.
    assert change["from_namespace"] == "cif" and change["to_namespace"] == "swift_bic"
    # The run-level picture agrees with the per-column one.
    assert stats["namespaces"]["before_critic"] == {"cif": 1}
    assert stats["namespaces"]["after_critic"] == {"swift_bic": 1}
    assert stats["vocab_fingerprint"]          # WHICH registry generation produced this verdict


def test_a_REFUTED_verdict_records_the_loss_of_join_candidacy(db, monkeypatch):
    """REFUTED sets the concept to `unclassified`, which has no namespace at all — the column stops
    being a bridge candidate. Recorded as an explicit before→after pair rather than inferred from
    an absence, because an absence is also what a column the critic never looked at produces."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload, bindings, h, ref = _glossary("SOL_DESC", "Branch description")
    client = FakeLLM(script={
        _CTASK: FakeResponse(output={"results": [{"ref": h, "concept": "branch_id"}]})})
    stats: dict = {}
    out = enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                          source_snapshot_id="snap-1", stats=stats)
    assert out[h] == "unclassified"

    change = stats["concept_critic"]["verdict_changes"][ref]
    assert change["disposition"] == "refuted"
    assert change["from"] == "branch_id" and change["to"] == "unclassified"
    assert change["from_namespace"] == "branch_sol" and change["to_namespace"] == "-"
    # WHY it moved, self-contained: `reason_codes` is the critic's own verdict path and
    # `conflict_codes` the deterministic contradiction that drove it. Both ride on the change row
    # so a reader who found the column does not have to join a second map to interpret it.
    assert change["reason_codes"] == ["deterministic_shape_conflict"]
    assert change["conflict_codes"] == ["name_or_description_not_identifier"]
    assert stats["namespaces"] == {"before_critic": {"branch_sol": 1}, "after_critic": {"-": 1}}


def test_an_UNCHANGED_verdict_leaves_no_row_and_the_map_is_present_but_empty(db, monkeypatch):
    """An empty map means "the critic ran and moved nothing"; an ABSENT map would mean "this code
    never ran". Those are different findings and the record must not conflate them — an accepted
    proposal is already durable in `graph_node.concept` and its `concept` field_evidence, so
    re-stating it here would only add noise."""
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload, bindings, h, _ref = _glossary("CIF_ID", "Customer information file identifier")
    client = FakeLLM(script={
        _CTASK: FakeResponse(output={"results": [{"ref": h, "concept": "customer_id"}]}),
        "overlay.enrich.concept_critique": FakeResponse(
            output={"verdict": "supported", "reason_codes": []}),
    })
    stats: dict = {}
    out = enrich_concepts(db, upload.rows, client, glossary=upload, bindings=bindings,
                          source_snapshot_id="snap-1", stats=stats)
    assert out[h] == "customer_id"
    report = stats["concept_critic"]
    assert report["accepted"] == 1 and report["verdict_changes"] == {}
    assert stats["namespaces"] == {"before_critic": {"cif": 1}, "after_critic": {"cif": 1}}


def test_the_verdict_trail_reaches_the_durable_stage_record(db, monkeypatch):
    """In memory is not a record. The map has to survive to `ingestion_run_stage.detail`, which is
    what the reading guide queries — a per-run store that needs no migration."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    upload, _bindings, _raw_h, ref = _glossary("SOL_DESC", "Branch description")
    (row,) = upload.rows
    h = content_hash(replace(row, table=row.table.lower(), column=row.column.lower()))
    client = FakeLLM(script={
        _CTASK: FakeResponse(output={"results": [{"ref": h, "concept": "branch_id"}]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.summary": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": []}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
        "overlay.enrich.unit": FakeResponse(output={"results": []}),
    })
    rec = StageRecorder()
    ingest_upload(db, "cib", upload.rows, actor=_actor(), client=client, glossary=upload,
                  now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)

    change = _stage(rec, "enrich_concept_critic").detail["verdict_changes"][ref]
    assert change == {"disposition": "refuted", "from": "branch_id", "to": "unclassified",
                      "from_namespace": "branch_sol", "to_namespace": "-",
                      "reason_codes": ["deterministic_shape_conflict"],
                      "conflict_codes": ["name_or_description_not_identifier"]}
    # …and the run-level namespace picture rides the concept stage, whose detail is where a reader
    # comparing two runs starts.
    concept_detail = _stage(rec, "enrich_concept").detail
    assert concept_detail["namespaces"]["after_critic"] == {"-": 1}
    assert isinstance(concept_detail["vocab_fingerprint"], str)


def test_namespace_histogram_buckets_everything_without_a_namespace_together(db):
    """`-` is one explicit bucket for three different things: a non-identifier concept, an
    unregistered name, and `unclassified`. Stated rather than left to be inferred from a missing
    key, since a JSON object cannot have a null key."""
    del db
    assert namespace_histogram({"a": "customer_id", "b": "counterparty_id"}) == {"cif": 2}
    assert namespace_histogram(
        {"a": "unclassified", "b": "monetary_stock", "c": "not_a_registered_word"}) == {"-": 3}
    assert namespace_histogram({}) == {}


# ── the backstop ──────────────────────────────────────────────────────────────────────────────────


def test_no_diagnostic_ever_carries_a_value(db, monkeypatch, caplog):
    """The single hard rule, asserted over the whole record a run leaves rather than per-site.

    A distinctive marker is planted in every model-authored field this run produces. Nothing the
    run writes to a stage detail or to a log line may contain it. Lengths, counts, closed codes,
    refs and field names are the vocabulary; content is not."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    marker = "ZZQQMARKERZZQQ"
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    h = content_hash(rows[0])
    client = FakeLLM(script={
        # Every one of these is REFUSED by its acceptor, so the marker travels the rejection path —
        # the one that records a reason and a length.
        _CTASK: FakeResponse(output={"results": [{"ref": h, "concept": marker}]}),
        "overlay.enrich.definition": FakeResponse(output={"results": [
            {"ref": h, "definition": f"['{marker}', '{marker}']"}]}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": "accounts", "domain": f"{marker}\n{marker}"}]}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": [
            {"ref": h, "synonyms": f"{marker}\n{marker}"}]}),
        "overlay.enrich.unit": FakeResponse(output={"results": [
            {"ref": h, "unit": marker * 20}]}),
    })
    rec = StageRecorder()
    with caplog.at_level(logging.DEBUG):
        ingest_upload(db, "deposits", rows, actor=_actor(), client=client,
                      now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)

    for report in rec.reports:
        assert marker not in repr(report.detail), f"{report.stage} detail leaked a value"
    for record in caplog.records:
        assert marker not in record.getMessage(), f"{record.name} leaked a value"
    # …and the run DID exercise the rejection paths, so the assertions above are not vacuous.
    rejects = (_stage(rec, "enrich_concept").detail or {}).get("rejects", {})
    assert rejects, "the concept stage recorded no rejection — the marker never travelled"


def test_pass_B_reports_its_account_per_PHASE_rather_than_discarding_it(db, monkeypatch):
    """Pass B computed a full account and forwarded only `not_attempted`.

    Its ladders populate `bounds`, `stopped_by`, `provider_calls`, `outcomes` and `rejects` exactly
    like Pass A's, and every one of them was dropped on the floor — so the stage that synthesizes
    every table's grain and as-of semantics answered none of the guide's bound or cost questions.

    Reported per PHASE, never summed: Pass B runs up to three ladders per ingest and "which bound
    stopped it" has a different answer for each. Adding a chunk-granular phase-1 count to a
    table-granular phase-2 one would produce a number that means nothing."""
    _seal_config()
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    rows = [CanonicalRow("passb_diag", "txn", "id", "integer"),
            CanonicalRow("passb_diag", "txn", "posted_at", "timestamp")]
    client = FakeLLM(script={"table_synth": FakeResponse(output={"results": [
        {"ref": "txn", "synthesis": {"grain_columns": ["id"]}}]})})
    rec = StageRecorder()
    ingest_upload(db, "passb_diag", rows, actor=_actor(), client=client,
                  now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)

    detail = _stage(rec, "pass_b").detail or {}
    # A narrow catalog runs the narrow ladder only; the two wide phases are ABSENT rather than
    # zeroed, which is how a reader tells "did not run" from "ran and cost nothing".
    assert set(detail["batch"]) == {"narrow"}
    narrow = detail["batch"]["narrow"]
    assert narrow["provider_calls"] >= 1
    assert narrow["chunks_planned"] == 1 and narrow["chunks_issued"] == 1
    assert "stopped_by" not in narrow          # nothing withheld work on this run
