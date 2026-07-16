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
from featuregen.overlay.upload.upload_catalog import table_ref

_NOW = datetime(2026, 7, 11, tzinfo=UTC)
_SRC = "deposits"
_FROM = "public.transactions.acct_id"
_TO = "public.accounts.account_id"


# ── Seed helpers ──────────────────────────────────────────────────────────────────────────────────


def _file_declared(db, from_ref=_FROM, to_ref=_TO, *, source=_SRC):
    """Mark a join as FILE-DECLARED in a prior upload (migration 0991). Stored SORTED, exactly as
    the detector records it — a divergence is only ever raised for a file-declared VERIFIED join."""
    lo, hi = sorted((from_ref, to_ref))
    db.execute(
        "INSERT INTO file_declared_join (catalog_source, from_ref, to_ref, declared_at)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (source, lo, hi, _NOW))


def _verified_edge(db, from_ref=_FROM, to_ref=_TO, *, source=_SRC, status="VERIFIED",
                   authority="operational", file_declared=True):
    """A fact-linked `joins` graph_edge in the shape `project_confirmed_joins` writes. By default
    also records the FILE-DECLARED marker (the common case — a join a file declared and humans then
    verified). `file_declared=False` seeds a PASS-C-DISCOVERED join (no marker): never in any
    file's joins_to, so the detector must never drift-check it."""
    db.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority,"
        " approved_join_fact_key, approved_join_status)"
        " VALUES (%s, 'joins', %s, %s, 'N:1', %s, 'fk-test', %s)",
        (source, from_ref, to_ref, authority, status))
    if file_declared:
        _file_declared(db, from_ref, to_ref, source=source)


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


def test_pass_c_discovered_join_never_diverges_but_file_declared_still_does(db):
    # THE fix (coordinator concern #2): a PASS-C-DISCOVERED VERIFIED join — proposed+confirmed from
    # upload metadata alone, NEVER in any file's joins_to — has no file-declared marker, so a
    # re-upload that (of course) does not declare it must raise NO divergence. A genuinely
    # file-declared-then-dropped join in the SAME source still surfaces its 'dropped' divergence.
    _verified_edge(db, from_ref="public.txn.merchant_id", to_ref="public.merchants.merchant_id",
                   file_declared=False)                              # Pass-C discovered: no marker
    _verified_edge(db)                                               # file-declared: has a marker
    # The re-upload declares NEITHER join.
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)
    rows = _divergence_rows(db)
    assert rows == [(_FROM, _TO, None, "dropped", None)]             # ONLY the file-declared one


def test_a_later_file_declaration_starts_drift_checking_a_pass_c_pair(db):
    # A Pass-C join is silent UNTIL a file declares the same pair — from then on it is drift-checked
    # (the marker is durable). First: Pass-C VERIFIED, no marker, re-upload without it -> silent.
    _verified_edge(db, file_declared=False)
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)
    assert _divergence_rows(db) == []
    # Now a file DECLARES the pair (records the marker) — the join is confirmed already.
    detect_governed_join_divergences(db, _SRC, [_row(joins_to="accounts.account_id")], now=_NOW)
    assert _divergence_rows(db) == []                                # declared == verified -> fine
    # A subsequent upload drops it -> NOW a 'dropped' divergence (it became file-declared).
    detect_governed_join_divergences(db, _SRC, [_row()], now=_NOW)
    assert [r[3] for r in _divergence_rows(db)] == ["dropped"]


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
    """Flag-on ingest of the declared join, then the dual platform-admin confirm -> VERIFIED.

    The ingest calls here use the REAL clock (no `now=`): the end-of-ingest
    `project_confirmed_joins` defaults to `datetime.now(UTC)` and `resolve_fact`'s drift-freshness
    guard (24h SLA) would refuse to serve the fact against a days-old fixed watermark — the same
    reason the full-ingestion e2e runs on the real clock."""
    monkeypatch.setenv("OVERLAY_GOVERNED_JOINS", "1")
    _seal_config()
    res = ingest_upload(db, _SRC, _join_rows(), actor=_actor())
    assert res.status == "ingested"
    ref = governed_join_proposal(_join_rows()[0])
    _confirm_join(db, ref, admin1=admin1, admin2=admin2)
    return ref, fact_key(ref, "approved_join")


def test_reingest_without_the_join_surfaces_dropped_and_never_demotes(
        db, monkeypatch, human_admin_1, human_admin_2):
    _ref, key = _verified_join_via_real_flow(db, monkeypatch, human_admin_1, human_admin_2)
    res = ingest_upload(db, _SRC, _join_rows(joins_to=""), actor=_actor())
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
                        actor=_actor())
    assert res.status == "ingested"
    (div,) = list_governed_join_divergences(db, _SRC)
    assert div["kind"] == "retargeted"
    assert div["declared_to_ref"] == "public.parties.party_id"
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"


def test_reingest_reaffirming_the_join_clears_the_divergence(
        db, monkeypatch, human_admin_1, human_admin_2):
    _verified_join_via_real_flow(db, monkeypatch, human_admin_1, human_admin_2)
    ingest_upload(db, _SRC, _join_rows(joins_to=""), actor=_actor())
    assert len(list_governed_join_divergences(db, _SRC)) == 1
    ingest_upload(db, _SRC, _join_rows(), actor=_actor())  # re-declares the join
    assert list_governed_join_divergences(db, _SRC) == []


def test_flag_off_never_calls_detection_and_writes_no_rows(db, monkeypatch):
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)
    monkeypatch.delenv("OVERLAY_PASS_C", raising=False)
    _seal_config()
    calls: list = []
    import featuregen.overlay.upload.ingest as ingest_module
    monkeypatch.setattr(ingest_module, "detect_governed_join_divergences",
                        lambda *a, **kw: calls.append(a))
    res = ingest_upload(db, _SRC, _join_rows(), actor=_actor())
    assert res.status == "ingested"
    assert calls == []                                               # detection never invoked
    assert _divergence_rows(db) == []


# ── Safety coverage: fail-soft detection + projection-lag no-op ──────────────────────────────────


def test_detection_db_fault_never_aborts_upload_or_rolls_back_pass_a(db, monkeypatch):
    """Fail-soft (the drift block's own savepoint + except in ingest_upload): a GENUINE DB-class
    fault inside `detect_governed_join_divergences` — a failed statement that aborts the
    transaction it runs in at the PG level — must degrade to a warning. The upload still ingests,
    the Pass A facts + graph are NOT rolled back, and the request tx is left healthy for the
    statements that follow (persist_quarantine). Mirrors test_governed_joins_failsoft, which
    proves the same for the propose seam."""
    import featuregen.overlay.upload.ingest as ingest_mod

    monkeypatch.setenv("OVERLAY_GOVERNED_JOINS", "1")
    _seal_config()
    calls: list[str] = []

    def _db_fault(conn, source, rows, **kw):
        calls.append(source)
        conn.execute("SELECT 1/0")   # DB-class fault: aborts the transaction it runs in

    monkeypatch.setattr(ingest_mod, "detect_governed_join_divergences", _db_fault)
    res = ingest_upload(db, _SRC, _join_rows(), actor=_actor())
    assert res.status == "ingested"                    # degraded to a warning, never an error
    assert calls == [_SRC]                             # the detection seam WAS reached + blew up
    # Pass A facts + graph intact — nothing the advisory failure could roll back did roll back.
    grain = load_fact(db, fact_key(table_ref(_SRC, "accounts"), "grain"))
    assert fold_overlay_state(grain).status == "VERIFIED"
    nodes = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = %s", (_SRC,)).fetchone()[0]
    assert nodes > 0
    assert db.execute("SELECT 1").fetchone()[0] == 1   # request tx healthy, not aborted


def test_projection_lag_skip_writes_no_false_dropped_divergence(
        db, monkeypatch, human_admin_1, human_admin_2):
    """Projection-lag no-op: when the lag guard SKIPS the end-of-ingest `project_confirmed_joins`,
    build_graph's wipe leaves graph_edge with ZERO VERIFIED operational joins — detection still
    runs, sees no verified joins, and must write NO false 'dropped' for the (still-VERIFIED) join
    the lagged re-upload didn't re-declare. It re-detects on the next caught-up ingest — the
    contract stated in ingest_upload's drift block."""
    import featuregen.overlay.upload.ingest as ingest_mod

    _ref, key = _verified_join_via_real_flow(db, monkeypatch, human_admin_1, human_admin_2)
    real_detect = ingest_mod.detect_governed_join_divergences
    detections: list[list[dict]] = []

    def _spy(conn, source, rows, **kw):
        out = real_detect(conn, source, rows, **kw)
        detections.append(out)
        return out

    monkeypatch.setattr(ingest_mod, "detect_governed_join_divergences", _spy)
    monkeypatch.setattr(ingest_mod, "projection_lag", lambda conn, name: 1)   # pretend halted
    res = ingest_upload(db, _SRC, _join_rows(joins_to=""), actor=_actor())    # drops the join
    assert res.status == "ingested"
    assert detections == [[]]                          # detection RAN and detected NOTHING
    # The lag guard really fired: the wiped edges were NOT re-projected, so mid-lag the source
    # carries zero VERIFIED joins — there is nothing for the detector to false-drop against.
    edges = db.execute(
        "SELECT count(*) FROM graph_edge WHERE catalog_source = %s AND kind = 'joins'"
        " AND approved_join_status = 'VERIFIED'", (_SRC,)).fetchone()[0]
    assert edges == 0
    assert _divergence_rows(db) == []                  # NO false 'dropped' row was written
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"   # the fact is untouched
