"""E1a Task 1: the field-parameterised ``llm/proposed`` evidence writer.
E1a Task 2: the AI-drafted ``definition`` promoted through it into governed evidence.

Proves the four properties the writer exists for:
  * a plain field write lands as ``producer=llm`` / ``strength=proposed`` evidence;
  * re-enrichment SUPERSEDES the prior LLM row (no reuse/cache — stale-all, then write fresh), so
    exactly one active row survives;
  * the TWO IDENTITIES stay separate — attachability is checked at the PUBLIC-flattened binding ref
    while the evidence is stored at the SCHEMA-preserving ref (collapsing them silently skips every
    non-``public``-schema column);
  * reconciliation retires prior AI evidence for a deliberately-withheld target but KEEPS it for a
    transient failure;
  * fail-soft is per ITEM and wraps the WHOLE body — a throwing ``ref_of`` counts one failure and
    the item AFTER it is still written; a SKIP (invalid value / no ref / unattachable) writes
    nothing and is NOT counted as a failure.

Task 2 adds the end-to-end proof: a glossary upload whose column declares NO definition gets one
drafted by the LLM, and that draft is now GOVERNED ``llm/proposed`` evidence that asset-detail
renders as "AI proposed" — plus the honest stage report (``partial``/``items_failed``) when an
evidence write fails, so lost metadata is never laundered into a success.

Task 2b closes the reconciliation gap: the stage reconciles the whole TARGET UNIVERSE by DISPOSITION,
not just the successes — a column whose definition becomes sanitizer-SUPPRESSED, or that stops being
an AI target at all, has its prior AI definition RETIRED; a transient miss KEEPS it; and a ref
written this run is never in the retire set.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_ftr_adapter import _HDR, _row

import featuregen.overlay.upload.enrich as enrich_mod
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.object_identity import ObjectBinding, ObjectIdentityStatus
from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import (
    _reconcile_llm_field_evidence,
    _write_definition_evidence,
    _write_llm_field_evidence,
    content_hash,
)
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.stage_report import StageRecorder
from featuregen.overlay.upload.upload_identity import classify_upload


def test_writes_generic_llm_proposed_evidence(overlay_conn):
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "notional", "numeric")])
    ref = f"{src}::public.trades.notional"
    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h1": "Notional amount of the trade"},
        ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    assert n == 0                      # zero failures
    ev = read_active_field_evidence(overlay_conn, ref, "definition")
    assert ev[0].producer == "llm" and ev[0].strength == "proposed"
    assert ev[0].proposed_value == "Notional amount of the trade"
    assert ev[0].producer_ref == "overlay-enrichment" and ev[0].producer_item_ref == "h1"


def test_reenrich_supersedes_prior_llm_evidence(overlay_conn):
    """Re-enrichment supersedes prior LLM evidence -> exactly one active row with the new value
    (no reuse optimization; the writer stales-all then writes fresh)."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "ccy", "text")])
    ref = f"{src}::public.trades.ccy"
    _write_llm_field_evidence(overlay_conn, field_name="definition",
                              items={"h": "Currency of the trade"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _write_llm_field_evidence(overlay_conn, field_name="definition",
                              items={"h": "Settlement currency"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    active = read_active_field_evidence(overlay_conn, ref, "definition")
    assert len(active) == 1 and active[0].proposed_value == "Settlement currency"


def test_binding_checked_at_public_ref_stored_at_schema_ref(overlay_conn):
    """The regression guard for the two-identity bug: a non-public-schema column must NOT be skipped.
    The binding lives under the PUBLIC-flattened ref (that is how ``classify_upload`` keys a
    CanonicalRow, which carries no schema); the evidence must store under the SCHEMA-preserving ref."""
    src = "ftr"
    row = CanonicalRow(src, "accounts", "balance", "numeric")
    build_graph(overlay_conn, src, [row], schemas={"public.accounts.balance": "dpl_eib_compliance"})
    ev_ref = normalize_ref(src, "dpl_eib_compliance", "accounts", "balance")  # schema-preserving
    bind_ref = normalize_ref(src, None, "accounts", "balance")                # public-flattened
    assert ev_ref != bind_ref
    bindings, _ = classify_upload([row])          # the REAL classify path: EXACT, keyed by bind_ref
    assert bind_ref in bindings

    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h": "Account balance"},
        ref_of=lambda k: (ev_ref, bind_ref, {"k": k}), source_snapshot_id="snap",
        bindings=bindings)

    assert n == 0
    # Stored under the SCHEMA ref, NOT skipped (the bug would write nothing here):
    assert read_active_field_evidence(overlay_conn, ev_ref, "definition")
    assert not read_active_field_evidence(overlay_conn, bind_ref, "definition")


def test_withheld_target_retires_prior_ai(overlay_conn):
    """A column that HAD AI evidence but is deliberately withheld this run -> prior AI is RETIRED."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "note", "text")])
    ref = f"{src}::public.trades.note"
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "old AI def"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _reconcile_llm_field_evidence(overlay_conn, field_name="definition", retire_refs={ref})
    assert read_active_field_evidence(overlay_conn, ref, "definition") == []


def test_transient_failure_keeps_prior_ai(overlay_conn):
    """A transient failure must NOT retire prior AI (its ref is excluded from ``retire_refs``)."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "memo", "text")])
    ref = f"{src}::public.trades.memo"
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "keep me"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _reconcile_llm_field_evidence(overlay_conn, field_name="definition", retire_refs=set())
    assert read_active_field_evidence(overlay_conn, ref, "definition")


def test_fail_soft_item_after_failure_still_written(overlay_conn):
    """A ``ref_of`` that THROWS for one key must not abort the batch; the item after it is written."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "good", "text")])
    good = f"{src}::public.trades.good"

    def ref_of(k):
        if k == "bad":
            raise ValueError("boom")     # throws where the OLD narrow try wouldn't catch it
        return (good, good, {"k": k})

    n = _write_llm_field_evidence(overlay_conn, field_name="definition",
                                  items={"bad": "x", "good": "kept"},   # dict order: bad first
                                  ref_of=ref_of, source_snapshot_id="snap")
    assert n == 1                                        # one failure counted, not raised
    assert read_active_field_evidence(overlay_conn, good, "definition")


# ── SKIPS are not FAILURES: an item the writer deliberately declines writes no row and must not
# inflate the failure count (which would report the stage `partial` for a correct run). ──


def test_skips_write_nothing_and_are_not_failures(overlay_conn):
    """An invalid value, a ``ref_of`` that returns None, a MISSING binding and an UNATTACHABLE
    binding are all deliberate skips: no evidence row, and ``failures == 0``."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "notional", "numeric")])
    ref = f"{src}::public.trades.notional"

    # 1) valid_fn rejects the value
    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h": "   "},
        ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap",
        valid_fn=lambda v: bool(v and v.strip()))
    assert n == 0
    assert read_active_field_evidence(overlay_conn, ref, "definition") == []

    # 2) ref_of declines the item
    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h": "a definition"},
        ref_of=lambda k: None, source_snapshot_id="snap")
    assert n == 0
    assert read_active_field_evidence(overlay_conn, ref, "definition") == []

    # 3) bindings supplied but this column has none
    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h": "a definition"},
        ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap", bindings={})
    assert n == 0
    assert read_active_field_evidence(overlay_conn, ref, "definition") == []

    # 4) the binding exists but is UNRESOLVED -> not attachable
    unattachable = {ref: ObjectBinding(logical_ref=None,
                                       status=ObjectIdentityStatus.UNRESOLVED, candidates=())}
    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h": "a definition"},
        ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap", bindings=unattachable)
    assert n == 0
    assert read_active_field_evidence(overlay_conn, ref, "definition") == []


def test_producer_configuration_hash_is_persisted(overlay_conn):
    """The writer's ``producer_configuration_hash`` reaches the row (it is what ties a proposal to
    the producer config that made it); ``None`` (the definition writer) stays NULL."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "ccy", "text")])
    ref = f"{src}::public.trades.ccy"
    _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h": "Settlement currency"},
        ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap",
        producer_configuration_hash="cfg-v7")
    assert read_active_field_evidence(
        overlay_conn, ref, "definition")[0].producer_configuration_hash == "cfg-v7"


# ── E1a Task 2: the AI definition end-to-end (upload -> AI draft -> governed evidence -> UI) ──

_NOW = datetime(2026, 7, 27, tzinfo=UTC)
_AI_SOURCE = "ftr_ai"
# The FTR glossary fixture, with the definition cell EMPTY — the blank the AI drafter targets.
# The FTR adapter (not the generic reader) is the real path: it preserves the declared schema, so
# the graph node carries `schema_name` and asset-detail resolves the SCHEMA-preserving logical_ref
# the evidence is keyed under.
_BLANK_DEF_CSV = _HDR + _row(definition="")
# The SAME column, re-uploaded with a definition the sanitizer blanks fail-closed (an unhandled
# sample-value marker) -> `definition_suppressed`: blank, but DELIBERATELY withheld, not missing.
_SUPPRESSED_DEF_CSV = _HDR + _row(definition="representative values")
# The same column with a real uploader-declared definition -> no longer an AI target at all.
_DECLARED_DEF_CSV = _HDR + _row()
_NAME_OBJECT_REF = "public.comp_fin_tran.cust_name"
_NAME_REF = normalize_ref(_AI_SOURCE, "DPL_EIB_COMPLIANCE", "COMP_FIN_TRAN", "CUST_NAME")
_ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform_admin",))


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _drafting_client(row: CanonicalRow, definition: str) -> FakeLLM:
    """A FakeLLM scripted for the three Pass A batch stages; the definition task drafts ``definition``
    for the (single) blank column. ``validate_rows`` NORMALIZES row identity before enrichment, so
    the batch item ref is the hash of the normalized row, not the raw-cased one the adapter emitted."""
    h = content_hash(replace(row, table=row.table.lower(), column=row.column.lower()))
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(
            output={"results": [{"ref": h, "concept": "customer_identifier"}]}),
        "overlay.enrich.definition": FakeResponse(
            output={"results": [{"ref": h, "definition": definition}]}),
        "overlay.enrich.domain": FakeResponse(
            output={"results": [{"ref": "comp_fin_tran", "domain": "Party"}]}),
    })


def test_ai_definition_becomes_governed_evidence(db):
    """THE E1a feature, end-to-end: a blank glossary definition is drafted by the LLM and lands as
    ``llm/proposed`` ``definition`` evidence at the SCHEMA-preserving ref, which asset-detail
    renders as "AI proposed" — no longer a display-only ``graph_node.definition``."""
    _seal()
    upload = to_glossary_upload(read_ftr_glossary(_BLANK_DEF_CSV, source=_AI_SOURCE))
    (row,) = upload.rows
    assert not row.definition                     # the uploader declared none — the AI's target
    client = _drafting_client(row, "Customer full legal name")

    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=_NOW, client=client,
                        glossary=upload)
    assert res.status == "ingested"

    ev = read_active_field_evidence(db, _NAME_REF, "definition")
    assert len(ev) == 1
    assert ev[0].producer == "llm" and ev[0].strength == "proposed"
    assert ev[0].proposed_value == "Customer full legal name"

    field = build_asset_detail(
        db, source=_AI_SOURCE, object_ref=_NAME_OBJECT_REF, roles=list(_ADMIN.role_claims),
        identity=_ADMIN, include=["effective_metadata"])["effective_metadata"]["fields"]["definition"]
    assert field["evidence_provenance"] == "AI proposed"
    assert field["value"] == "Customer full legal name"


def test_definition_evidence_write_failure_records_partial(db, monkeypatch):
    """FAILURE PROPAGATION: the definition stage wrote nothing but had work to do — the contained
    evidence-write failure must surface as ``partial``/``items_failed``, never a laundered
    ``succeeded`` (losing the metadata while reporting success is the bug this prevents)."""
    _seal()
    upload = to_glossary_upload(read_ftr_glossary(_BLANK_DEF_CSV, source=_AI_SOURCE))
    client = _drafting_client(upload.rows[0], "Customer full legal name")

    def _boom(*a, **k):
        raise RuntimeError("field_evidence store unavailable")

    monkeypatch.setattr(enrich_mod, "record_field_evidence", _boom)
    rec = StageRecorder()
    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=_NOW, client=client,
                        glossary=upload, stage_recorder=rec)
    assert res.status == "ingested"               # the contained failure never aborts the upload

    report = next(r for r in rec.reports if r.stage == "enrich_definition")
    assert report.state == "partial" and report.reason_code == "items_failed"
    assert report.detail["internal_failures"] == 1
    assert read_active_field_evidence(db, _NAME_REF, "definition") == []


# ── E1a Task 2b: reconcile the TARGET UNIVERSE, not just the successes ────────────────────────────
# A column that DROPS OUT of a run must not keep asserting yesterday's AI value. Retirement is by
# DISPOSITION: deliberately withheld / no longer a target -> RETIRE; a transient miss -> KEEP.


def _ai_defs(conn, ref: str) -> list:
    """The ACTIVE ``llm`` definition proposals at ``ref`` (producer-scoped: a source/human row at the
    same ref is another producer's and must never be touched by AI reconciliation)."""
    return [e for e in read_active_field_evidence(conn, ref, "definition") if e.producer == "llm"]


def _blank_def_upload():
    return to_glossary_upload(read_ftr_glossary(_BLANK_DEF_CSV, source=_AI_SOURCE))


def _ingest_drafted(db, now: datetime) -> None:
    """Ingest the blank-definition glossary so the column carries an AI definition."""
    upload = _blank_def_upload()
    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=now,
                        client=_drafting_client(upload.rows[0], "Customer full legal name"),
                        glossary=upload)
    assert res.status == "ingested"
    assert [e.proposed_value for e in _ai_defs(db, _NAME_REF)] == ["Customer full legal name"]


def test_suppressed_definition_retires_prior_ai(db):
    """THE KEY PROPERTY, end-to-end: a column whose definition becomes sanitizer-SUPPRESSED drops out
    of the drafter's target set — the prior AI definition must be RETIRED, not left asserting itself
    as "AI proposed" for a value the model no longer proposes."""
    _seal()
    _ingest_drafted(db, _NOW)

    sup = to_glossary_upload(read_ftr_glossary(_SUPPRESSED_DEF_CSV, source=_AI_SOURCE))
    (row,) = sup.rows
    assert not row.definition                          # blank...
    assert sup.records[0].definition_suppressed        # ...but WITHHELD, not missing
    res = ingest_upload(db, _AI_SOURCE, sup.rows, actor=_actor(), now=_NOW + timedelta(hours=1),
                        client=_drafting_client(row, "must not be drafted"), glossary=sup)
    assert res.status == "ingested"
    assert _ai_defs(db, _NAME_REF) == []


def test_no_longer_a_target_retires_prior_ai(db):
    """A column that now carries an UPLOADER-declared definition is no longer an AI target (R3 never
    overwrites a human's) — its prior AI proposal is retired rather than left competing."""
    _seal()
    _ingest_drafted(db, _NOW)

    declared = to_glossary_upload(read_ftr_glossary(_DECLARED_DEF_CSV, source=_AI_SOURCE))
    (row,) = declared.rows
    assert row.definition                              # source-provided -> not a blank to draft
    res = ingest_upload(db, _AI_SOURCE, declared.rows, actor=_actor(),
                        now=_NOW + timedelta(hours=1),
                        client=_drafting_client(row, "must not be drafted"), glossary=declared)
    assert res.status == "ingested"
    assert _ai_defs(db, _NAME_REF) == []
    # producer-scoped: the SOURCE's own definition proposal at the same ref is untouched.
    assert [e.producer for e in read_active_field_evidence(db, _NAME_REF, "definition")] == ["source"]


def test_transient_miss_keeps_prior_ai(overlay_conn):
    """THE SAFETY PROPERTY: the column is still an expected target (blank, NOT suppressed) but the
    drafter returned nothing — a provider blip. The prior AI definition SURVIVES with its value."""
    upload = _blank_def_upload()
    (row,) = upload.rows
    build_graph(overlay_conn, _AI_SOURCE, [row])
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "prior AI def"},
                              ref_of=lambda k: (_NAME_REF, _NAME_REF, {"k": k}),
                              source_snapshot_id="snap")
    n = _write_definition_evidence(overlay_conn, source=_AI_SOURCE, rows=[row], definitions={},
                                   glossary=upload, concepts=None, bindings=None,
                                   source_snapshot_id="snap2")
    assert n == 0
    assert [e.proposed_value for e in _ai_defs(overlay_conn, _NAME_REF)] == ["prior AI def"]


def test_fresh_write_is_never_retired(overlay_conn):
    """The set-difference guard: a ref written THIS run must be excluded from ``retire_refs`` — one
    active row carrying the new value, never a freshly-staled one."""
    upload = _blank_def_upload()
    (row,) = upload.rows
    build_graph(overlay_conn, _AI_SOURCE, [row])
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "prior AI def"},
                              ref_of=lambda k: (_NAME_REF, _NAME_REF, {"k": k}),
                              source_snapshot_id="snap")
    n = _write_definition_evidence(
        overlay_conn, source=_AI_SOURCE, rows=[row],
        definitions={content_hash(row): "Customer full legal name"}, glossary=upload,
        concepts=None, bindings=None, source_snapshot_id="snap2")
    assert n == 0
    assert [e.proposed_value for e in _ai_defs(overlay_conn, _NAME_REF)] == ["Customer full legal name"]


def test_definition_evidence_escape_records_partial(db, monkeypatch):
    """T2-M2: a throw ESCAPING ``_write_definition_evidence`` (outside the writer's per-item try)
    must still count as a failure — the savepoint rolls the writes back, so reporting ``succeeded``
    would launder lost metadata into a success."""
    _seal()
    upload = _blank_def_upload()

    def _boom(*a, **k):
        raise RuntimeError("field_evidence store unavailable")

    monkeypatch.setattr(enrich_mod, "_reconcile_llm_field_evidence", _boom)
    rec = StageRecorder()
    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=_NOW,
                        client=_drafting_client(upload.rows[0], "Customer full legal name"),
                        glossary=upload, stage_recorder=rec)
    assert res.status == "ingested"               # the contained failure never aborts the upload

    report = next(r for r in rec.reports if r.stage == "enrich_definition")
    assert report.state == "partial" and report.reason_code == "items_failed"
    assert report.detail["internal_failures"] == 1
    assert _ai_defs(db, _NAME_REF) == []
