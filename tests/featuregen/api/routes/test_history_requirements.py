"""C9 — the optional history panel: computed listing, one-click declaration, zero obligation."""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

SOURCE = "histbank"
PATH = "/governance/history-requirements"


def _confirmer(user: str = "sme-lead") -> dict:
    return {"X-User": user, "X-Roles": "platform-admin,platform_admin"}


def _reader() -> dict:
    return {"X-User": "viewer", "X-Roles": "catalog_viewer"}


def _seed(conn) -> None:
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.object_ref import normalize_ref

    rows = [
        (CanonicalRow(SOURCE, "transactions", "acct_ref", "integer", is_grain=True,
                      entity="Account"), "account_id"),
        (CanonicalRow(SOURCE, "transactions", "booked_ts", "timestamp"), "event_timestamp"),
    ]
    build_graph(conn, SOURCE, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    # The table's shape fact: DECLARED event (the panel lists event tables only).
    logical = normalize_ref(SOURCE, "public", "transactions", None)
    record_field_evidence(
        conn, logical_ref=logical, field_name="event_or_snapshot", proposed_value="event",
        producer="source", strength="attested", producer_ref="src:manifest",
        source_snapshot_id="snap-test",
        input_hash=field_input_hash(logical_ref=logical, field_name="event_or_snapshot",
                                    material="event"))
    conn.execute(
        "UPDATE graph_node SET event_or_snapshot = 'event' "
        "WHERE catalog_source = %s AND kind = 'table' AND table_name = 'transactions'",
        (SOURCE,))
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, "
        "last_run_id, head_seq) VALUES (%s, %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (SOURCE, now, now))


def test_the_listing_computes_the_need_beside_the_declared_truth(client, conn):
    """Business phrasing, computed from the registry's window axes: the panel says how far
    features could look back; "not stated" is a fact, never a to-do."""
    _seed(conn)
    res = client.get(f"{PATH}?source={SOURCE}", headers=_reader())
    assert res.status_code == 200, res.text
    body = res.json()
    entry = next(t for t in body["tables"] if t["table"] == "transactions")
    assert entry["features_look_back_days"] >= 180      # the registry's deepest window axis
    assert entry["declared_depth_days"] is None         # not stated — and that is fine
    assert entry["sufficient"] is None                  # unknown, honestly


def test_one_optional_click_declares_and_the_panel_reflects_it(client, conn):
    _seed(conn)
    res = client.post(PATH, headers=_confirmer(), json={
        "source": SOURCE, "table": "transactions", "depth_days": 400})
    assert res.status_code == 200, res.text

    body = client.get(f"{PATH}?source={SOURCE}", headers=_reader()).json()
    entry = next(t for t in body["tables"] if t["table"] == "transactions")
    assert entry["declared_depth_days"] == 400
    assert entry["declared_by"] == "human/confirmed"
    assert entry["sufficient"] is True


def test_the_declaration_needs_the_confirmer_claim_and_a_real_table(client, conn):
    _seed(conn)
    res = client.post(PATH, headers=_reader(), json={
        "source": SOURCE, "table": "transactions", "depth_days": 400})
    assert res.status_code == 403
    res = client.post(PATH, headers=_confirmer(), json={
        "source": SOURCE, "table": "not_a_table", "depth_days": 400})
    assert res.status_code == 404
