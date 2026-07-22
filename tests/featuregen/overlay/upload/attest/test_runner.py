"""Task 5 — the shadow RUNNER (composes Tasks 1-4) + the stratified gold-worksheet emit/ingest.
Seeding mirrors ``test_grounding.py``: ``build_graph`` + ``record_field_evidence`` over a real
``overlay_conn``, no LLM except a FakeLLM for the reclassifier."""
from __future__ import annotations

import dataclasses

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.attest import runner as R
from featuregen.overlay.upload.attest.reclassify import _TASK as RECLASSIFY_TASK
from featuregen.overlay.upload.attest.shadow_store import write_gold_label
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

_RAW_SSN_VALUE = "123-45-6789"


def _seed(conn, logical_ref: str, field_name: str, value, *, n: int,
         producer: str = "parser", strength: str = "supported") -> None:
    record_field_evidence(
        conn, logical_ref=logical_ref, field_name=field_name, proposed_value=value,
        producer=producer, strength=strength, producer_ref="test",
        source_snapshot_id="snap", input_hash=f"h{n}")


def _seed_catalog(conn, source: str) -> dict[str, str]:
    """A 3-column, 2-table catalog: a well-grounded monetary column, a currency sibling, and a
    high-risk PII column whose raw-value-embedding description exercises the leak backstop."""
    build_graph(conn, source, [
        CanonicalRow(source, "trades", "notional", "numeric"),
        CanonicalRow(source, "trades", "currency", "text"),
        CanonicalRow(source, "customers", "ssn", "text"),
    ], domains={"trades": "payments", "customers": "compliance"})
    refs = {
        "notional": f"{source}::public.trades.notional",
        "currency": f"{source}::public.trades.currency",
        "ssn": f"{source}::public.customers.ssn",
    }
    n = 0

    def seed(logical_ref, field_name, value, **kw):
        nonlocal n
        n += 1
        _seed(conn, logical_ref, field_name, value, n=n, **kw)

    # notional: well-grounded monetary column, low intrinsic risk.
    seed(refs["notional"], "semantic_type", "amount")
    seed(refs["notional"], "logical_representation", "decimal")
    seed(refs["notional"], "business_term", "Monetary Flow", producer="source", strength="attested")
    seed(refs["notional"], "concept", "monetary_flow", producer="llm", strength="proposed")
    seed(refs["notional"], "sensitivity_floor", "public", producer="taxonomy", strength="proposed")
    seed(refs["notional"], "leakage_anchor", False, producer="taxonomy", strength="proposed")
    seed(refs["notional"], "definition", "The trade notional amount.",
         producer="source", strength="attested")
    seed(refs["notional"], "bian_path", "Payment/Transaction", producer="source", strength="attested")

    # ssn: PII, high intrinsic risk. Description EMBEDS a raw sample-values clause (the FTR-glossary
    # shape strip_sample_values exists to strip) so the worksheet's no-raw-value guarantee is a real
    # regression test, not a vacuous one.
    seed(refs["ssn"], "concept", "kyc_document", producer="llm", strength="proposed")
    seed(refs["ssn"], "sensitivity_floor", "pii", producer="taxonomy", strength="proposed")
    seed(refs["ssn"], "leakage_anchor", False, producer="taxonomy", strength="proposed")
    seed(refs["ssn"], "definition",
         f"The customer's SSN. The sample profile is ALPHA_SPECIAL, with representative values "
         f"such as {_RAW_SSN_VALUE}; 987-65-4321, which supports identity verification.",
         producer="source", strength="attested")

    return refs


def _authority_counts(conn) -> tuple[int, int, int]:
    fe = conn.execute("SELECT count(*) FROM field_evidence").fetchone()[0]
    fde = conn.execute("SELECT count(*) FROM field_decision_event").fetchone()[0]
    gn = conn.execute("SELECT count(*) FROM graph_node").fetchone()[0]
    return fe, fde, gn


# ── run_shadow ──────────────────────────────────────────────────────────────────────────────────
def test_run_shadow_writes_one_observation_per_gold_and_reconciles(overlay_conn) -> None:
    source = "run_src_a"
    refs = _seed_catalog(overlay_conn, source)
    write_gold_label(overlay_conn, catalog_source=source, logical_ref=refs["notional"],
                     field_name="concept", gold_value="monetary_flow", labeller_ids=["l1", "l2"],
                     adjudicated_by="reviewer_1")
    write_gold_label(overlay_conn, catalog_source=source, logical_ref=refs["ssn"],
                     field_name="concept", gold_value="kyc_document", labeller_ids=["l1", "l2"],
                     adjudicated_by="reviewer_1")
    client = FakeLLM(script={RECLASSIFY_TASK: FakeResponse(output={"concept": "monetary_flow"})})

    rec = R.run_shadow(overlay_conn, source, client=client, shadow_run_id="srun_a",
                       gold_version="gv1")

    assert rec.expected == 2 and rec.present == 2
    assert rec.missing == ()
    assert rec.complete is True

    rows = overlay_conn.execute(
        "SELECT logical_ref, field_name, risk_tier, proposer_value, reclassify_value, "
        "reclassify_agrees FROM attestation_shadow_observation WHERE shadow_run_id = %s "
        "ORDER BY logical_ref", ("srun_a",)).fetchall()
    assert len(rows) == 2
    by_ref = {r[0]: r for r in rows}

    notional_row = by_ref[refs["notional"]]
    assert notional_row[1] == "concept"
    assert notional_row[2] == "low"                 # public sensitivity, no leakage anchor
    assert notional_row[3] == "monetary_flow"        # proposer's llm-evidence value
    assert notional_row[4] == "monetary_flow"        # FakeLLM reclassifier response
    assert notional_row[5] is True                   # agrees

    ssn_row = by_ref[refs["ssn"]]
    assert ssn_row[2] == "high"                       # sensitivity_floor = pii
    assert ssn_row[3] == "kyc_document"
    assert ssn_row[4] == "monetary_flow"               # same FakeLLM response -> disagreement here
    assert ssn_row[5] is False


def test_run_shadow_writes_no_authority_state(overlay_conn) -> None:
    """MEASURE-ONLY invariant: zero rows added to field_evidence / field_decision_event /
    graph_node across the whole run (snapshot counts before/after)."""
    source = "run_src_b"
    refs = _seed_catalog(overlay_conn, source)
    write_gold_label(overlay_conn, catalog_source=source, logical_ref=refs["notional"],
                     field_name="concept", gold_value="monetary_flow", labeller_ids=["l1"],
                     adjudicated_by="reviewer_1")
    write_gold_label(overlay_conn, catalog_source=source, logical_ref=refs["ssn"],
                     field_name="concept", gold_value="kyc_document", labeller_ids=["l1"],
                     adjudicated_by="reviewer_1")
    client = FakeLLM(script={RECLASSIFY_TASK: FakeResponse(output={"concept": "monetary_flow"})})

    before = _authority_counts(overlay_conn)
    R.run_shadow(overlay_conn, source, client=client, shadow_run_id="srun_b", gold_version="gv1")
    after = _authority_counts(overlay_conn)

    assert after == before


def test_run_shadow_no_gold_labels_reconciles_empty(overlay_conn) -> None:
    source = "run_src_c"
    _seed_catalog(overlay_conn, source)
    client = FakeLLM(script={RECLASSIFY_TASK: FakeResponse(output={"concept": "monetary_flow"})})

    rec = R.run_shadow(overlay_conn, source, client=client, shadow_run_id="srun_c",
                       gold_version="gv1")

    assert rec.expected == 0 and rec.present == 0
    assert rec.complete is True


# ── emit_gold_worksheet / ingest_gold_worksheet ────────────────────────────────────────────────
def test_emit_gold_worksheet_returns_all_columns_when_below_size(overlay_conn) -> None:
    source = "run_src_d"
    refs = _seed_catalog(overlay_conn, source)

    rows = R.emit_gold_worksheet(overlay_conn, source, size=120, seed=42)

    sampled_refs = {r.logical_ref for r in rows}
    assert sampled_refs == set(refs.values())        # catalog (3 cols) < size -> every column
    fields_per_ref: dict[str, set[str]] = {}
    for row in rows:
        fields_per_ref.setdefault(row.logical_ref, set()).add(row.field_name)
    assert all(fields == {"concept", "sensitivity"} for fields in fields_per_ref.values())


def test_emit_gold_worksheet_stratified_subset_is_deterministic(overlay_conn) -> None:
    source = "run_src_e"
    _seed_catalog(overlay_conn, source)

    first = R.emit_gold_worksheet(overlay_conn, source, size=2, seed=7)
    second = R.emit_gold_worksheet(overlay_conn, source, size=2, seed=7)
    third = R.emit_gold_worksheet(overlay_conn, source, size=2, seed=99)

    assert first == second                            # same seed -> byte-identical worksheet
    first_refs = {r.logical_ref for r in first}
    assert len(first_refs) == 2                        # size=2 columns -> 2 distinct logical_refs
    third_refs = {r.logical_ref for r in third}
    assert len(third_refs) == 2
    # Not asserting first != third (a 3-column catalog with 2 strata may coincide), just that both
    # are valid, differently-seeded, deterministic draws.


def test_emit_gold_worksheet_payload_excludes_ai_concept_and_raw_values(overlay_conn) -> None:
    source = "run_src_f"
    refs = _seed_catalog(overlay_conn, source)

    rows = R.emit_gold_worksheet(overlay_conn, source, size=120, seed=1)

    for row in rows:
        rendered = repr(row)
        assert "monetary_flow" not in rendered         # the AI's proposed concept for notional
        assert "kyc_document" not in rendered           # the AI's proposed concept for ssn
        assert _RAW_SSN_VALUE not in rendered            # the raw value embedded in ssn's description
        assert "987-65-4321" not in rendered             # the second raw value in that same clause

    ssn_rows = [r for r in rows if r.logical_ref == refs["ssn"]]
    assert ssn_rows
    for row in ssn_rows:
        # strip_sample_values excises the WHOLE "sample profile is ... values such as ...,
        # which ..." clause (including its trailing interpretation prose) — only the business
        # sentence ahead of it survives; no raw value anywhere in the residual.
        assert row.definition == "The customer's SSN."
        assert _RAW_SSN_VALUE not in row.definition


def test_ingest_gold_worksheet_writes_only_adjudicated_rows(overlay_conn) -> None:
    source = "run_src_g"
    refs = _seed_catalog(overlay_conn, source)
    rows = R.emit_gold_worksheet(overlay_conn, source, size=120, seed=3)
    concept_rows = [r for r in rows if r.field_name == "concept"]
    adjudicated = []
    for row in concept_rows:
        gold = "monetary_flow" if row.logical_ref == refs["notional"] else None
        if gold is None:
            adjudicated.append(row)                     # left un-adjudicated on purpose
            continue
        adjudicated.append(dataclasses.replace(
            row, gold_value=gold, labeller_ids=("l1", "l2"), adjudicated_by="reviewer_1"))

    written = R.ingest_gold_worksheet(overlay_conn, adjudicated)

    assert written == 1
    label = overlay_conn.execute(
        "SELECT gold_value, adjudicated_by FROM attestation_gold_label "
        "WHERE logical_ref = %s AND field_name = 'concept'", (refs["notional"],)).fetchone()
    assert label == ("monetary_flow", "reviewer_1")
    missing = overlay_conn.execute(
        "SELECT count(*) FROM attestation_gold_label WHERE logical_ref = %s",
        (refs["ssn"],)).fetchone()[0]
    assert missing == 0


def test_ingest_gold_worksheet_is_idempotent(overlay_conn) -> None:
    source = "run_src_h"
    refs = _seed_catalog(overlay_conn, source)
    row = R.WorksheetRow(catalog_source=source, logical_ref=refs["notional"], field_name="concept",
                         column_name="notional", gold_value="monetary_flow",
                         labeller_ids=("l1",), adjudicated_by="reviewer_1")

    first = R.ingest_gold_worksheet(overlay_conn, [row])
    second = R.ingest_gold_worksheet(overlay_conn, [row])   # re-ingest same adjudicated row

    assert first == 1 and second == 1                        # write_gold_label no-ops the duplicate
    n = overlay_conn.execute(
        "SELECT count(*) FROM attestation_gold_label WHERE logical_ref = %s AND field_name = %s",
        (refs["notional"], "concept")).fetchone()[0]
    assert n == 1
