"""D13.2 — the LLM-proposed `sub_domain` axis, end to end, with FakeLLM only.

The product decision: a FINER axis beside the coarse source `domain`, produced by the EXISTING
Pass-A domain task (a real schema v2 body + a prompt bump, never a second LLM call), rendered with
its `llm_proposed` authority label, human-editable through the existing four-eyes flow, never
load-bearing and never overwriting `domain`.

Witness-shaped, per the decision's own acceptance note: a BIC-like column, a date-like column and a
flag column must be REPRESENTABLE as receiving sub-domains finer than their table's domain. These
are FIXTURE witnesses — they prove the pipeline can carry the answer end to end. Whether the live
model actually produces them is the Gate-B re-enrichment question and is deliberately not asserted
here (no live LLM call anywhere).
"""
from __future__ import annotations

import json

import pytest

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload import enrich
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich_llm import _require_schema, register_enrichment_schemas
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload

_SOURCE = "ftr"
_TABLE = "comp_fin_tran"
_SCHEMA = "dpl_core"
_DOMAIN_TASK = "overlay.enrich.domain"

#: The three witnesses D13.2 names: an identifier-shaped code, a temporal column, and a flag.
_WITNESSES = {
    "counter_party_bic": "Correspondent Banking",
    "pstd_date": "Transaction Posting",
    "sanctions_hit_flg": "Sanctions Screening",
}


def _rows() -> list[CanonicalRow]:
    return [CanonicalRow(_SOURCE, _TABLE, c, "unknown") for c in _WITNESSES]


def _upload() -> GlossaryUpload:
    rows = _rows()
    records = [
        GlossaryRecord(logical_ref=f"{_SOURCE}::{_SCHEMA}.{_TABLE}.{r.column}",
                       term_name=r.column.replace("_", " ").title(),
                       definition=f"The {r.column} of the transaction.",
                       schema=_SCHEMA, declared_type="varchar")
        for r in rows
    ]
    return GlossaryUpload(rows=rows, records=records)


def _domain_response() -> FakeResponse:
    return FakeResponse(output={"results": [{
        "ref": _TABLE,
        "domain": "Compliance",
        "column_domains": [],
        "column_sub_domains": [{"column": c, "sub_domain": s} for c, s in _WITNESSES.items()],
    }]})


# ── the contract: a REAL v2 body, registered before anything requests it ─────────────────────────


def test_the_domain_batch_v2_schema_is_a_real_registered_body(db) -> None:
    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)
    v1 = _require_schema(db, reg, "overlay_domain_batch", 1)
    v2 = _require_schema(db, reg, "overlay_domain_batch", 2)
    assert v1 != v2, "v2 must not be a byte-alias of v1 — it has to ADMIT the new field"
    item = v2["properties"]["results"]["items"]["properties"]
    assert "column_sub_domains" in item
    assert item["column_sub_domains"]["items"]["properties"].keys() == {"column", "sub_domain"}
    # additionalProperties:false is preserved at every level (D10).
    assert v2["additionalProperties"] is False
    assert v2["properties"]["results"]["items"]["additionalProperties"] is False
    assert item["column_sub_domains"]["items"]["additionalProperties"] is False
    # And the flat single/fallback seam resolves at v2 too, so a degraded batch never dispatches
    # unenforced (the `schema_for(id, N+1) -> None` trap).
    assert _require_schema(db, reg, "overlay_domain", 2) is not None


def test_the_finer_axis_costs_no_extra_llm_call(db, monkeypatch) -> None:
    """D13.2's explicit rule. One domain request in, sub-domains out."""
    monkeypatch.setenv("OVERLAY_ENRICH_DOMAIN_MODE", "batch")
    calls: list = []

    class _Counting(FakeLLM):
        def call(self, request):
            calls.append(request)
            return super().call(request)

    client = _Counting(script={_DOMAIN_TASK: _domain_response()})
    subs: dict = {}
    enrich.classify_domains(db, _rows(), client, column_sub_domains=subs, glossary=_upload())
    assert len(calls) == 1
    assert calls[0].prompt_version == 2
    assert calls[0].output_schema_version == 2
    assert calls[0].prompt_id == "overlay_domain_batch_v2"
    assert len(subs) == 3


# ── the witnesses ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("column,expected", sorted(_WITNESSES.items()))
def test_each_witness_column_can_receive_a_sub_domain_finer_than_its_table_domain(
        db, monkeypatch, column, expected) -> None:
    monkeypatch.setenv("OVERLAY_ENRICH_DOMAIN_MODE", "batch")
    client = FakeLLM(script={_DOMAIN_TASK: _domain_response()})
    overrides: dict = {}
    subs: dict = {}
    domains = enrich.classify_domains(db, _rows(), client, column_domains=overrides,
                                      column_sub_domains=subs, glossary=_upload())
    assert domains[_TABLE] == "Compliance"          # the coarse axis is unchanged
    assert overrides == {}                          # and no column OVERRIDES it
    assert subs[(_TABLE, column)] == expected       # the finer axis is carried per column
    assert expected != domains[_TABLE]              # genuinely finer, never a restatement


def test_a_sub_domain_that_restates_the_table_domain_is_dropped(db, monkeypatch) -> None:
    """A restated coarse domain is not a finer axis; writing evidence for it would fabricate a
    column-level assertion for what is really inheritance."""
    monkeypatch.setenv("OVERLAY_ENRICH_DOMAIN_MODE", "batch")
    client = FakeLLM(script={_DOMAIN_TASK: FakeResponse(output={"results": [{
        "ref": _TABLE, "domain": "Compliance",
        "column_sub_domains": [{"column": "counter_party_bic", "sub_domain": "Compliance"},
                               {"column": "pstd_date", "sub_domain": "Transaction Posting"}]}]})})
    subs: dict = {}
    enrich.classify_domains(db, _rows(), client, column_sub_domains=subs, glossary=_upload())
    assert subs == {(_TABLE, "pstd_date"): "Transaction Posting"}


def test_an_unusable_sub_domain_drops_only_itself(db, monkeypatch) -> None:
    """Per-field salvage, deliberately asymmetric to a coarse OVERRIDE: dropping a bad sub-domain
    changes nothing a consumer relies on (the coarse domain still resolves), whereas dropping a bad
    override would read downstream as "the model WITHDREW it" and retire the prior AI domain."""
    monkeypatch.setenv("OVERLAY_ENRICH_DOMAIN_MODE", "batch")
    client = FakeLLM(script={_DOMAIN_TASK: FakeResponse(output={"results": [{
        "ref": _TABLE, "domain": "Compliance",
        "column_sub_domains": [
            {"column": "counter_party_bic", "sub_domain": "overlay.enrich.domain"},   # task echo
            {"column": "pstd_date", "sub_domain": "Transaction Posting"}]}]})})
    subs: dict = {}
    domains = enrich.classify_domains(db, _rows(), client, column_sub_domains=subs,
                                      glossary=_upload())
    assert domains[_TABLE] == "Compliance"                       # the item still resolves
    assert subs == {(_TABLE, "pstd_date"): "Transaction Posting"}


# ── persistence: llm/proposed evidence, never load-bearing, never touching `domain` ──────────────


def test_sub_domains_persist_as_llm_proposed_evidence_beside_an_untouched_domain(db) -> None:
    upload = _upload()
    enrich._write_sub_domain_evidence(
        db, source=_SOURCE, rows=upload.rows,
        column_sub_domains={(_TABLE, c): s for c, s in _WITNESSES.items()},
        glossary=upload, bindings=None, source_snapshot_id="snap-1")
    for column, expected in _WITNESSES.items():
        ref = f"{_SOURCE}::{_SCHEMA}.{_TABLE}.{column}"
        rows = read_active_field_evidence(db, ref, "sub_domain")
        assert [(e.producer, e.strength, e.proposed_value) for e in rows] == [
            ("llm", "proposed", expected)]
        # The coarse axis is a DIFFERENT field and gains nothing from this writer.
        assert read_active_field_evidence(db, ref, "domain") == []


def test_a_column_the_classifier_stops_singling_out_is_retired(db) -> None:
    upload = _upload()
    enrich._write_sub_domain_evidence(
        db, source=_SOURCE, rows=upload.rows,
        column_sub_domains={(_TABLE, c): s for c, s in _WITNESSES.items()},
        glossary=upload, bindings=None, source_snapshot_id="snap-1")
    # A second run singles out only one column: the other two are withdrawals, not misses — the
    # table WAS classified this run, so their absence is information.
    enrich._write_sub_domain_evidence(
        db, source=_SOURCE, rows=upload.rows,
        column_sub_domains={(_TABLE, "pstd_date"): "Transaction Posting"},
        glossary=upload, bindings=None, source_snapshot_id="snap-2")
    kept = read_active_field_evidence(db, f"{_SOURCE}::{_SCHEMA}.{_TABLE}.pstd_date", "sub_domain")
    gone = read_active_field_evidence(
        db, f"{_SOURCE}::{_SCHEMA}.{_TABLE}.counter_party_bic", "sub_domain")
    assert [e.proposed_value for e in kept] == ["Transaction Posting"]
    assert gone == []


def test_a_table_whose_classification_missed_keeps_its_sub_domains(db) -> None:
    """KEEP is the safe default: a transient provider miss must not retire good AI evidence."""
    upload = _upload()
    enrich._write_sub_domain_evidence(
        db, source=_SOURCE, rows=upload.rows,
        column_sub_domains={(_TABLE, c): s for c, s in _WITNESSES.items()},
        glossary=upload, bindings=None, source_snapshot_id="snap-1")
    enrich._write_sub_domain_evidence(          # nothing classified this run at all
        db, source=_SOURCE, rows=upload.rows, column_sub_domains={},
        glossary=upload, bindings=None, source_snapshot_id="snap-2")
    for column in _WITNESSES:
        ref = f"{_SOURCE}::{_SCHEMA}.{_TABLE}.{column}"
        assert read_active_field_evidence(db, ref, "sub_domain"), column


def test_the_envelope_round_trips_through_the_cache_shape() -> None:
    """The cached envelope is the ONE canonical string both seams and the cache carry."""
    accept = enrich._accept_domain_result(64)
    raw = json.dumps({"domain": "Compliance", "column_domains": [],
                      "column_sub_domains": [{"column": "pstd_date",
                                              "sub_domain": "Transaction Posting"}]})
    envelope, reason = accept(raw)
    assert reason == "valid"
    assert enrich._parse_domain_result(envelope) == (
        "Compliance", {}, {"pstd_date": "Transaction Posting"})


# ── the dossier surface ──────────────────────────────────────────────────────────────────────────


def test_the_dossier_shows_a_sub_domain_labeled_llm_proposed(db) -> None:
    """The no-"blocked" rule: an AI-proposed finer axis is VISIBLE and labeled, never hidden and
    never framed as a failure — and it never displaces the coarse `domain` beside it."""
    from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
    from featuregen.overlay.field_evidence import record_field_evidence
    from featuregen.overlay.upload.asset_detail import build_asset_detail
    from featuregen.overlay.upload.field_resolution import resolve_and_project
    from featuregen.overlay.upload.graph import build_graph
    from featuregen.overlay.upload.ingest import _write_glossary_source_evidence

    upload = _upload()
    schemas = {f"public.{_TABLE}.{r.column}": _SCHEMA for r in upload.rows}
    schemas[f"public.{_TABLE}"] = _SCHEMA
    build_graph(db, _SOURCE, upload.rows, concepts={}, schemas=schemas)
    ref = f"{_SOURCE}::{_SCHEMA}.{_TABLE}.counter_party_bic"
    rec = next(r for r in upload.records if r.logical_ref == ref)
    _write_glossary_source_evidence(db, logical_ref=ref, rec=rec, snapshot_id="snap-1")
    record_field_evidence(
        db, logical_ref=ref, field_name="sub_domain", proposed_value="Correspondent Banking",
        producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
        producer_ref="overlay-enrichment", source_snapshot_id="snap-1", input_hash="d" * 64)
    record_field_evidence(
        db, logical_ref=ref, field_name="domain", proposed_value="Compliance",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.PROPOSED,
        producer_ref="snap-1", source_snapshot_id="snap-1", input_hash="e" * 64)
    resolve_and_project(db, source=_SOURCE, logical_refs=[ref])

    dossier = build_asset_detail(
        db, source=_SOURCE, object_ref=f"public.{_TABLE}.counter_party_bic",
        roles=("catalog_reader", "pii_reader", "restricted_reader", "confidential_reader"),
        include=("effective_metadata",))
    fields = dossier["effective_metadata"]["fields"]
    sub = fields["sub_domain"]
    assert sub["value"] == "Correspondent Banking"            # VISIBLE, never hidden
    assert sub["evidence_provenance"] == "AI proposed"        # and honestly labeled as the AI's
    assert sub["authority"] == "hint"                         # never governed on an AI's say-so
    assert sub["c1_status"] != "confirmed"
    domain = fields["domain"]
    assert domain["value"] == "Compliance"                    # the coarse axis is untouched
    assert domain["evidence_provenance"] != "AI proposed"     # and still the source's
