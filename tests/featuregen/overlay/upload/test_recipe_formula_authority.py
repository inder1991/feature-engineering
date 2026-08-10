from __future__ import annotations

from types import SimpleNamespace

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.recipe_formula_authority import (
    FormulaAuthorityEnvelopeV1,
    FormulaAuthorityRejection,
    build_formula_authority_envelope,
    verify_formula_authority_envelope,
)


def _record_concept(db, ref: str, value: str) -> None:
    record_field_evidence(
        db,
        logical_ref=ref,
        field_name="concept",
        proposed_value=value,
        producer=EvidenceProducer.HUMAN,
        strength=AssertionStrength.CONFIRMED,
        producer_ref="human:test",
        source_snapshot_id="snapshot-1",
        input_hash=field_input_hash(
            logical_ref=ref, field_name="concept", material=value),
    )


def _record_event_time(db, source: str, ref: str) -> None:
    record_field_evidence(
        db,
        logical_ref=ref,
        field_name="temporal_role",
        proposed_value="event",
        producer=EvidenceProducer.HUMAN,
        strength=AssertionStrength.CONFIRMED,
        producer_ref="human:test",
        source_snapshot_id="snapshot-1",
        input_hash=field_input_hash(
            logical_ref=ref, field_name="temporal_role", material="event"),
    )
    resolve_and_project(
        db, source=source, logical_refs=[ref], fields=["temporal_role"])


def _context(source: str, refs: dict[str, str]):
    concepts = {
        "amount": "monetary_flow",
        "event": "event_timestamp",
        "entity": "customer_id",
    }
    return SimpleNamespace(
        recipe_candidate_key="candidate-1",
        need_bindings=tuple(
            SimpleNamespace(
                role=role,
                logical_ref=ref,
                expected_concept=concepts[role],
            )
            for role, ref in refs.items()
        ),
    )


def _expectation(refs: dict[str, str]):
    return SimpleNamespace(
        expressions=(
            SimpleNamespace(
                operand_ref=refs["amount"],
                event_time_ref=refs["event"],
            ),
        ),
        grain_key_refs=(refs["entity"],),
    )


def test_authority_envelope_uses_raw_concept_evidence_and_verified_grain(db):
    source = "authority"
    rows = [
        CanonicalRow(source, "txn", "amount", "numeric"),
        CanonicalRow(source, "txn", "event_ts", "timestamp"),
        CanonicalRow(source, "txn", "customer_id", "string", is_grain=True),
    ]
    build_graph(db, source, rows)
    refs = {
        "amount": normalize_ref(source, None, "txn", "amount"),
        "event": normalize_ref(source, None, "txn", "event_ts"),
        "entity": normalize_ref(source, None, "txn", "customer_id"),
    }
    for role, ref in refs.items():
        _record_concept(
            db,
            ref,
            {
                "amount": "monetary_flow",
                "event": "event_timestamp",
                "entity": "customer_id",
            }[role],
        )
    _record_event_time(db, source, refs["event"])
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id='grain-event-1' "
        "WHERE catalog_source=%s AND object_ref='public.txn.customer_id'",
        (source,),
    )

    result = build_formula_authority_envelope(
        db,
        context=_context(source, refs),
        expectation=_expectation(refs),
    )
    assert isinstance(result, FormulaAuthorityEnvelopeV1)
    assert len(result.bindings) == 3
    assert result.grain_facts[0]["fact_event_id"] == "grain-event-1"
    assert result.event_time_facts[0]["temporal_role"] == "event"
    assert result.content_hash
    assert verify_formula_authority_envelope(db, result.to_json()) is None
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id='grain-event-changed' "
        "WHERE catalog_source=%s AND object_ref='public.txn.customer_id'",
        (source,),
    )
    assert (
        verify_formula_authority_envelope(db, result.to_json())
        == "GRAIN_AUTHORITY_DRIFT"
    )


def test_authority_envelope_rejects_recipe_to_evidence_concept_mismatch(db):
    source = "authority_mismatch"
    build_graph(db, source, [CanonicalRow(source, "txn", "amount", "numeric")])
    ref = normalize_ref(source, None, "txn", "amount")
    _record_concept(db, ref, "monetary_stock")
    context = SimpleNamespace(
        recipe_candidate_key="candidate-2",
        need_bindings=(
            SimpleNamespace(
                role="amount",
                logical_ref=ref,
                expected_concept="monetary_flow",
            ),
        ),
    )
    expectation = SimpleNamespace(
        expressions=(SimpleNamespace(operand_ref=ref, event_time_ref=ref),),
        grain_key_refs=(),
    )

    result = build_formula_authority_envelope(
        db,
        context=context,
        expectation=expectation,
    )
    assert result == FormulaAuthorityRejection(
        "CONCEPT_BINDING_MISMATCH",
        logical_ref=ref,
        role="amount",
    )


def test_authority_envelope_rejects_event_time_without_operational_role(db):
    source = "authority_no_event"
    rows = [
        CanonicalRow(source, "txn", "amount", "numeric"),
        CanonicalRow(source, "txn", "event_ts", "timestamp"),
        CanonicalRow(source, "txn", "customer_id", "string", is_grain=True),
    ]
    build_graph(db, source, rows)
    refs = {
        "amount": normalize_ref(source, None, "txn", "amount"),
        "event": normalize_ref(source, None, "txn", "event_ts"),
        "entity": normalize_ref(source, None, "txn", "customer_id"),
    }
    for role, ref in refs.items():
        _record_concept(
            db,
            ref,
            {
                "amount": "monetary_flow",
                "event": "event_timestamp",
                "entity": "customer_id",
            }[role],
        )
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id='grain-event-2' "
        "WHERE catalog_source=%s AND object_ref='public.txn.customer_id'",
        (source,),
    )

    result = build_formula_authority_envelope(
        db,
        context=_context(source, refs),
        expectation=_expectation(refs),
    )
    assert result == FormulaAuthorityRejection(
        "EVENT_TIME_AUTHORITY_NO_DECISION",
        logical_ref=refs["event"],
        role="event",
    )


def test_authority_envelope_rejects_concept_compatible_but_economic_role_unproven(db):
    """BR-5: the formula path consumes the SAME governed economic-role evidence as the binding
    policy — a concept-compatible column with no human-confirmed economic role cannot verify a
    role that demands one, and the additive parameter leaves every legacy caller byte-identical."""
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence

    source = "authority_econ"
    rows = [
        CanonicalRow(source, "txn", "amount", "numeric"),
        CanonicalRow(source, "txn", "event_ts", "timestamp"),
        CanonicalRow(source, "txn", "customer_id", "string", is_grain=True),
    ]
    build_graph(db, source, rows)
    refs = {
        "amount": normalize_ref(source, None, "txn", "amount"),
        "event": normalize_ref(source, None, "txn", "event_ts"),
        "entity": normalize_ref(source, None, "txn", "customer_id"),
    }
    for role, ref in refs.items():
        _record_concept(db, ref, {"amount": "monetary_flow", "event": "event_timestamp",
                                  "entity": "customer_id"}[role])
    _record_event_time(db, source, refs["event"])
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id='grain-event-econ' "
        "WHERE catalog_source=%s AND object_ref='public.txn.customer_id'", (source,))

    demanded = {refs["amount"]: "settlement_flow"}
    rejected = build_formula_authority_envelope(
        db, context=_context(source, refs), expectation=_expectation(refs),
        required_economic_roles=demanded)
    assert isinstance(rejected, FormulaAuthorityRejection)
    assert rejected.reason == "ECONOMIC_ROLE_UNPROVEN"
    assert rejected.logical_ref == refs["amount"]

    # human-confirmed evidence — and only that — satisfies it, through the same store
    record_field_evidence(
        db, logical_ref=refs["amount"], field_name="economic_role",
        proposed_value="settlement_flow", producer="human", strength="confirmed",
        producer_ref="user:sme", source_snapshot_id="snap-econ",
        input_hash=field_input_hash(logical_ref=refs["amount"], field_name="economic_role",
                                    material="settlement_flow"))
    accepted = build_formula_authority_envelope(
        db, context=_context(source, refs), expectation=_expectation(refs),
        required_economic_roles=demanded)
    assert isinstance(accepted, FormulaAuthorityEnvelopeV1)
