"""E2 — the Workbench serving budgets, red-lined on a CIB-sized catalog.

SE-0 measured the live cib catalog at 237 read-scoped columns; this fixture rebuilds that
shape (240 columns over 12 tables) and asserts NAMED budgets on one scoped considered-set
request under semantic_v1:

* **SQL** — total statements on the request connection. The B6/C1 architecture makes the
  engine O(fact families), never O(recipes) or O(columns); the budget catches a regression
  back to per-candidate reads.
* **Provider calls** — exactly the audited intent call + the advisory recommend_set. The
  retired free-form generator can never sneak a third call back in (GEN-01's tail).
* **Latency** — a generous wall ceiling for the whole request on this fixture. Not a p95
  microbenchmark (CI machines vary); a red line that catches an order-of-magnitude cliff.
"""
from __future__ import annotations

import time

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

CHURN = "customer.relationship_attrition.churn"
SOURCE = "budget_cib"

#: The red lines. Raising one is a REVIEWED change with a reason, never a quiet bump.
#: Measured composition at pinning (2026-08-14, 502 total): ~234 append-only PRODUCT writes
#: (one observation + one decision row per served candidate — the record IS the product),
#: ~185 tie-break replay lookups (bounded by GENUINE same-concept ties — this fixture is
#: deliberately tie-pathological with 16 same-concept columns per table; real catalogs have
#: distinct meanings), ~60 idempotent registry upserts, and ~20 fixed reads (the B6/C1
#: batched compile: capability evidence + revalidations + reviews + licences, constant in
#: candidate count).
#: T2 (2026-08-24) moved that composition DOWN, to 391 measured on this fixture: no candidate
#: on the cib shape binds all of its required operands, so no option id and no decision row is
#: minted for one. The budget is a ceiling and is deliberately NOT lowered to match — it exists
#: to catch a per-candidate read coming back, and a catalog that does serve cards must still
#: fit under it.
SQL_BUDGET = 600
PROVIDER_CALL_BUDGET = 2
LATENCY_BUDGET_SECONDS = 20.0


def _cib_sized_catalog(conn) -> None:
    """240 columns over 12 tables — the SE-0 measured shape, deterministic."""
    from datetime import UTC, datetime

    rows = []
    concept_cycle = ("monetary_flow", "monetary_stock", "category_code", "boolean_flag",
                     "free_text", "quantity")
    for t in range(12):
        table = f"cib_table_{t:02d}"
        rows.append((CanonicalRow(SOURCE, table, "cust_num", "integer", is_grain=True,
                                  entity="Customer"), "customer_id"))
        rows.append((CanonicalRow(SOURCE, table, "event_ts", "timestamp"),
                     "event_timestamp"))
        rows.append((CanonicalRow(SOURCE, table, "as_of_date", "timestamp", as_of=True),
                     "as_of_date"))
        rows.append((CanonicalRow(SOURCE, table, "complaint_flag", "boolean"),
                     "complaint_event"))
        for c in range(16):
            concept = concept_cycle[c % len(concept_cycle)]
            extra = ({"additivity": "additive", "currency": "USD"}
                     if concept == "monetary_flow"
                     else {"additivity": "semi_additive", "currency": "USD"}
                     if concept == "monetary_stock" else {})
            rows.append((CanonicalRow(SOURCE, table, f"col_{t:02d}_{c:02d}", "numeric",
                                      **extra), concept))
    assert len(rows) == 240
    build_graph(conn, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, "
        "last_run_id, head_seq) VALUES (%s, %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (SOURCE, now, now))


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "fits"}),
        "overlay.feature.intents": FakeResponse(output={"intents": []}),
    })


def test_the_considered_set_stays_inside_its_budgets(make_client, conn, monkeypatch):
    _cib_sized_catalog(conn)
    fake = _fake()
    client = make_client(llm_client=fake)

    statements: list[str] = []
    original_execute = conn.execute

    def counting_execute(query, *args, **kwargs):
        statements.append(str(query))
        return original_execute(query, *args, **kwargs)

    conn.execute = counting_execute
    started = time.monotonic()
    try:
        res = client.post("/contract/considered-set", json={
            "hypothesis": "customers churn when balances drop over 90 days",
            "objective": "predict churn", "catalog_source": SOURCE,
            "contract_version": 2,
            "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
        }, headers=AUTH)
    finally:
        conn.execute = original_execute
    elapsed = time.monotonic() - started
    assert res.status_code == 200, res.text
    body = res.json()
    # T2: this fixture rebuilds the LIVE cib shape, and on that shape no candidate binds all
    # of its required operands — which is the finding the 2026-08-24 audit made against the
    # real thing (135 cards served, SME-keep 0/135). So "the engine served candidates" is not
    # the guard any more; it was the defect, asserted. The guard that keeps the budgets below
    # non-vacuous is that the engine DID the work and answered for every candidate it saw.
    assert body["needs_setup"], "the engine answered on the CIB-sized shape"
    assert not body["alternatives"], (
        "nothing on this catalog can compute — a card here would be the audited defect")

    provider_calls = sum(fake._calls.values())
    assert provider_calls <= PROVIDER_CALL_BUDGET, (
        f"{provider_calls} provider calls — the budget is {PROVIDER_CALL_BUDGET} "
        "(one audited intent call + the advisory recommend_set; the retired free-form "
        "generator can never sneak a third back in)")
    assert len(statements) <= SQL_BUDGET, (
        f"{len(statements)} SQL statements — the budget is {SQL_BUDGET}. The engine is "
        "O(fact families), never O(candidates); look for a per-candidate read that "
        "escaped the batched compile.")
    reads = [q for q in statements if str(q).lstrip().upper().startswith("SELECT")
             and "structured_result" not in str(q)]
    assert len(reads) <= 40, (
        f"{len(reads)} non-replay SELECTs — the batched compile's fixed read set grew; "
        "look for a per-candidate read that escaped B6/C1")
    assert elapsed <= LATENCY_BUDGET_SECONDS, (
        f"{elapsed:.1f}s for one considered-set request on the CIB-sized fixture — the "
        f"red line is {LATENCY_BUDGET_SECONDS}s")
