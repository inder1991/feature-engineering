"""Contract tests for the OpenMetadata translator + client (spec mapping table, row by row).

Everything runs on recorded fixture pages behind the FetchPage seam — no network. The real httpx
transport's error mapping is proven with httpx.MockTransport (still no network).
"""
from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from tests.featuregen._helpers import make_actor
from tests.featuregen.connectors._fixtures import (
    CARDS_CONFIG,
    fixture_fetch,
    fixture_pages,
)

from featuregen.connectors.openmetadata import (
    OMAuthRejected,
    OMUnreachable,
    build_preview,
    fetch_tables,
    httpx_fetch,
    read_openmetadata,
    semantics_pending_count,
    snapshot_hash,
)
from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows
from featuregen.overlay.upload.ingest import ingest_upload


def _rows():
    tables = fetch_tables(fixture_fetch())
    return read_openmetadata(tables, CARDS_CONFIG)


def _by_column(rows):
    return {(r.table, r.column): r for r in rows}


# ---- Pagination -------------------------------------------------------------------------------


def test_pagination_assembles_all_pages():
    tables = fetch_tables(fixture_fetch())
    assert [t["name"] for t in tables] == ["customers", "accounts", "transactions"]


def test_repeated_cursor_raises_instead_of_looping():
    p1, _ = fixture_pages()          # page 1 keeps pointing at itself

    def fetch(path, params):
        return p1

    with pytest.raises(OMUnreachable, match="repeated a cursor"):
        fetch_tables(fetch)


def test_page_without_data_list_fails_whole_pull():
    with pytest.raises(OMUnreachable, match="no 'data' list"):
        fetch_tables(lambda path, params: {"paging": {}})


# ---- Mapping table ----------------------------------------------------------------------------


def test_source_table_column_type_map():
    rows = _by_column(_rows().rows)
    balance = rows[("accounts", "balance")]
    assert balance.source == "cards"                 # explicit config, not the FQN
    assert balance.type == "decimal"                 # lowercased OM token
    assert balance.definition == "current ledger balance"
    # type variety survives verbatim-lowercased
    assert rows[("transactions", "metadata")].type == "json"
    assert rows[("transactions", "is_disputed")].type == "boolean"
    assert rows[("customers", "email")].type == "varchar"
    assert rows[("accounts", "opened_on")].type == "date"


def test_primary_key_maps_to_grain_from_both_shapes():
    rows = _by_column(_rows().rows)
    assert rows[("customers", "cust_id")].is_grain          # tableConstraints PRIMARY_KEY
    assert rows[("accounts", "account_id")].is_grain        # column-level constraint marker
    assert rows[("transactions", "txn_id")].is_grain
    assert not rows[("customers", "email")].is_grain


def test_foreign_key_maps_to_joins_to_with_unknown_cardinality():
    rows = _by_column(_rows().rows)
    fk = rows[("accounts", "cust_id")]
    assert fk.joins_to == "customers.cust_id"
    assert fk.cardinality == ""                      # unknown stays blank, never invented
    assert rows[("transactions", "account_id")].joins_to == "accounts.account_id"


def test_mapped_tag_becomes_sensitivity():
    rows = _by_column(_rows().rows)
    assert rows[("customers", "email")].sensitivity == "pii"


def test_unmapped_tag_passes_through_literally_and_quarantines():
    translation = _rows()
    rows = _by_column(translation.rows)
    assert rows[("customers", "ssn")].sensitivity == "Confidential.Internal"
    vr = validate_rows(list(translation.rows), "cards")
    quarantined = {(e.row.table, e.row.column): e.message for e in vr.quarantined}
    assert ("customers", "ssn") in quarantined
    assert "unrecognized sensitivity" in quarantined[("customers", "ssn")]
    assert len(vr.quarantined) == 1 and len(vr.good) == 13


def test_ignored_tag_maps_to_blank_sensitivity():
    config = replace(CARDS_CONFIG, tag_map={"PII.Sensitive": "pii", "Confidential.Internal": ""})
    rows = _by_column(read_openmetadata(fetch_tables(fixture_fetch()), config).rows)
    assert rows[("customers", "ssn")].sensitivity == ""


def test_semantics_arrive_blank_and_pending():
    translation = _rows()
    for r in translation.rows:
        assert (r.as_of, r.as_of_basis, r.additivity, r.unit, r.currency, r.entity) == \
            (False, "", "", "", "", "")
    vr = validate_rows(list(translation.rows), "cards")
    assert semantics_pending_count(vr.good) == len(vr.good) == 13


def test_partition_and_time_hints_are_suggestions_never_as_of():
    translation = _rows()
    assert all(not r.as_of for r in translation.rows)
    hints = {(s.table, s.column): s.hint for s in translation.as_of_suggestions}
    assert hints[("accounts", "opened_on")] == "partition column (TIME-UNIT)"
    assert "time axis" in hints[("transactions", "posted_at")]
    assert "time axis" in hints[("customers", "created_at")]


def test_tag_counts_cover_every_tag_seen():
    assert _rows().tag_counts == {"PII.Sensitive": 1, "Confidential.Internal": 1}


def test_schema_table_naming_folds_tables_and_join_targets():
    config = replace(CARDS_CONFIG, table_naming="schema_table")
    translation = read_openmetadata(fetch_tables(fixture_fetch()), config)
    tables = {r.table for r in translation.rows}
    assert tables == {"public_customers", "public_accounts", "public_transactions"}
    fk = _by_column(translation.rows)[("public_accounts", "cust_id")]
    assert fk.joins_to == "public_customers.cust_id"


def test_scope_filters_exclude_and_include():
    tables = fetch_tables(fixture_fetch())
    none = read_openmetadata(tables, replace(CARDS_CONFIG, filters={"schema": "private"}))
    assert none.rows == []
    some = read_openmetadata(tables, replace(CARDS_CONFIG, filters={"service": "mysql_*",
                                                                    "database": "cards_db"}))
    assert len(some.rows) == 14


# ---- Snapshot hash ----------------------------------------------------------------------------


def test_snapshot_hash_is_order_insensitive_but_content_sensitive():
    rows = _rows().rows
    assert snapshot_hash(rows) == snapshot_hash(list(reversed(rows)))
    changed = [replace(rows[0], type="text"), *rows[1:]]
    assert snapshot_hash(changed) != snapshot_hash(rows)


# ---- Real transport error mapping (httpx.MockTransport — still no network) --------------------


def _transport_fetch(handler):
    return httpx_fetch("https://om.test", "bot-token", transport=httpx.MockTransport(handler))


def test_httpx_fetch_sends_bearer_and_returns_json():
    def handler(request):
        assert request.headers["authorization"] == "Bearer bot-token"
        assert request.url.path == "/api/v1/tables"
        return httpx.Response(200, json={"data": [], "paging": {}})

    assert _transport_fetch(handler)("/api/v1/tables", {"fields": "x", "limit": 5}) == \
        {"data": [], "paging": {}}


def test_httpx_fetch_maps_auth_rejection():
    def handler(request):
        return httpx.Response(401, json={"message": "invalid token"})

    with pytest.raises(OMAuthRejected):
        _transport_fetch(handler)("/api/v1/tables", {})


def test_httpx_fetch_maps_server_error_and_non_json_to_unreachable():
    with pytest.raises(OMUnreachable):
        _transport_fetch(lambda r: httpx.Response(500))("/api/v1/tables", {})
    with pytest.raises(OMUnreachable):
        _transport_fetch(lambda r: httpx.Response(200, text="<html>"))("/api/v1/tables", {})


def test_httpx_fetch_maps_connect_error_to_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(OMUnreachable):
        _transport_fetch(handler)("/api/v1/tables", {})


def test_httpx_fetch_refuses_to_follow_redirects():
    """A 3xx to an off-allowlist host would bypass the egress allowlist. follow_redirects is OFF,
    so the redirect target is never requested and the 3xx surfaces as a clean failure."""
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/api/v1/tables"})

    with pytest.raises(OMUnreachable, match="redirect"):
        _transport_fetch(handler)("/api/v1/tables", {})
    assert calls == ["https://om.test/api/v1/tables"]   # the Location was never chased


# ---- Preview dry run (DB-backed) --------------------------------------------------------------


def _existing_cards_rows() -> list[CanonicalRow]:
    """A pre-existing cards catalog: customers matches the OM pull exactly; accounts has balance
    as `numeric` (OM says decimal -> a type change); transactions is absent (-> new)."""
    return [
        CanonicalRow(source="cards", table="customers", column="cust_id", type="bigint",
                     is_grain=True, definition="customer primary key"),
        CanonicalRow(source="cards", table="customers", column="email", type="varchar",
                     definition="customer contact email", sensitivity="pii"),
        CanonicalRow(source="cards", table="customers", column="created_at", type="timestamp",
                     definition="row creation time"),
        CanonicalRow(source="cards", table="accounts", column="account_id", type="bigint",
                     is_grain=True, definition="account primary key"),
        CanonicalRow(source="cards", table="accounts", column="cust_id", type="bigint",
                     definition="owning customer", joins_to="customers.cust_id"),
        CanonicalRow(source="cards", table="accounts", column="balance", type="numeric",
                     definition="current ledger balance"),
        CanonicalRow(source="cards", table="accounts", column="opened_on", type="date",
                     definition="account opening date"),
    ]


def test_preview_against_empty_catalog_is_all_new(conn):
    preview = build_preview(conn, CARDS_CONFIG, _rows())
    assert preview["summary"] == {"tables": 3, "columns": 14, "new": 3, "changed": 0,
                                  "unchanged": 0, "removed": 0, "would_quarantine": 1,
                                  "semantics_pending": 13}
    assert preview["brake"] == {"would_hold": False, "reason": None}
    assert len(preview["snapshot_hash"]) == 64
    # preview never writes
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'cards'"
                        ).fetchone()[0] == 0


def test_preview_diffs_against_current_catalog(conn):
    actor = make_actor(subject="user:owner", roles=("data_owner",))
    assert ingest_upload(conn, "cards", _existing_cards_rows(), actor=actor).status == "ingested"

    preview = build_preview(conn, CARDS_CONFIG, _rows())
    assert preview["summary"]["new"] == 1
    assert preview["summary"]["changed"] == 1
    assert preview["summary"]["unchanged"] == 1
    by_table = {t["table"]: t for t in preview["tables"]}
    assert by_table["transactions"]["status"] == "new"
    assert by_table["customers"]["status"] == "unchanged"
    assert by_table["accounts"]["status"] == "changed"
    assert "balance type: numeric -> decimal" in by_table["accounts"]["changes"]
    assert by_table["customers"]["quarantine"] == [
        {"column": "ssn",
         "reason": "unrecognized sensitivity 'Confidential.Internal' "
                   "(expected one of: pii, restricted)"}]


def test_preview_flags_whole_table_removal(conn):
    """A table in the current catalog that the pull no longer includes is surfaced as 'removed' —
    import DELETE-then-rebuilds the source, so the human must see the drop before approving."""
    actor = make_actor(subject="user:owner", roles=("data_owner",))
    existing = [
        *_existing_cards_rows(),
        CanonicalRow(source="cards", table="promotions", column="promo_id", type="bigint",
                     is_grain=True, definition="promotion key"),
        CanonicalRow(source="cards", table="promotions", column="discount", type="numeric",
                     definition="discount amount"),
    ]
    assert ingest_upload(conn, "cards", existing, actor=actor).status == "ingested"

    preview = build_preview(conn, CARDS_CONFIG, _rows())
    assert preview["summary"]["removed"] == 1
    assert preview["summary"]["tables"] == 3          # 'tables' still counts only the pull
    removed = {t["table"]: t for t in preview["tables"]}["promotions"]
    assert removed["status"] == "removed"
    assert removed["columns"] == 2
    assert "drop this table" in removed["changes"][0]


def test_preview_predicts_brake_hold_on_shrunken_pull(conn):
    """The brake verdict is PREDICTED with the same large_change_brake the pipeline runs: a pull
    that would remove most of a source's objects reports would_hold without ingesting anything."""
    actor = make_actor(subject="user:owner", roles=("data_owner",))
    big = [CanonicalRow(source="cards", table=f"legacy_{t}", column=f"col_{c}", type="text")
           for t in range(5) for c in range(5)]
    assert ingest_upload(conn, "cards", big, actor=actor).status == "ingested"

    preview = build_preview(conn, CARDS_CONFIG, _rows())
    assert preview["brake"]["would_hold"] is True
    assert preview["brake"]["reason"]
    # still a dry run: the prior snapshot is untouched
    assert conn.execute("SELECT count(*) FROM overlay_catalog_object "
                        "WHERE catalog_source = 'cards'").fetchone()[0] == 30


def test_preview_of_empty_pull_raises_value_error(conn):
    with pytest.raises(ValueError, match="nothing to import"):
        build_preview(conn, CARDS_CONFIG,
                      read_openmetadata([], CARDS_CONFIG))
