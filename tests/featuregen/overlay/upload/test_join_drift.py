"""Governed-join drift detection — a re-upload that RETARGETS or DROPS a `joins_to` humans already
VERIFIED as an approved_join yields ONE advisory `governed_join_divergence` row; a re-upload that
re-affirms the verified target RESOLVES (deletes) it. Three invariants under test:

* **Never mutates the VERIFIED fact/edge** — detection is a read + advisory-table write; the
  approved_join fact stays VERIFIED and its operational graph_edge is untouched (no auto-demote).
* **Flag-off byte-for-byte** — with the governed seam OFF, ingest never calls detection and no
  divergence row exists.
* **Fail-soft / refresh-not-duplicate** — a re-upload UPSERTs the same (source, from, verified_to)
  row (re-opening it even if previously acknowledged), never a duplicate; acknowledge hides it
  from the open list.

Unit tests drive `detect_governed_join_divergences` against directly-seeded VERIFIED edges;
ingest tests drive the REAL flow (flag-on ingest -> dual platform-admin confirm -> re-ingest),
mirroring test_governed_joins + the passc conftest helpers.
"""
# ruff: noqa: F811 — the passc conftest fixtures are IMPORTED by name (this module lives outside
# tests/.../passc/, so its conftest does not apply); pytest resolves them via the test parameters.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen.overlay.upload.passc.conftest import (  # noqa: F401 — pytest fixtures
    _confirm_join,
    human_admin_1,
    human_admin_2,
)

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import governed_join_proposal
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.join_drift import (
    acknowledge_governed_join_divergence,
    detect_governed_join_divergences,
    list_governed_join_divergences,
)

_NOW = datetime(2026, 7, 11, tzinfo=UTC)
_SRC = "deposits"
_FROM = "public.transactions.acct_id"
_TO = "public.accounts.account_id"


# ── Seed helpers ──────────────────────────────────────────────────────────────────────────────────


def _verified_edge(db, from_ref=_FROM, to_ref=_TO, *, source=_SRC, status="VERIFIED",
                   authority="operational"):
    """A fact-linked `joins` graph_edge in the shape `project_confirmed_joins` writes."""
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority,"
        " approved_join_fact_key, approved_join_status)"
        " VALUES (%s, 'joins', %s, %s, 'N:1', %s, 'fk-test', %s)",
        (source, from_ref, to_ref, authority, status))


def _row(table="transactions", column="acct_id", joins_to="", **kw) -> CanonicalRow:
    return CanonicalRow(_SRC, table, column, "integer", joins_to=joins_to, **kw)


def _divergence_rows(db, source=_SRC):
    return db.execute(
        "SELECT from_ref, verified_to_ref, declared_to_ref, kind, acknowledged_at"
        " FROM governed_join_divergence WHERE catalog_source = %s ORDER BY id", (source,)).fetchall()


# ── Detection unit tests (directly-seeded VERIFIED edges) ────────────────────────────────────────


def test_dropped_join_yields_one_dropped_divergence(db):
    _verified_edge(db)
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)   # no joins_to declared
    rows = _divergence_rows(db)
    assert rows == [(_FROM, _TO, None, "dropped", None)]


def test_retargeted_join_records_the_declared_target(db):
    _verified_edge(db)
    detect_governed_join_divergences(
        db, _SRC, [_row(joins_to="parties.party_id")], now=_NOW)
    rows = _divergence_rows(db)
    assert rows == [(_FROM, _TO, "public.parties.party_id", "retargeted", None)]


def test_reaffirmed_join_yields_no_divergence(db):
    _verified_edge(db)
    detect_governed_join_divergences(db, _SRC, [_row(joins_to="accounts.account_id")], now=_NOW)
    assert _divergence_rows(db) == []


def test_reaffirmed_join_clears_a_prior_divergence(db):
    _verified_edge(db)
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)               # dropped
    assert len(_divergence_rows(db)) == 1
    detect_governed_join_divergences(db, _SRC, [_row(joins_to="accounts.account_id")], now=_NOW)
    assert _divergence_rows(db) == []                                            # resolved


def test_reverse_orientation_declaration_counts_as_reaffirmed(db):
    # The VERIFIED edge is in the CONFIRMED direction, which may be the reverse of the file's
    # declaration (Pass C can confirm either orientation). The pair is intact -> no divergence.
    _verified_edge(db)
    detect_governed_join_divergences(
        db, _SRC, [_row(table="accounts", column="account_id",
                        joins_to="transactions.acct_id")], now=_NOW)
    assert _divergence_rows(db) == []


def test_malformed_joins_to_is_skipped_and_reads_as_dropped(db):
    # An unparseable joins_to cannot re-affirm the verified join (the propose seam skips it loud
    # too) — the verified join is honestly no longer declared.
    _verified_edge(db)
    detect_governed_join_divergences(db, _SRC, [_row(joins_to="garbage")], now=_NOW)
    rows = _divergence_rows(db)
    assert [r[3] for r in rows] == ["dropped"]


def test_reupload_refreshes_the_same_row_and_reopens_an_acknowledged_one(db):
    _verified_edge(db)
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)
    (open_row,) = list_governed_join_divergences(db, _SRC)
    acked = acknowledge_governed_join_divergence(
        db, open_row["id"], subject="user:admin", now=_NOW)
    assert acked is not None and acked["acknowledged_by"] == "user:admin"
    assert list_governed_join_divergences(db, _SRC) == []          # acknowledged -> not open
    # A fresh detection (retargeted this time) REFRESHES the same row and RE-OPENS it.
    detect_governed_join_divergences(
        db, _SRC, [_row(joins_to="parties.party_id")], now=_NOW + timedelta(days=1))
    rows = _divergence_rows(db)
    assert rows == [(_FROM, _TO, "public.parties.party_id", "retargeted", None)]
    assert len(list_governed_join_divergences(db, _SRC)) == 1


def test_detection_never_touches_the_verified_edge(db):
    _verified_edge(db)
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)   # a 'dropped' divergence
    edge = db.execute(
        "SELECT authority, approved_join_status, approved_join_fact_key FROM graph_edge"
        " WHERE catalog_source = %s AND kind = 'joins' AND from_ref = %s", (_SRC, _FROM)).fetchone()
    assert edge == ("operational", "VERIFIED", "fk-test")            # untouched — no auto-demote


def test_non_verified_and_display_only_edges_are_ignored(db):
    _verified_edge(db, status="REJECTED")                            # linked but not VERIFIED
    _verified_edge(db, from_ref="public.transactions.card_id", to_ref="public.cards.card_id",
                   authority="display_only")                         # not operational
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)
    assert _divergence_rows(db) == []


def test_acknowledge_unknown_id_returns_none(db):
    assert acknowledge_governed_join_divergence(db, 999_999_999, subject="u", now=_NOW) is None


def test_list_scopes_to_the_source(db):
    _verified_edge(db)
    _verified_edge(db, source="cards")
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)
    detect_governed_join_divergences(db, "cards", [], now=_NOW)
    assert {d["from_ref"] for d in list_governed_join_divergences(db, _SRC)} == {_FROM}
    assert len(list_governed_join_divergences(db, "cards")) == 1


# ── Ingest wiring (the REAL flow: flag-on ingest -> dual confirm -> re-ingest) ───────────────────


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _join_rows(joins_to="accounts.account_id") -> list[CanonicalRow]:
    """First-upload shape (mirrors test_governed_joins): a declared N:1 join + its target grain.
    Re-upload variants keep every column (and the cardinality hint) so catalog drift never stales
    the join fact — ONLY the joins_to declaration varies."""
    rows = [CanonicalRow(_SRC, "transactions", "acct_id", "integer",
                         joins_to=joins_to, cardinality="N:1"),
            CanonicalRow(_SRC, "accounts", "account_id", "integer", is_grain=True)]
    if joins_to == "parties.party_id":
        rows.append(CanonicalRow(_SRC, "parties", "party_id", "integer", is_grain=True))
    return rows


def _verified_join_via_real_flow(db, monkeypatch, admin1, admin2):
    """Flag-on ingest of the declared join, then the dual platform-admin confirm -> VERIFIED."""
    monkeypatch.setenv("OVERLAY_GOVERNED_JOINS", "1")
    _seal_config()
    res = ingest_upload(db, _SRC, _join_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    ref = governed_join_proposal(_join_rows()[0])
    _confirm_join(db, ref, admin1=admin1, admin2=admin2)
    return ref, fact_key(ref, "approved_join")


def test_reingest_without_the_join_surfaces_dropped_and_never_demotes(
        db, monkeypatch, human_admin_1, human_admin_2):
    _ref, key = _verified_join_via_real_flow(db, monkeypatch, human_admin_1, human_admin_2)
    res = ingest_upload(db, _SRC, _join_rows(joins_to=""), actor=_actor(), now=_NOW)
    assert res.status == "ingested"                                  # advisory: never aborts
    (div,) = list_governed_join_divergences(db, _SRC)
    assert div["kind"] == "dropped" and div["from_ref"] == _FROM
    assert div["verified_to_ref"] == _TO and div["declared_to_ref"] is None
    # THE invariant: the VERIFIED fact and its operational edge are UNCHANGED (no auto-demote).
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    edge = db.execute(
        "SELECT authority, approved_join_status FROM graph_edge WHERE catalog_source = %s"
        " AND kind = 'joins' AND from_ref = %s AND to_ref = %s", (_SRC, _FROM, _TO)).fetchone()
    assert edge == ("operational", "VERIFIED")


def test_reingest_retargeting_the_join_surfaces_retargeted(
        db, monkeypatch, human_admin_1, human_admin_2):
    _ref, key = _verified_join_via_real_flow(db, monkeypatch, human_admin_1, human_admin_2)
    res = ingest_upload(db, _SRC, _join_rows(joins_to="parties.party_id"),
                        actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    (div,) = list_governed_join_divergences(db, _SRC)
    assert div["kind"] == "retargeted"
    assert div["declared_to_ref"] == "public.parties.party_id"
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"


def test_reingest_reaffirming_the_join_clears_the_divergence(
        db, monkeypatch, human_admin_1, human_admin_2):
    _verified_join_via_real_flow(db, monkeypatch, human_admin_1, human_admin_2)
    ingest_upload(db, _SRC, _join_rows(joins_to=""), actor=_actor(), now=_NOW)
    assert len(list_governed_join_divergences(db, _SRC)) == 1
    ingest_upload(db, _SRC, _join_rows(), actor=_actor(), now=_NOW)  # re-declares the join
    assert list_governed_join_divergences(db, _SRC) == []


def test_flag_off_never_calls_detection_and_writes_no_rows(db, monkeypatch):
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)
    monkeypatch.delenv("OVERLAY_PASS_C", raising=False)
    _seal_config()
    calls: list = []
    import featuregen.overlay.upload.ingest as ingest_module
    monkeypatch.setattr(ingest_module, "detect_governed_join_divergences",
                        lambda *a, **kw: calls.append(a))
    res = ingest_upload(db, _SRC, _join_rows(), actor=_actor(), now=_NOW)
    assert res.status == "ingested"
    assert calls == []                                               # detection never invoked
    assert _divergence_rows(db) == []
