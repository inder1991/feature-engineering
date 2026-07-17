"""Task 10 — the A1 capstone: one full API -> PostgreSQL -> search acceptance test over a
SYNTHESIZED upload modelling the REAL FTR file's shape (round-5 resolution R5-10).

The previous synthetic fixture (5 invented term_types, clean short definitions) missed two real
defects the actual ``FTR_Column_Mapping_final.csv`` run exposed: a 6th term_type
(``Regulatory Term``) was quarantined by the closed vocabulary (R5-1), and 41 definitions saying
"The sample profile has no non-blank values." were blanked by the value-shape guesser (R5-2).
This fixture models the real file's METADATA — content is synthesized, never read from a real
export:

- 127 data rows = 126 three-part column FQNs + 1 two-part table term, all under ONE schema
  (``DPL_EIB_COMPLIANCE``), one table (``COMP_FIN_TRAN``); unique integer source_rows 18..144.
- term_type distribution ``Business Term``x81, ``Dimension``x27, ``Code Value``x9, ``Measure``x6,
  ``Reference Data``x3, ``Regulatory Term``x1 — ALL must ingest (open vocabulary, R5-1).
- 85 definitions carry the canonical ``... The sample profile is <TYPE>, with representative
  values such as A; B; C, which supports interpretation.`` clause (STRIPPED — prose survives);
  41 say ``... The sample profile has no non-blank values.`` (PRESERVED — must NOT blank);
  every definition > 200 chars.
- declared ``data_type`` varies VARCHAR / DOUBLE / TIMESTAMP and must reach the concept
  classifier (R5-5) instead of the operational ``unknown``.

Two sample-bearing definitions carry SYNTHETIC scrub tokens (``ARTKOM``, ``8484848481``) that
must be ABSENT from every persistence/egress surface afterwards: ``graph_node`` (definition,
semantic_terms, search_doc), ``field_evidence``, ``quarantine_row.raw``, and the immutable
``llm_call`` audit records (``redacted_input``) of the enrichment run bucket. (ARTKOM has non-hex
letters and the numeric token is a 10-digit run, so neither can appear by chance inside a sha256
content hash.)

The LLM is a PERMISSIVE fake (`_EchoLLM`): for a batch prompt it echoes one fixed valid value per
REQUESTED ref, for a single prompt the flat shape — the test proves the WIRING (chunked batching
past the 40-item cap, egress redaction, audit records), not specific enrichment values.
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter

from tests.featuregen.api._helpers import AUTH, upload_csv

from featuregen.intake.llm import PROVIDER_OK, LLMRequest, LLMResult
from featuregen.overlay.upload.enrich_llm import ENRICHMENT_RUN_ID
from featuregen.overlay.upload.ingestion_run import RUN_ID_HEADER

# ── Synthesized REAL-SHAPED FTR fixture (R5-10 — programmatic, NEVER a real export) ──────────────

_HDR = ("source_row,schema.table.column,term_name,description_business_definition,data_domain,"
        "term_type,related_business_process_l1,related_terms,related_business_process_l2,"
        "related_business_process_l3,synonyms_aliases,bian_level_1,bian_level_2,bian_level_3,"
        "bian_level_4,fibo_level_1,data_type\n")

_SCHEMA = "DPL_EIB_COMPLIANCE"
_TABLE = "COMP_FIN_TRAN"
_N_COLS = 126
_N_ROWS = 127            # 126 column terms + 1 table term — the honest input row count (R5-9)

# The REAL file's term_type distribution across all 127 rows. The 126 COLUMN rows take the first
# 126 (80 Business Term + the rest); the TABLE term takes the 81st "Business Term".
_TT_COLUMNS: tuple[str, ...] = (("Business Term",) * 80 + ("Dimension",) * 27
                                + ("Code Value",) * 9 + ("Measure",) * 6
                                + ("Reference Data",) * 3 + ("Regulatory Term",))
_TT_DIST = {"Business Term": 81, "Dimension": 27, "Code Value": 9, "Measure": 6,
            "Reference Data": 3, "Regulatory Term": 1}
_N_SAMPLED = 85          # canonical representative-values clause -> STRIPPED
_N_NO_SAMPLE = 41        # "sample profile has no non-blank values" -> PRESERVED, must NOT blank

# Distinctive SYNTHETIC scrub tokens riding inside canonical sample clauses of two rows (0 and 44
# — different concept batch chunks). ENTITY_TOKEN has non-hex letters, NUMERIC_TOKEN is a 10-digit
# run: neither can appear by chance inside the sha256 hashes this ingest mints.
ENTITY_TOKEN = "ARTKOM"
NUMERIC_TOKEN = "8484848481"
_TOKEN_ROWS = {0, 44}

_DATA_TYPES = ("VARCHAR", "DOUBLE", "TIMESTAMP")   # assigned i % 3 — cust_name (i=0) is VARCHAR

# Canonical FTR sample clauses (the shape strip_sample_values excises), varied per declared type.
_CLAUSES = {
    "VARCHAR": (" The sample profile is ALPHA_NUMERIC, with representative values such as "
                "K4M2X9; P7Q1R3; T5W8Z2, which supports interpretation."),
    "DOUBLE": (" The sample profile is NUMERIC, with representative values such as "
               "104.25; 98.10; 250.75, which supports interpretation."),
    "TIMESTAMP": (" The sample profile is ALPHA_SPECIAL, with representative values such as "
                  "2031-01-15 10:01:02; 2031-02-20 15:07:08, which supports interpretation."),
}
_TOKEN_CLAUSE = (" The sample profile is TEXT, with representative values such as "
                 f"{ENTITY_TOKEN} GLOBAL FZE; {NUMERIC_TOKEN}; 9021055512, "
                 "which supports interpretation.")
_NO_SAMPLE_TAIL = " The sample profile has no non-blank values."


def _prose(name: str, term: str) -> str:
    """Comma-bearing business prose > 200 chars on its own (the real file's definitions all exceed
    200 chars) — so EVERY emitted definition is >200 whichever tail it gets, and csv.writer must
    quote every row."""
    return (f"{term} as captured on the {name.lower()} field of the daily compliance transaction "
            "feed, sourced from the finance transaction repository at end of day and reviewed by "
            "the compliance operations team for completeness, accuracy and timeliness before "
            "regulatory submission.")


def _real_shaped_ftr_csv() -> str:
    """The exact FTR header + 126 column rows + one 2-part table term row, modelling the REAL
    file's metadata (module docstring). All commas in definitions are QUOTED via csv.writer;
    source_row values are unique parsed ints 18..144."""
    buf = io.StringIO()
    buf.write(_HDR)
    w = csv.writer(buf, lineterminator="\n")
    for i in range(_N_COLS):
        if i == 0:
            name, term = "CUST_NAME", "Counterparty Legal Name"
            synonyms = "Client Name|Account Holder"
        elif i == _N_COLS - 1:   # the single Regulatory Term column (_TT_COLUMNS[-1])
            name, term = "REG_RPT_CD", "Regulatory Reporting Code"
            synonyms = ""
        else:
            name, term = f"COL_{i}", f"Compliance Attribute {i}"
            synonyms = ""
        dtype = _DATA_TYPES[i % len(_DATA_TYPES)]
        definition = _prose(name, term)
        if i < _N_SAMPLED:       # 85 sample-bearing definitions (canonical clause -> stripped)
            definition += _TOKEN_CLAUSE if i in _TOKEN_ROWS else _CLAUSES[dtype]
        else:                    # 41 "no non-blank values" definitions (must NOT blank)
            definition += _NO_SAMPLE_TAIL
        w.writerow([18 + i, f"{_SCHEMA}.{_TABLE}.{name}", term, definition, "Compliance",
                    _TT_COLUMNS[i], "Monitoring", "", "Screening", "", synonyms, "Compliance",
                    "Transaction", "", "", "fibo-fbc:FinancialTransaction", dtype])
    # The 2-part TABLE term (record-only — no CanonicalRow): the 81st "Business Term".
    w.writerow([18 + _N_COLS, f"{_SCHEMA}.{_TABLE}", "Financial Transaction Repository",
                "Daily compliance transaction repository, covering settled transactions reported "
                "by the finance systems of record, retained for regulatory review and "
                "reconciliation across the compliance monitoring, screening and reporting "
                "processes of the enterprise investment bank.",
                "Compliance", "Business Term", "", "", "", "", "", "Reference", "Table",
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
    monkeypatch.setenv("OVERLAY_PASS_C", "1")                    # flags ON, as in production
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")   # 126 items >> 40-item batch cap
    llm = _EchoLLM()
    client = make_client(llm)

    csv_text = _real_shaped_ftr_csv()
    # ── Fixture self-guards: the CSV really models the REAL file (else every downstream absence /
    # distribution assertion could pass vacuously on a drifted fixture).
    data_rows = list(csv.reader(io.StringIO(csv_text)))[1:]
    assert len(data_rows) == _N_ROWS
    defs = [r[3] for r in data_rows]
    assert all(len(d) > 200 for d in defs)                       # real file: all defs > 200 chars
    assert sum("representative values such as" in d for d in defs) == _N_SAMPLED
    assert sum("no non-blank values" in d for d in defs) == _N_NO_SAMPLE
    assert Counter(r[5] for r in data_rows) == _TT_DIST          # all 6 term_types, real counts
    assert len({r[0] for r in data_rows}) == _N_ROWS             # unique source_row 18..144
    assert csv_text.count(ENTITY_TOKEN) == len(_TOKEN_ROWS)      # the raw CSV DID carry the tokens
    assert NUMERIC_TOKEN in csv_text

    # 1) THE assertion that catches the two real blockers: the Regulatory Term row and all 41
    #    "no non-blank values" rows INGEST — status ingested, NOTHING quarantined, and FTR
    #    declares no grain/as-of facts (honest label).
    res = upload_csv(client, "ftr", csv_text)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ingested"
    assert body["asserted"] == 0
    assert body["quarantined"] == 0

    # 2) Exactly 126 column nodes + 1 table node landed, scoped by catalog_source.
    n_cols = conn.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'ftr' AND kind = 'column'"
    ).fetchone()[0]
    assert n_cols == _N_COLS
    n_tables = conn.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'ftr' AND kind = 'table'"
    ).fetchone()[0]
    assert n_tables == 1

    # 3) EVERY definition is PRESENT — none of the 41 "no non-blank values" definitions was
    #    blanked, and the 85 stripped ones kept their business prose (the second real blocker).
    n_defined = conn.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = %s AND kind = 'column' "
        "AND definition IS NOT NULL AND definition <> ''", ("ftr",)).fetchone()[0]
    assert n_defined == _N_COLS
    # The parse stage's honest sanitize provenance agrees: exactly the 85 canonical clauses were
    # stripped and NOTHING was suppressed/blanked fail-closed (R5-8).
    state, detail = _stage(conn, "parse")
    assert state == "succeeded"
    assert detail.get("definitions_stripped") == _N_SAMPLED, detail
    assert detail.get("definitions_suppressed") == 0, detail
    assert detail.get("rows") == _N_COLS                # the 126 CanonicalRows (table term aside)

    # 4) The Regulatory Term column ingested (the first real blocker: the closed vocabulary
    #    quarantined it) — its node exists with its definition intact.
    reg_def, = conn.execute(
        "SELECT definition FROM graph_node WHERE catalog_source = 'ftr' "
        "AND object_ref = 'public.comp_fin_tran.reg_rpt_cd'").fetchone()
    assert reg_def and "Regulatory Reporting Code" in reg_def

    # The table term resolved onto the (public-flattened) table node; both the table node AND a
    # column node preserve the declared (pre-flatten) schema.
    t_def, t_domain, t_schema = conn.execute(
        "SELECT definition, domain, schema_name FROM graph_node "
        "WHERE catalog_source = 'ftr' AND object_ref = 'public.comp_fin_tran'").fetchone()
    assert t_def is not None and t_def.startswith("Daily compliance transaction repository")
    assert t_domain is not None
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
        # The egressed payload specifically: no enrichment call's immutable redacted_input
        # carries a sample value.
        assert conn.execute(
            "SELECT count(*) FROM llm_call WHERE run_id = %s AND redacted_input::text ILIKE %s",
            (ENRICHMENT_RUN_ID, f"%{token}%")).fetchone()[0] == 0
        # And the search index can never surface it as a query hit.
        assert conn.execute(
            "SELECT count(*) FROM graph_node WHERE catalog_source = 'ftr' "
            "AND search_doc @@ plainto_tsquery('english', %s)", (token,)).fetchone()[0] == 0

    # 6) The DECLARED type reached the classifier (R5-5): every audited concept batch item carries
    #    the file's varchar/double/timestamp, never the operational 'unknown'; the VARCHAR column
    #    cust_name specifically egressed as 'varchar'.
    concept_inputs = conn.execute(
        "SELECT redacted_input FROM llm_call WHERE run_id = %s "
        "AND task = 'overlay.enrich.concept' AND prompt_id = 'overlay_concept_batch_v1'",
        (ENRICHMENT_RUN_ID,)).fetchall()
    items = [it for (ri,) in concept_inputs for it in ri["catalog_metadata"]["items"]]
    assert len(items) == _N_COLS                     # one item per column across the chunks
    assert {it.get("type") for it in items} == {"varchar", "double", "timestamp"}
    assert not any(it.get("type") == "unknown" for it in items)
    by_col = {it["column"].lower(): it for it in items}
    assert by_col["cust_name"]["type"] == "varchar"

    # 7) NON-VACUOUS: enrichment really ran, chunked past the 40-item batch cap — ceil(126/40) = 4
    #    audited concept batch calls minimum.
    total_llm_calls = conn.execute(
        "SELECT count(*) FROM llm_call WHERE run_id = %s", (ENRICHMENT_RUN_ID,)).fetchone()[0]
    assert total_llm_calls >= 1
    assert len(concept_inputs) >= 4         # ceil(126 / 40) — proves chunking, audited durably
    assert llm.concept_batch_calls >= 4     # and the provider actually saw each chunk
    # Every requested item resolved: the concept stage's honest detail saw all 126 columns.
    state, detail = _stage(conn, "enrich_concept")
    assert state == "succeeded", (state, detail)
    assert detail.get("expected") == _N_COLS and detail.get("resolved") == _N_COLS
    for other in ("enrich_definition", "enrich_domain"):
        state, detail = _stage(conn, other)
        assert state == "succeeded", (other, state, detail)

    # 8) The run manifest records the HONEST input row count (R5-9): all 127 data rows — the
    #    table term included — not len(rows).
    run_id = res.headers[RUN_ID_HEADER]
    (row_count,) = conn.execute(
        "SELECT row_count FROM ingestion_run WHERE id = %s", (run_id,)).fetchone()
    assert row_count == _N_ROWS

    # 9) SEARCH — the ingested glossary is findable end-to-end through the API (and, per #5 above,
    #    a scrub token never is).
    hits = client.get("/search", params={"q": "counterparty", "source": "ftr"},
                      headers=AUTH).json()["hits"]
    assert "public.comp_fin_tran.cust_name" in {h["object_ref"] for h in hits}
    assert ENTITY_TOKEN.lower() not in json.dumps(hits).lower()
