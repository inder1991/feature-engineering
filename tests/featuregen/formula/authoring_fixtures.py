"""Task 12 — the ONE governed catalog the orchestrator suites author against.

Shared by ``test_authoring.py`` (deterministic plumbing) and ``test_gold_gate.py`` (curated
fixtures), the same way ``c1_fixtures.py`` / ``factories.py`` are shared by the earlier task
suites. Everything is seeded through the REAL machinery — ``build_graph`` for the physical
columns, ``record_field_evidence`` + ``resolve_and_project`` for the governed decisions — never a
flat ``graph_node`` insert that would skip the decision lifecycle ``read_operational_value``
derives its statuses from.

The seeded shape is deliberately the SIMPLEST one that lets every §J operation resolve:

* ``txn_amt`` / ``fee_amt`` — numeric measures with a GOVERNED ``logical_representation``
  (source-ATTESTED evidence, resolved + projected), so a SUM/DIFFERENCE reaches
  ``external_type_required=False``. ``ungoverned_amt`` deliberately has NO type decision, so a SUM
  over it resolves to ``external_type_required=True`` — the carry-forward #2 probe.
* ``cif_id`` — a governed grain key (``grain_fact_event_id``); ``txn_dt`` — a governed as-of column
  (``availability_fact_event_id``), directly usable as ``WindowPolicy.event_time_ref``.
* NO ``unit`` / ``currency`` anywhere, so every resolved policy carries ``unit=None,
  currency=None`` and the curated gold fixtures stay hand-checkable.
"""
from __future__ import annotations

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph

SOURCE = "authored"
TABLE_REF = "authored::public.txns"
REF_AMT = "authored::public.txns.txn_amt"
REF_FEE = "authored::public.txns.fee_amt"
REF_UNGOVERNED = "authored::public.txns.ungoverned_amt"
REF_DT = "authored::public.txns.txn_dt"
REF_CIF = "authored::public.txns.cif_id"
REF_MERCHANT = "authored::public.txns.merchant_id"
REF_STATUS = "authored::public.txns.status_cd"

#: A distinctive stand-in for catalog free text / a raw data value. Nothing the orchestrator writes
#: to the trace (or egresses to a provider) may contain it.
CANARY = "leak-canary-1212"

#: The columns whose ``logical_representation`` is seeded as a GOVERNED decision.
GOVERNED_NUMERIC_REFS: tuple[str, ...] = (REF_AMT, REF_FEE)


def seed_authoring_catalog(db, *, source: str = SOURCE) -> None:
    """Build the governed catalog every orchestrator test authors over (module docstring)."""
    rows = [
        CanonicalRow(source, "txns", "txn_amt", "numeric"),
        CanonicalRow(source, "txns", "fee_amt", "numeric"),
        CanonicalRow(source, "txns", "ungoverned_amt", "numeric"),
        CanonicalRow(source, "txns", "txn_dt", "date", as_of=True),
        CanonicalRow(source, "txns", "cif_id", "text", is_grain=True, entity="customer"),
        CanonicalRow(source, "txns", "merchant_id", "text"),
        CanonicalRow(source, "txns", "status_cd", "text"),
    ]
    build_graph(db, source, rows)

    # The governed FACT links (grain / availability): the provenance read_column_facts derives
    # "governed" authority from for the two SPECIALIZED_FACT fields.
    db.execute(
        "UPDATE graph_node SET grain_fact_event_id = 'ovf_evt_grain' "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.cif_id'", (source,))
    db.execute(
        "UPDATE graph_node SET availability_fact_event_id = 'ovf_evt_asof' "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.txn_dt'", (source,))

    # GOVERNED numeric type on the two measures: source-ATTESTED logical_representation evidence,
    # resolved + projected by the REAL resolver (the only route to status="resolved").
    governed = [f"{source}::public.txns.{c}" for c in ("txn_amt", "fee_amt")]
    for ref in governed:
        _attest(db, ref, "logical_representation", "numeric")
    resolve_and_project(db, source=source, logical_refs=governed)

    # Free text the catalog carries but no tool result, provider payload, or trace payload may echo.
    db.execute(
        "UPDATE graph_node SET definition = %s "
        "WHERE catalog_source = %s AND object_ref = 'public.txns.txn_amt'",
        (f"average balance e.g. {CANARY}", source))
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, now(), 'r', 0) ON CONFLICT (catalog_source) "
        "DO UPDATE SET last_completed_at = now()", (source,))


def _attest(db, logical_ref: str, field_name: str, value: str) -> None:
    record_field_evidence(
        db, logical_ref=logical_ref, field_name=field_name, proposed_value=value,
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="authoring-fixture", source_snapshot_id="authoring-fixture-snap",
        input_hash=field_input_hash(
            logical_ref=logical_ref, field_name=field_name, material=value))
