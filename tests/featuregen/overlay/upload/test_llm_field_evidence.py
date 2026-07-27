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
written this run is never in the retire set. Retirement is also SOURCE-scoped: ingesting one source
can never reach into another's AI evidence.

Task 3 makes ``domain`` TWO-LEVEL: the table's domain is the DEFAULT context its columns inherit and
is evidence on the TABLE node; a column carries its own ``domain`` evidence ONLY where it OVERRIDES
that default. Inheritance is never fabricated as evidence — asset-detail says ``origin="inherited"``
with ``inherited_from``, or ``origin="direct"`` when the column really does assert its own. The AI
fills a BLANK domain and never contests a sidecar-curated one (two competing proposals resolve to a
display CONFLICT that would blank the curated value).
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
from featuregen.overlay.upload.feature_assist import (
    _candidate_columns,
    select_relevant_context,
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


# ── Cross-SOURCE isolation (review finding T2b-M3) ────────────────────────────────────────────────
# Retirement is source-wide and destructive; ingesting source A must never reach into source B.


def test_reconciliation_never_retires_another_sources_ai(overlay_conn):
    """Ingesting source A must NOT retire source B's AI evidence: the target universe is scoped to
    ONE catalog source (a ref of another source is not this upload's to retire)."""
    upload = _blank_def_upload()
    (row,) = upload.rows
    build_graph(overlay_conn, _AI_SOURCE, [row])
    other = normalize_ref("ftr_other", "dpl_eib_compliance", "comp_fin_tran", "cust_name")
    _write_llm_field_evidence(overlay_conn, field_name="definition",
                              items={"h": "another source's AI def"},
                              ref_of=lambda k: (other, other, {"k": k}), source_snapshot_id="snap")

    # Source A drops its own target entirely (nothing drafted, nothing expected) -> full retirement
    # of ITS universe. Source B's row must be untouched.
    n = _write_definition_evidence(overlay_conn, source=_AI_SOURCE, rows=[], definitions={},
                                   glossary=upload, concepts=None, bindings=None,
                                   source_snapshot_id="snap2")
    assert n == 0
    assert [e.proposed_value for e in _ai_defs(overlay_conn, other)] == ["another source's AI def"]


# ── E1a Task 3: TWO-LEVEL domain — a table DEFAULT + explicit column OVERRIDES ────────────────────
# The table's domain is the context every column inherits; a column only carries its OWN domain
# evidence when the classifier gave it a DIFFERENT one. Inheritance is never fabricated as evidence.

# Two columns of ONE table, both with the sidecar's `data_domain` cell EMPTY, so neither column
# carries a SOURCE domain assertion of its own — the AI's two-level classification is the only
# domain in play (an inherited column has NO column-level evidence at all).
_DOMAIN_CSV = _HDR + _row(domain="") + _row(
    source_row="19", fqn="DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.FRAUD_SCORE", term_name="Fraud Score",
    definition='"Model score for the transaction fraud risk."', domain="", term_type="Measure",
    synonyms="", b1="", b2="", b3="", b4="", fibo="", data_type="DECIMAL")
_TABLE_REF = normalize_ref(_AI_SOURCE, "DPL_EIB_COMPLIANCE", "COMP_FIN_TRAN")
_SCORE_REF = normalize_ref(_AI_SOURCE, "DPL_EIB_COMPLIANCE", "COMP_FIN_TRAN", "FRAUD_SCORE")
_SCORE_OBJECT_REF = "public.comp_fin_tran.fraud_score"


def _ai_domains(conn, ref: str) -> list:
    """The ACTIVE ``llm`` domain proposals at ``ref`` (producer-scoped)."""
    return [e for e in read_active_field_evidence(conn, ref, "domain") if e.producer == "llm"]


def _domain_upload():
    return to_glossary_upload(read_ftr_glossary(_DOMAIN_CSV, source=_AI_SOURCE))


def _domain_client(table_domain: str = "retail_banking",
                   overrides: tuple[dict, ...] = ({"column": "fraud_score",
                                                   "domain": "fraud_risk"},)) -> FakeLLM:
    """A FakeLLM whose domain task returns the TWO-LEVEL result: the table's default domain plus
    ONLY the columns whose domain differs from it."""
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": []}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": "comp_fin_tran", "domain": table_domain,
             "column_domains": list(overrides)}]}),
    })


def _ingest_domains(db) -> None:
    _seal()
    upload = _domain_upload()
    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=_NOW,
                        client=_domain_client(), glossary=upload)
    assert res.status == "ingested"


def _domain_field(db, object_ref: str) -> dict:
    return build_asset_detail(
        db, source=_AI_SOURCE, object_ref=object_ref, roles=list(_ADMIN.role_claims),
        identity=_ADMIN, include=["effective_metadata"])["effective_metadata"]["fields"]["domain"]


def test_table_domain_is_table_level_evidence_columns_inherit(db):
    """The TABLE default: ``llm/proposed`` ``domain`` evidence lands on the TABLE node (at its
    SCHEMA-preserving ref, never a ``public`` twin), and a column that does NOT override it renders
    ``origin="inherited"`` with NO column-level domain evidence fabricated for it."""
    _ingest_domains(db)

    ev = _ai_domains(db, _TABLE_REF)
    assert len(ev) == 1
    assert ev[0].strength == "proposed" and ev[0].proposed_value == "retail_banking"

    assert _ai_domains(db, _NAME_REF) == []          # inheritance is NOT evidence
    field = _domain_field(db, _NAME_OBJECT_REF)
    assert field["origin"] == "inherited" and field["inherited_from"] == "comp_fin_tran"
    assert field["value"] == "retail_banking"        # the effective domain is the table's default


def test_column_domain_override_is_direct_column_evidence(db):
    """An OVERRIDE: the column whose domain DIFFERS from its table gets its own ``llm/proposed``
    ``domain`` evidence, renders ``origin="direct"``, and its effective domain is the override —
    not the table default."""
    _ingest_domains(db)

    ev = _ai_domains(db, _SCORE_REF)
    assert len(ev) == 1
    assert ev[0].strength == "proposed" and ev[0].proposed_value == "fraud_risk"

    field = _domain_field(db, _SCORE_OBJECT_REF)
    assert field["origin"] == "direct" and "inherited_from" not in field
    assert field["value"] == "fraud_risk"
    assert field["evidence_provenance"] == "AI proposed"


def test_dropped_domain_override_is_retired(overlay_conn):
    """A column that STOPS being an override (its table classified fine, and the model no longer
    gives that column a different domain) must stop asserting yesterday's AI override."""
    upload = _domain_upload()
    build_graph(overlay_conn, _AI_SOURCE, upload.rows)
    _write_llm_field_evidence(overlay_conn, field_name="domain", items={"h": "fraud_risk"},
                              ref_of=lambda k: (_SCORE_REF, _SCORE_REF, {"k": k}),
                              source_snapshot_id="snap")

    n = enrich_mod._write_domain_evidence(
        overlay_conn, source=_AI_SOURCE, rows=upload.rows,
        domains={"comp_fin_tran": "retail_banking"}, column_domains={}, glossary=upload,
        bindings=None, source_snapshot_id="snap2")

    assert n == 0
    assert _ai_domains(overlay_conn, _SCORE_REF) == []                     # no longer an override
    assert [e.proposed_value for e in _ai_domains(overlay_conn, _TABLE_REF)] == ["retail_banking"]


def test_transient_domain_miss_keeps_prior_ai(overlay_conn):
    """THE SAFETY PROPERTY: the table is still a target but the classifier returned nothing — a
    provider blip. BOTH the table domain and its column overrides survive."""
    upload = _domain_upload()
    build_graph(overlay_conn, _AI_SOURCE, upload.rows)
    for ref, value in ((_TABLE_REF, "retail_banking"), (_SCORE_REF, "fraud_risk")):
        _write_llm_field_evidence(overlay_conn, field_name="domain", items={"h": value},
                                  ref_of=lambda k, r=ref: (r, r, {"k": k}),
                                  source_snapshot_id="snap")

    n = enrich_mod._write_domain_evidence(
        overlay_conn, source=_AI_SOURCE, rows=upload.rows, domains={}, column_domains={},
        glossary=upload, bindings=None, source_snapshot_id="snap2")

    assert n == 0
    assert [e.proposed_value for e in _ai_domains(overlay_conn, _TABLE_REF)] == ["retail_banking"]
    assert [e.proposed_value for e in _ai_domains(overlay_conn, _SCORE_REF)] == ["fraud_risk"]


def test_domain_evidence_write_failure_records_partial(db, monkeypatch):
    """FAILURE PROPAGATION at the domain stage too: a contained evidence-write failure must surface
    as ``partial``/``items_failed``, never a laundered ``succeeded``."""
    _seal()
    upload = _domain_upload()

    def _boom(*a, **k):
        raise RuntimeError("field_evidence store unavailable")

    monkeypatch.setattr(enrich_mod, "record_field_evidence", _boom)
    rec = StageRecorder()
    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=_NOW,
                        client=_domain_client(), glossary=upload, stage_recorder=rec)
    assert res.status == "ingested"               # the contained failure never aborts the upload

    report = next(r for r in rec.reports if r.stage == "enrich_domain")
    assert report.state == "partial" and report.reason_code == "items_failed"
    assert _ai_domains(db, _TABLE_REF) == []


def test_domain_evidence_escape_records_partial(db, monkeypatch):
    """A throw ESCAPING ``_write_domain_evidence`` (outside the writer's per-item try) must still
    count: the savepoint rolls the writes back, so ``succeeded`` would launder lost metadata."""
    _seal()
    upload = _domain_upload()

    def _boom(*a, **k):
        raise RuntimeError("field_evidence store unavailable")

    monkeypatch.setattr(enrich_mod, "_reconcile_llm_field_evidence", _boom)
    rec = StageRecorder()
    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=_NOW,
                        client=_domain_client(), glossary=upload, stage_recorder=rec)
    assert res.status == "ingested"

    report = next(r for r in rec.reports if r.stage == "enrich_domain")
    assert report.state == "partial" and report.reason_code == "items_failed"
    assert report.detail["internal_failures"] == 1
    assert _ai_domains(db, _TABLE_REF) == []


def test_source_declared_domain_is_never_contested(overlay_conn):
    """The AI fills a BLANK domain, it never contests a CURATED one: a column whose sidecar declares
    its own ``data_domain`` is not an AI target at all — and a prior AI proposal for it is retired.
    (Two competing proposals resolve to a display CONFLICT, which would BLANK the curated value.)"""
    declared = to_glossary_upload(read_ftr_glossary(_HDR + _row(), source=_AI_SOURCE))
    assert declared.records[0].domain                     # the sidecar curated it ("Party")
    build_graph(overlay_conn, _AI_SOURCE, declared.rows)
    _write_llm_field_evidence(overlay_conn, field_name="domain", items={"h": "stale ai domain"},
                              ref_of=lambda k: (_NAME_REF, _NAME_REF, {"k": k}),
                              source_snapshot_id="snap")

    n = enrich_mod._write_domain_evidence(
        overlay_conn, source=_AI_SOURCE, rows=declared.rows,
        domains={"comp_fin_tran": "retail_banking"},
        column_domains={("comp_fin_tran", "cust_name"): "fraud_risk"}, glossary=declared,
        bindings=None, source_snapshot_id="snap2")

    assert n == 0
    assert _ai_domains(overlay_conn, _NAME_REF) == []      # not a target -> not written, and retired


def test_table_no_longer_in_the_upload_is_retired(overlay_conn):
    """A table that DROPPED OUT of the source entirely is no longer a target — its AI domain is
    retired rather than left asserting itself for a table nobody uploaded."""
    upload = _domain_upload()
    build_graph(overlay_conn, _AI_SOURCE, upload.rows)
    gone = normalize_ref(_AI_SOURCE, "dpl_eib_compliance", "old_table")
    _write_llm_field_evidence(overlay_conn, field_name="domain", items={"h": "legacy"},
                              ref_of=lambda k: (gone, gone, {"k": k}), source_snapshot_id="snap")

    n = enrich_mod._write_domain_evidence(
        overlay_conn, source=_AI_SOURCE, rows=upload.rows,
        domains={"comp_fin_tran": "retail_banking"}, column_domains={}, glossary=upload,
        bindings=None, source_snapshot_id="snap2")

    assert n == 0
    assert _ai_domains(overlay_conn, gone) == []


# ── E1a Task 4: LLM synonyms as FIRST-CLASS `llm/proposed` semantic evidence ──────────────────────
# The AI's synonyms are not search-only decoration and need no human confirmation: they are durable
# `field_evidence(field_name="semantic_terms", producer=llm, strength=proposed)` MERGED (union, never
# overwrite) onto `graph_node.semantic_terms` AFTER `build_graph` — which DELETE+RECREATES the
# source's nodes, so an enrich-time projection would be silently wiped. An AI synonym may be the ONLY
# semantic signal that selects a column; it flows through the EXISTING menu/relevance path with no
# new gate. Store the provenance WITH the synonym; let downstream decide how much authority to give it.

_AMT_REF = normalize_ref(_AI_SOURCE, "DPL_EIB_COMPLIANCE", "COMP_FIN_TRAN", "TXN_AMT")
_AMT_OBJECT_REF = "public.comp_fin_tran.txn_amt"
# Two glossary columns, both with CURATED synonyms of their own ("Client Name|Account Holder", "Amt")
# — so the merge has something real to preserve, and the AI's terms are distinguishable from them.
_SYN_CSV = _HDR + _row() + _row(
    source_row="19", fqn="DPL_EIB_COMPLIANCE.COMP_FIN_TRAN.TXN_AMT", term_name="Transaction Amount",
    definition='"The monetary amount of the transaction."', domain="Payments", term_type="Measure",
    l1="Settlement", related="Amount Alias", l2="Clearing", l3="", synonyms="Amt", b1="Payment",
    b2="Transaction", b3="Amount", b4="", fibo="fibo-fbc:MonetaryAmount", data_type="DECIMAL")


def _syn_upload():
    return to_glossary_upload(read_ftr_glossary(_SYN_CSV, source=_AI_SOURCE))


def _synonym_client(upload, terms: dict[str, str]) -> FakeLLM:
    """A FakeLLM whose SYNONYM task drafts ``terms[column]`` for each named column. ``validate_rows``
    normalizes row identity before enrichment, so the batch ref is the normalized row's hash."""
    results = []
    for row in upload.rows:
        drafted = terms.get(row.column.lower())
        if drafted:
            h = content_hash(replace(row, table=row.table.lower(), column=row.column.lower()))
            results.append({"ref": h, "synonyms": drafted})
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": []}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": []}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": results}),
    })


def _ingest_synonyms(db, terms: dict[str, str], now: datetime = _NOW) -> None:
    _seal()
    upload = _syn_upload()
    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=now,
                        client=_synonym_client(upload, terms), glossary=upload)
    assert res.status == "ingested"


def _ai_terms(conn, ref: str) -> list:
    """The ACTIVE ``llm`` ``semantic_terms`` proposals at ``ref`` (producer-scoped)."""
    return [e for e in read_active_field_evidence(conn, ref, "semantic_terms")
            if e.producer == "llm"]


def _node_terms(conn, object_ref: str) -> str:
    row = conn.execute(
        "SELECT semantic_terms FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (_AI_SOURCE, object_ref)).fetchone()
    return row[0] or ""


def test_ai_synonyms_are_llm_proposed_semantic_evidence(db):
    """The AI's synonyms land as durable ``llm/proposed`` ``semantic_terms`` evidence at the
    SCHEMA-preserving ref (never the ``public`` twin the binding is keyed by) — the provenance is
    stored WITH the synonym, so downstream can decide how much authority to give it."""
    _ingest_synonyms(db, {"txn_amt": "available funds, cleared amount"})

    ev = _ai_terms(db, _AMT_REF)
    assert len(ev) == 1
    assert ev[0].producer == "llm" and ev[0].strength == "proposed"
    assert ev[0].proposed_value == "available funds, cleared amount"
    assert _ai_terms(db, normalize_ref(_AI_SOURCE, None, "COMP_FIN_TRAN", "TXN_AMT")) == []


def test_ai_synonyms_merge_with_glossary_terms_and_survive_build_graph(db):
    """MERGE, NEVER OVERWRITE — and the ORDERING proof: enrichment runs BEFORE ``build_graph``, which
    DELETE+RECREATES the source's ``graph_node`` rows, so the union must be projected AFTER it. After
    a FULL ingest the node carries BOTH the curated glossary terms and the AI's."""
    _ingest_synonyms(db, {"txn_amt": "available funds, cleared amount"})

    terms = _node_terms(db, _AMT_OBJECT_REF)
    assert "Transaction Amount" in terms and "Amt" in terms      # the glossary's own, untouched
    assert "available funds" in terms and "cleared amount" in terms   # the AI's, merged in
    # The other column is unaffected: no AI synonym, curated terms intact.
    assert "Client Name" in _node_terms(db, _NAME_OBJECT_REF)


def test_ai_synonym_alone_reaches_the_candidate_menu(db):
    """FEATURE-GEN REACH, no new gate: a column whose ONLY signal for the objective's words is an AI
    synonym is selected — and ranked first — by the EXISTING relevance path, and the synonym rides
    the menu column itself."""
    _ingest_synonyms(db, {"txn_amt": "available funds"})
    assert "available" not in _node_terms(db, _NAME_OBJECT_REF).lower()   # the AI is the only source

    cols = _candidate_columns(db, _AI_SOURCE, roles=list(_ADMIN.role_claims))
    selected, _context, _dropped = select_relevant_context(
        db, cols, objective="available funds", entity=None)

    assert selected[0]["object_ref"] == _AMT_OBJECT_REF
    assert "available funds" in selected[0]["semantic_terms"]


def test_reenrichment_stales_only_the_prior_ai_synonym(db):
    """Producer-scoped staleness: a CHANGED synonym supersedes the prior LLM row (exactly one active
    proposal, the new value) and drops out of the projection — while the glossary's curated terms are
    never touched."""
    _ingest_synonyms(db, {"txn_amt": "available funds"})
    _ingest_synonyms(db, {"txn_amt": "cleared balance"}, now=_NOW + timedelta(hours=1))

    assert [e.proposed_value for e in _ai_terms(db, _AMT_REF)] == ["cleared balance"]
    terms = _node_terms(db, _AMT_OBJECT_REF)
    assert "cleared balance" in terms
    assert "available funds" not in terms
    assert "Transaction Amount" in terms and "Amt" in terms      # the curated terms survive


def test_no_llm_client_records_skipped_and_ingestion_continues(db):
    """No provider configured: the synonym stage honestly records ``skipped_no_client`` and the
    upload completes normally (the stage is never invented as a failure)."""
    _seal()
    upload = _syn_upload()
    rec = StageRecorder()
    res = ingest_upload(db, _AI_SOURCE, upload.rows, actor=_actor(), now=_NOW, glossary=upload,
                        stage_recorder=rec)

    assert res.status == "ingested"
    assert next(r for r in rec.reports if r.stage == "enrich_synonyms").state == "skipped_no_client"
    assert _ai_terms(db, _AMT_REF) == []
