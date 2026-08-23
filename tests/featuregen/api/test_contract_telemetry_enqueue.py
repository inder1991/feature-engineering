"""S1B-3 at the ROUTE: the telemetry flag is read here, and the served response does not move.

The builder-level suite (``test_gate1_telemetry_enqueue.py``) pins byte identity and the query
delta. This one pins the two things only the route can answer: that
``FEATUREGEN_INTENT_SHADOW_TELEMETRY`` is read HERE (beside the shadow planner's, not inside the
builder), and that the eligible set frozen into the work item is the V2 universe the DISPOSITIONS
report on — not the legacy template registry, which would freeze a question this run never asked.
"""
from __future__ import annotations

import json
import re

from tests.featuregen.api._helpers import AUTH
from tests.featuregen.overlay.upload.contract.test_gate1_telemetry_enqueue import _canonical_ids

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

CHURN = "customer.relationship_attrition.churn"
FLAG = "FEATUREGEN_INTENT_SHADOW_TELEMETRY"


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits"}),
    })


def _bank(conn) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
    ]
    build_graph(conn, "bank", [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (now, now))


def _post(client) -> dict:
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops over 30 days",
        "objective": "predict churn", "catalog_source": "bank",
        # the confirmed prediction grain — without it the governed telemetry has no grain to
        # plan toward, and the builder correctly queues nothing
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact",
                            "target_entity": "customer"},
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


#: A live route response also carries WALL CLOCK: every disposition stage stamps its own
#: ``evaluated_at`` from the route's ``now``. Canonicalized by order of first appearance for the
#: same reason as the minted ids — two requests are two moments, and that is not a difference the
#: telemetry flag caused. ``test_two_identical_requests_differ_only_in_ids_and_clock`` is the
#: control that proves this is all the canonicalization removes.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?")


def _canonical(body: dict) -> str:
    payload = _canonical_ids(json.dumps(body, sort_keys=True, default=str))
    seen: dict[str, str] = {}

    def _replace(match: re.Match) -> str:
        stamp = match.group(0)
        if stamp not in seen:
            seen[stamp] = f"<clock #{len(seen)}>"
        return seen[stamp]

    return _TIMESTAMP.sub(_replace, payload)


def _outbox(conn) -> list[tuple]:
    return conn.execute(
        "SELECT generation_run_id, intent_id, frozen_inputs FROM governed_telemetry_outbox "
        "ORDER BY work_item_id").fetchall()


def test_the_route_queues_nothing_with_the_flag_unset(make_client, conn, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    _bank(conn)
    body = _post(make_client(llm_client=_fake()))
    assert body["generation_run_id"]
    assert _outbox(conn) == []


def test_the_route_queues_one_item_carrying_the_runs_own_lineage(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    _bank(conn)
    body = _post(make_client(llm_client=_fake()))

    rows = _outbox(conn)
    assert len(rows) == 1
    run_id, intent_id, frozen = rows[0]
    assert run_id == body["generation_run_id"]
    assert intent_id == body["intent_id"]
    assert frozen["frozen_inputs_version"] == "1"
    assert frozen["anchor_catalog_source"] == "bank"
    assert frozen["catalog_scope_material"]["authorized_catalog_sources"] == ["bank"]
    # The frozen recipe references come from the ELIGIBLE set, not the whole dispositioned
    # universe: `in_scope_count` is what the route narrowed to, and the manifest matches it. This
    # is the assertion that would fail if the route ever threaded `applicability.eligible_ids`
    # (the LEGACY template registry) instead of the V2 disposition universe.
    dispositioned = {d["recipe_id"] for d in body["dispositions"]}
    frozen_recipes = {ref["canonical_definition_id"] for ref in frozen["requests"]
                      if ref["origin"] == "recipe_v2"}
    assert frozen_recipes and frozen_recipes <= dispositioned
    assert len(frozen_recipes) == body["in_scope_count"] < len(dispositioned)


def test_the_served_response_does_not_move_when_telemetry_is_on(make_client, conn, monkeypatch):
    """Two live requests through the real route, canonicalized for the ids every request mints
    afresh (see the builder suite's comment for what those are and why). Anything the telemetry
    path changed about the ANSWER would survive that canonicalization and fail here."""
    _bank(conn)
    monkeypatch.delenv(FLAG, raising=False)
    off = _post(make_client(llm_client=_fake()))
    monkeypatch.setenv(FLAG, "1")
    on = _post(make_client(llm_client=_fake()))

    assert _canonical(on) == _canonical(off)
    assert len(_outbox(conn)) == 1


def test_two_identical_requests_differ_only_in_ids_and_clock(make_client, conn, monkeypatch):
    """The control for the pin above — it is comparing a real, fully-populated response."""
    monkeypatch.delenv(FLAG, raising=False)
    _bank(conn)
    first = _post(make_client(llm_client=_fake()))
    second = _post(make_client(llm_client=_fake()))
    assert first != second                                     # ids and clocks move
    assert _canonical(first) == _canonical(second)
