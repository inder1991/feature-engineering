"""Whole-branch fix #1 — Pass B advisory table ref must share the SCHEMA its columns use.

A NON-public-schema glossary keys its column decisions under the schema-preserving ref
(``src::dpl_eib_compliance.txn.<col>``). Pass B recorded its advisory table_role/primary_entity
evidence under a schema-forced-PUBLIC table ref (``src::public.txn``). ``readiness._tables_of`` is
schema-aware and aggregates BOTH out of ``field_decision_event`` — so ONE physical table appeared
under TWO (schema, table) pairs, which (a) DOUBLE-COUNTED the grain/availability/join requirements
and (b) made a bare ``subset="txn"`` raise "ambiguous TABLE subset".
"""
from __future__ import annotations

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.glossary_reader import read_glossary
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.readiness import ReadinessScopeType, compute_readiness

_SCHEMA = "dpl_eib_compliance"
_GLOSSARY_CSV = (
    "physical_name,business_term,description_business_definition,data_domain,bian_path,fibo_path\n"
    f"{_SCHEMA}.txn.txn_id,Transaction ID,Unique identifier assigned to each posted transaction.,"
    "Payments,Payment/Transaction,fibo-fbc:TransactionIdentifier\n"
    f"{_SCHEMA}.txn.amt,Transaction Amount,The monetary amount of the posted transaction.,"
    "Payments,Payment/Transaction,fibo-fbc:MonetaryAmount\n")


def _client(glossary) -> FakeLLM:
    hashes = [content_hash(r) for r in glossary.rows]
    synthesis = {"grain_columns": ["txn_id"], "as_of_column": None, "as_of_basis": None,
                 "table_role": "fact", "primary_entity": "transaction",
                 "event_or_snapshot": "event"}
    client = FakeLLM(script={
        "table_synth": FakeResponse(output={"results": [{"ref": "txn", "synthesis": synthesis}]}),
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": "monetary_stock"} for h in hashes]}),
        "overlay.enrich.definition": FakeResponse(output={"results": [
            {"ref": h, "definition": "A one-line business definition."} for h in hashes]}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": "txn", "domain": "payments"}]}),
    })
    client.script(task="overlay.enrich.concept", prompt_id="overlay_concept_v1",
                  responses=[FakeResponse(output={"concept": "monetary_stock"})])
    client.script(task="overlay.enrich.definition", prompt_id="overlay_definition_v1",
                  responses=[FakeResponse(output={"definition": "A one-line business definition."})])
    client.script(task="overlay.enrich.domain", prompt_id="overlay_domain_v1",
                  responses=[FakeResponse(output={"domain": "payments"})])
    return client


def test_nonpublic_glossary_pass_b_does_not_double_count_or_go_ambiguous(
        overlay_conn, human_actor, monkeypatch):
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    glossary = read_glossary(_GLOSSARY_CSV, source="src")
    client = _client(glossary)

    r1 = ingest_upload(overlay_conn, "src", glossary.rows, actor=human_actor,
                       client=client, glossary=glossary)
    assert r1.status == "ingested"

    # (a) CATALOG readiness must emit EXACTLY ONE grain requirement for the single physical table —
    #     the schema-forced-public advisory ref must not manufacture a second (schema, table) pair.
    rd = compute_readiness(overlay_conn, source="src", scope=ReadinessScopeType.CATALOG)
    grain_reqs = [r for r in rd.blocking_requirements + rd.review_requirements
                  if r.requirement_id.startswith("grain:")]
    assert len(grain_reqs) == 1, [r.requirement_id for r in grain_reqs]

    # (b) a bare TABLE subset must not be ambiguous across a phantom (public, txn) pair.
    rd_tbl = compute_readiness(overlay_conn, source="src", scope=ReadinessScopeType.TABLE,
                               subset="txn")
    assert rd_tbl is not None
