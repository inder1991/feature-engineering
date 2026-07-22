"""Task 2 — the deterministic grounding signal (pure, no provider). Seeding mirrors
``test_asset_detail_provenance.py``: ``build_graph`` + ``record_field_evidence`` over a real
``overlay_conn``, no LLM, no writes from ``ground_concept`` itself."""
from __future__ import annotations

from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload.attest.grounding import ground_concept
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph


def _seed_evidence(conn, logical_ref: str, field_name: str, value: str, *, n: int,
                    producer: str = "parser", strength: str = "supported") -> None:
    record_field_evidence(
        conn, logical_ref=logical_ref, field_name=field_name, proposed_value=value,
        producer=producer, strength=strength, producer_ref="test",
        source_snapshot_id="snap", input_hash=f"h{n}",
    )


def test_numeric_column_matching_path_and_sibling_all_pass(overlay_conn) -> None:
    """A numeric column proposed monetary_flow, with parser-type evidence agreeing (numeric) AND an
    attested path/business_term sharing the concept's own name tokens AND a currency sibling column
    in the same table -> every check passes, full coverage, no conflict."""
    source = "grd_ok"
    build_graph(overlay_conn, source, [
        CanonicalRow(source, "trades", "notional", "numeric"),
        CanonicalRow(source, "trades", "currency", "text"),
    ])
    logical_ref = f"{source}::public.trades.notional"
    _seed_evidence(overlay_conn, logical_ref, "semantic_type", "amount", n=1)
    _seed_evidence(overlay_conn, logical_ref, "logical_representation", "decimal", n=2)
    _seed_evidence(overlay_conn, logical_ref, "business_term", "Monetary Flow", n=3,
                    producer="source", strength="attested")

    result = ground_concept(overlay_conn, logical_ref, "monetary_flow")

    assert result.checks == {
        "type_consistency": "pass", "path_agreement": "pass", "sibling_consistency": "pass",
    }
    assert result.coverage == 1.0
    assert result.conflict is False


def test_text_column_proposed_monetary_flow_conflicts_on_type(overlay_conn) -> None:
    """A TEXT column proposed monetary_flow (an additive money amount, implied family numeric) ->
    type_consistency fails and the grounding is flagged in conflict."""
    source = "grd_conflict"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "notional", "text")])
    logical_ref = f"{source}::public.trades.notional"
    _seed_evidence(overlay_conn, logical_ref, "semantic_type", "text", n=1)

    result = ground_concept(overlay_conn, logical_ref, "monetary_flow")

    assert result.checks["type_consistency"] == "fail"
    assert result.conflict is True


def test_no_parser_type_and_no_attested_path_are_absent_not_fail(overlay_conn) -> None:
    """A column with no parser-type evidence and no attested path/business_term evidence at all ->
    those two checks are 'absent' (missing evidence, not a conflict), so coverage is under 1.0 and
    there is still no conflict (a missing signal must never be invented as a failure)."""
    source = "grd_thin"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "notional", "numeric")])
    logical_ref = f"{source}::public.trades.notional"

    result = ground_concept(overlay_conn, logical_ref, "monetary_flow")

    assert result.checks["type_consistency"] == "absent"
    assert result.checks["path_agreement"] == "absent"
    assert result.coverage < 1.0
    assert result.conflict is False


def test_ambiguous_group_leaves_type_consistency_absent_even_with_evidence(overlay_conn) -> None:
    """Honesty check: concepts.py carries no per-concept type-family field, only the coarser 'group'
    — and some groups (e.g. 'identifier') mix physically different shapes (numeric_string vs text),
    so their implied family is deliberately left unmapped. Even with parser evidence PRESENT,
    type_consistency must report 'absent' rather than guess a family for an unmapped group."""
    source = "grd_unmapped"
    build_graph(overlay_conn, source, [CanonicalRow(source, "trades", "external_id", "text")])
    logical_ref = f"{source}::public.trades.external_id"
    _seed_evidence(overlay_conn, logical_ref, "semantic_type", "identifier", n=1)

    result = ground_concept(overlay_conn, logical_ref, "customer_id")

    assert result.checks["type_consistency"] == "absent"
