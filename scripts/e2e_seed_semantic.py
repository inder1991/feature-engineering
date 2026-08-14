"""Seed the Playwright journey's semantic catalog into FEATUREGEN_DSN.

Run by frontend/e2e/workbench-journey.spec.ts (beforeAll) via `uv run python`. Mirrors the
E0 walkthrough's fixture exactly: one cib-shaped table whose concepts are ALL AI-proposed
(graph stamps + llm/proposed field evidence — the state a freshly ingested catalog is in),
so the browser journey clears REAL blockers through REAL surfaces. Idempotent: build_graph
rebuilds the source; the evidence writes are input-hash keyed.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg

SOURCE = os.environ.get("E2E_SEMANTIC_SOURCE", "e2e_semantic")


def seed(conn) -> None:
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.column_authority import logical_ref_of
    from featuregen.overlay.upload.enrich import content_hash
    from featuregen.overlay.upload.graph import build_graph

    rows = [
        (CanonicalRow(SOURCE, "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(SOURCE, "accounts", "open_date", "timestamp"), "origination_date"),
        (CanonicalRow(SOURCE, "accounts", "complaint_flag", "boolean"), "complaint_event"),
        (CanonicalRow(SOURCE, "accounts", "as_of_date", "timestamp", as_of=True),
         "as_of_date"),
        (CanonicalRow(SOURCE, "accounts", "balance", "numeric",
                      additivity="semi_additive", currency="USD"), "monetary_stock"),
        (CanonicalRow(SOURCE, "accounts", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow(SOURCE, "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(SOURCE, "accounts", "churned", "boolean"), "outcome_label"),
    ]
    build_graph(conn, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    for row, concept in rows:
        logical = logical_ref_of(conn, SOURCE, f"public.{row.table}.{row.column}")
        record_field_evidence(
            conn, logical_ref=logical, field_name="concept", proposed_value=concept,
            producer="llm", strength="proposed", producer_ref="svc:enrichment",
            source_snapshot_id="e2e-seed",
            input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                        material=concept))
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, "
        "last_run_id, head_seq) VALUES (%s, %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (SOURCE, now, now))


def main() -> None:
    dsn = os.environ["FEATUREGEN_DSN"]
    with psycopg.connect(dsn) as conn:
        seed(conn)
        conn.commit()
    print(f"seeded {SOURCE} into {dsn}")


if __name__ == "__main__":
    main()
