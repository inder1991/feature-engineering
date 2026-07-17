"""Task 10 — the A1 capstone: one full API -> PostgreSQL -> search acceptance test over a
SYNTHESIZED realistic FTR upload (126 column terms + 1 table term), proving the whole adapter
works end-to-end and is sample-safe (round-4 resolutions #11/#12).

The fixture is generated programmatically (never read from a real export): the exact 17-header FTR
layout, unique integer source_rows, term_type varied across the closed vocabulary, and a few
definitions carrying recognized/unrecognized sample clauses with SYNTHETIC scrub tokens
(``ARTKOM``, ``8484848481``) that must be ABSENT from every persistence/egress surface afterwards:
``graph_node`` (definition, semantic_terms, search_doc), ``field_evidence``, ``quarantine_row``,
and the immutable ``llm_call`` audit records (``redacted_input``) of the enrichment run bucket.

The LLM is a PERMISSIVE fake (`_EchoLLM`): for a batch prompt it echoes one fixed valid value per
REQUESTED ref (whatever the chunk asked for), for a single prompt it returns the flat shape — the
test proves the WIRING (chunked batching past the 40-item cap, egress redaction, audit records),
not specific enrichment values. Non-vacuousness is asserted explicitly: the concept stage saw all
126 columns and >= 2 audited batch calls occurred, so "no leaked payload" is never a vacuous pass.
"""
from __future__ import annotations

import csv
import io
import json

from tests.featuregen.api._helpers import AUTH, upload_csv

from featuregen.intake.llm import PROVIDER_OK, LLMRequest, LLMResult
from featuregen.overlay.upload.enrich_llm import ENRICHMENT_RUN_ID

# ── Synthesized FTR fixture (resolution #12 — programmatic, realistic, NEVER a real export) ──────

_HDR = ("source_row,schema.table.column,term_name,description_business_definition,data_domain,"
        "term_type,related_business_process_l1,related_terms,related_business_process_l2,"
        "related_business_process_l3,synonyms_aliases,bian_level_1,bian_level_2,bian_level_3,"
        "bian_level_4,fibo_level_1,data_type\n")

_SCHEMA = "DPL_EIB_COMPLIANCE"
_TABLE = "COMP_FIN_TRAN"
_N_COLS = 126

# Distinctive SYNTHETIC scrub tokens. ENTITY_TOKEN has non-hex letters so it can never appear by
# chance inside a sha256 content hash; NUMERIC_TOKEN is a 10-digit run for the same reason (a
# short digit group like "84848" occurs in ~0.06% of 64-char hex hashes — a real flake risk given
# the hundreds of hashes this ingest mints into refs/producer_item_refs/redacted_input).
ENTITY_TOKEN = "ARTKOM"
NUMERIC_TOKEN = "8484848481"

# Three sample-bearing definitions exercising all sanitizer paths (verified against sanitize.py):
# recognized clause with entity values -> STRIPPED; recognized clause with numeric values ->
# STRIPPED; an unrecognized residual value list -> the whole field BLANKED (suspected_value_list).
_D_ENTITY = ("Registered legal name of the counterparty entity. The sample profile is TEXT, with "
             f"representative values such as {ENTITY_TOKEN} GLOBAL FZE; NORDIC HOLDINGS AS, "
             "which supports interpretation.")
_D_NUMERIC = ("Internal ledger movement identifier. The sample profile is NUMERIC, with "
              f"representative values such as {NUMERIC_TOKEN}; 9021055512, which supports "
              "interpretation.")
_D_RESIDUAL = f"Reconciliation batch counter; the values were {NUMERIC_TOKEN}; 9021055512."

_TERM_TYPES = ("Measure", "Dimension", "Code Value", "Reference Data", "Business Term")
_DATA_TYPES = ("VARCHAR", "DECIMAL", "DATE", "INTEGER", "CHAR(3)")


def _ftr_csv(n_cols: int = _N_COLS) -> str:
    """The exact FTR header + ``n_cols`` column rows + one 2-part table term row. Rows are written
    via csv.writer so every comma-bearing definition is QUOTED (resolution #11). source_row values
    are unique parsed ints (18 .. 18+n_cols)."""
    buf = io.StringIO()
    buf.write(_HDR)
    w = csv.writer(buf, lineterminator="\n")
    for i in range(n_cols):
        if i == 0:
            name, term, definition, ttype = ("CUST_NAME", "Counterparty Legal Name",
                                             _D_ENTITY, "Dimension")
            synonyms = "Client Name|Account Holder"
        elif i == 1:
            name, term, definition, ttype = ("TXN_REF_NBR", "Transaction Reference Number",
                                             _D_NUMERIC, "Business Term")
            synonyms = "Txn Ref"
        elif i == 2:
            name, term, definition, ttype = ("RECON_BATCH_CT", "Reconciliation Batch Count",
                                             _D_RESIDUAL, "Measure")
            synonyms = ""
        else:
            name = f"COL_{i}"
            term = f"Compliance Attribute {i}"
            # Plain, comma-bearing prose: stays searchable, never trips the value-shape gate.
            definition = (f"Business attribute recording the {name.lower()} field, as reported "
                          "for daily compliance monitoring.")
            ttype = _TERM_TYPES[i % len(_TERM_TYPES)]   # several Measure rows among them
            synonyms = ""
        w.writerow([18 + i, f"{_SCHEMA}.{_TABLE}.{name}", term, definition, "Compliance", ttype,
                    "Monitoring", "", "Screening", "", synonyms, "Compliance", "Transaction",
                    "", "", "fibo-fbc:FinancialTransaction", _DATA_TYPES[i % len(_DATA_TYPES)]])
    # The 2-part TABLE term (record-only — no CanonicalRow, no grain/as-of assertion).
    w.writerow([18 + n_cols, f"{_SCHEMA}.{_TABLE}", "Financial Transaction Repository",
                "Daily compliance transaction repository, covering settled transactions.",
                "Compliance", "Reference Data", "", "", "", "", "", "Reference", "Table",
                "", "", "", ""])
    return buf.getvalue()


# ── Permissive FakeLLM: echoes a fixed valid value for whatever refs each prompt requested ───────

_ANSWERS = {
    "overlay.enrich.concept": ("concept", "monetary_stock"),          # a known vocabulary concept
    "overlay.enrich.definition": ("definition", "A drafted business definition."),
    "overlay.enrich.domain": ("domain", "compliance"),
}


class _EchoLLM:
    """LLMClient that satisfies every enrichment prompt: batch prompts get one valid result per
    REQUESTED ref (read from the request's ``catalog_metadata.items``, so 126-column chunking is
    exercised without scripting 126 exact content hashes); single prompts get the flat shape."""

    def __init__(self) -> None:
        self.concept_batch_calls = 0

    def call(self, request: LLMRequest) -> LLMResult:
        out_key, value = _ANSWERS[request.task]
        if "batch" in request.prompt_id:
            items = request.inputs["catalog_metadata"].get("items", [])
            if request.task == "overlay.enrich.concept":
                self.concept_batch_calls += 1
            output = {"results": [{"ref": it["ref"], out_key: value} for it in items]}
        else:
            output = {out_key: value}
        return LLMResult(output=output, self_reported_scores={}, call_ref="", status=PROVIDER_OK)


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────────

def _count_matching(conn, table: str, token: str, where: str = "TRUE", params: tuple = ()) -> int:
    """Rows of ``table`` whose ENTIRE row text carries ``token`` — the strongest absence probe
    (covers every column at once: definitions, semantic_terms, search_doc lexemes, jsonb raw,
    evidence values/spans, audit payloads)."""
    return conn.execute(
        f"SELECT count(*) FROM {table} t WHERE {where} AND t::text ILIKE %s",  # noqa: S608
        (*params, f"%{token}%")).fetchone()[0]


def _stage(conn, stage: str) -> tuple[str, dict]:
    state, detail = conn.execute(
        "SELECT s.state, s.detail FROM ingestion_run_stage s "
        "JOIN ingestion_run r ON r.id = s.ingestion_run_id "
        "WHERE r.catalog_source = 'ftr' AND s.stage = %s "
        "ORDER BY s.id DESC LIMIT 1", (stage,)).fetchone()
    return state, detail or {}


# ── The capstone acceptance test ─────────────────────────────────────────────────────────────────

def test_ftr_full_api_pg_search_acceptance(make_client, conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")                    # resolution #11 — flags ON
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")   # 126 items >> 40-item batch cap
    llm = _EchoLLM()
    client = make_client(llm)

    csv_text = _ftr_csv()
    # Guard against fixture drift making sample-safety vacuous: the raw upload DOES carry both
    # scrub tokens (and a Measure row) before we assert their absence downstream.
    assert ENTITY_TOKEN in csv_text and NUMERIC_TOKEN in csv_text
    assert ",Measure," in csv_text

    # 1) Upload ingests cleanly: no fake grain/as-of facts (#8 honest label), nothing quarantined.
    res = upload_csv(client, "ftr", csv_text)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ingested"
    assert body["asserted"] == 0        # FTR declares no grain/as-of facts
    assert body["quarantined"] == 0

    # 2) Exactly 126 column nodes landed, scoped by catalog_source.
    n_cols = conn.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'ftr' AND kind = 'column'"
    ).fetchone()[0]
    assert n_cols == _N_COLS
    n_tables = conn.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'ftr' AND kind = 'table'"
    ).fetchone()[0]
    assert n_tables == 1

    # 3) The table term resolved onto the (public-flattened) table node: definition projected from
    #    the dedicated table-term evidence path, domain classified by enrichment.
    t_def, t_domain, t_schema = conn.execute(
        "SELECT definition, domain, schema_name FROM graph_node "
        "WHERE catalog_source = 'ftr' AND object_ref = 'public.comp_fin_tran'").fetchone()
    assert t_def is not None and t_def.startswith("Daily compliance transaction repository")
    assert t_domain is not None

    # 4) Both the table node AND a column node preserve the declared (pre-flatten) schema.
    assert t_schema == _SCHEMA
    (c_schema,) = conn.execute(
        "SELECT schema_name FROM graph_node "
        "WHERE catalog_source = 'ftr' AND object_ref = 'public.comp_fin_tran.cust_name'"
    ).fetchone()
    assert c_schema == _SCHEMA

    # 5) SAMPLE-SAFETY — the scrub tokens are absent from EVERY surface. Whole-row text scans
    #    cover graph_node.definition/semantic_terms/search_doc::text, the field_evidence value +
    #    span columns, quarantine_row.raw::text, and the llm_call rows of the enrichment bucket
    #    (redacted_input + raw_output + input_redaction) in one strongest-form probe each.
    for token in (ENTITY_TOKEN, NUMERIC_TOKEN):
        assert _count_matching(conn, "graph_node", token) == 0
        assert _count_matching(conn, "field_evidence", token) == 0
        assert _count_matching(conn, "quarantine_row", token) == 0
        assert _count_matching(conn, "llm_call", token,
                               where="t.run_id = %s", params=(ENRICHMENT_RUN_ID,)) == 0
        # The egressed payload specifically (resolution #11's audit check): no enrichment call's
        # immutable redacted_input carries a sample value.
        assert conn.execute(
            "SELECT count(*) FROM llm_call WHERE run_id = %s AND redacted_input::text ILIKE %s",
            (ENRICHMENT_RUN_ID, f"%{token}%")).fetchone()[0] == 0
        # And the search index can never surface it as a query hit.
        assert conn.execute(
            "SELECT count(*) FROM graph_node WHERE catalog_source = 'ftr' "
            "AND search_doc @@ plainto_tsquery('english', %s)", (token,)).fetchone()[0] == 0

    # 6) NON-VACUOUS: enrichment really ran, chunked past the 40-item batch cap. ceil(126/40) = 4
    #    chunks minimum, each one audited llm_call — so >= 2 batch calls is guaranteed with margin.
    total_llm_calls = conn.execute(
        "SELECT count(*) FROM llm_call WHERE run_id = %s", (ENRICHMENT_RUN_ID,)).fetchone()[0]
    assert total_llm_calls >= 1
    concept_batch_audits = conn.execute(
        "SELECT count(*) FROM llm_call WHERE run_id = %s AND task = 'overlay.enrich.concept' "
        "AND prompt_id = 'overlay_concept_batch_v1'", (ENRICHMENT_RUN_ID,)).fetchone()[0]
    assert concept_batch_audits >= 4        # ceil(126 / 40) — proves chunking, audited durably
    assert llm.concept_batch_calls >= 4     # and the provider actually saw each chunk
    # Every requested item resolved: the concept stage's honest detail saw all 126 columns.
    state, detail = _stage(conn, "enrich_concept")
    assert state == "succeeded", (state, detail)
    assert detail.get("expected") == _N_COLS and detail.get("resolved") == _N_COLS
    for other in ("enrich_definition", "enrich_domain"):
        state, detail = _stage(conn, other)
        assert state == "succeeded", (other, state, detail)
    # The parse stage recorded the sanitize provenance: >= 3 clauses stripped/blanked (the three
    # sample-bearing definitions), so the sanitizer provably fired on this upload.
    state, detail = _stage(conn, "parse")
    assert state == "succeeded"
    assert detail.get("sanitized_clauses", 0) >= 3, detail

    # 7) SEARCH — the ingested glossary is findable end-to-end through the API (and, per #5 above,
    #    a scrub token never is).
    hits = client.get("/search", params={"q": "counterparty", "source": "ftr"},
                      headers=AUTH).json()["hits"]
    assert "public.comp_fin_tran.cust_name" in {h["object_ref"] for h in hits}
    assert ENTITY_TOKEN.lower() not in json.dumps(hits).lower()
