from __future__ import annotations

from types import SimpleNamespace

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.catalog_realizations import derive_catalog_realizations
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import ReplayFreshness
from featuregen.overlay.upload.planner.fingerprint import _VERSIONS, compiler_input_fingerprint
from featuregen.overlay.upload.planner.replay import (
    CurrentEvidenceV1,
    StoredEvidenceV1,
    compare,
    read_current_evidence,
    replay_freshness,
)
from featuregen.overlay.upload.templates import _load_columns

_V = _VERSIONS


def _stored(fp="fp1", head=5) -> StoredEvidenceV1:
    return StoredEvidenceV1(fingerprints={"core": fp}, head_seqs={"core": head}, versions=_V)


def _current(fp="fp1", head=5, checkpoint=10, versions=_V) -> CurrentEvidenceV1:
    return CurrentEvidenceV1(fingerprints={"core": fp}, head_seqs={"core": head},
                             checkpoint=checkpoint, versions=versions)


# ── pure comparator ──
def test_match_is_current():
    assert compare(_stored(), _current()) is ReplayFreshness.current


def test_fingerprint_change_is_drifted():
    assert compare(_stored(fp="a"), _current(fp="b")) is ReplayFreshness.drifted


def test_head_seq_change_is_drifted():
    assert compare(_stored(head=5), _current(head=6)) is ReplayFreshness.drifted


def test_version_mismatch_is_incompatible_not_drifted():
    other = (*_V, ("extra", "9.9.9"))
    assert compare(_stored(), _current(versions=other)) is ReplayFreshness.incompatible


def test_projection_lag_is_unverifiable():
    # checkpoint < head_seq -> the projection hasn't caught up -> can't verify (NOT drift, NOT current)
    assert compare(_stored(head=8), _current(head=8, checkpoint=3)) is ReplayFreshness.unverifiable


def test_unrelated_checkpoint_advance_is_not_drift():
    # checkpoint advanced far past head (unrelated events) but fp+head match -> still current
    assert compare(_stored(head=5), _current(head=5, checkpoint=9999)) is ReplayFreshness.current


def test_unreadable_current_is_unverifiable():
    cur = CurrentEvidenceV1(fingerprints={"core": None}, head_seqs={"core": None}, checkpoint=10, versions=_V)
    assert compare(_stored(), cur) is ReplayFreshness.unverifiable


def test_empty_stamp_is_unverifiable():
    empty = StoredEvidenceV1(fingerprints={}, head_seqs={}, versions=_V)
    assert compare(empty, _current()) is ReplayFreshness.unverifiable


def test_incompatible_and_unverifiable_are_never_current():
    assert compare(_stored(), _current(versions=(("x", "1"),))) is not ReplayFreshness.current
    assert compare(_stored(fp=""), _current()) is not ReplayFreshness.current


# ── integration: a real compiler_input_fingerprint drifts on a graph rebuild (head_seq unchanged) ──
def _seed(db):
    rows = [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
    ]
    build_graph(db, "core", [r for r, _ in rows], concepts={content_hash(r): cn for r, cn in rows})


def _fp(db) -> str:
    mini = SimpleNamespace(
        columns_by_catalog={"core": {c.object_ref: c for c in _load_columns(db, "core", ())}},
        realizations_by_catalog={"core": derive_catalog_realizations(db, "core").realizations})
    return compiler_input_fingerprint(mini, "core")


def test_graph_rebuild_drifts_the_fingerprint_at_fixed_head(db):
    _seed(db)
    stored_fp = _fp(db)
    # fixed head + caught-up checkpoint -> a plain re-read is current
    stored = StoredEvidenceV1(fingerprints={"core": stored_fp}, head_seqs={"core": 3}, versions=_V)
    assert compare(stored, _current(fp=_fp(db), head=3, checkpoint=100)) is ReplayFreshness.current
    # rebuild the graph with a DIFFERENT column additivity (a classifier input) — head_seq is untouched
    rows = [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_flow"),   # was monetary_stock
    ]
    build_graph(db, "core", [r for r, _ in rows], concepts={content_hash(r): cn for r, cn in rows})
    assert _fp(db) != stored_fp
    assert compare(stored, _current(fp=_fp(db), head=3, checkpoint=100)) is ReplayFreshness.drifted


def test_adapter_recomputes_and_missing_catalog_is_unverifiable(db):
    # drives the IMPURE read_current_evidence + replay_freshness end-to-end. A catalog with no data
    # (drift_head_seq -> None) is unverifiable; a re-read of the same fingerprint is NOT a drift.
    _seed(db)
    stored = StoredEvidenceV1(fingerprints={"core": _fp(db), "ghost": "x"},
                             head_seqs={"core": 3, "ghost": 3}, versions=_V)
    cur = read_current_evidence(db, stored)
    assert cur.fingerprints["core"] == _fp(db)          # recomputed the real per-catalog fingerprint
    assert cur.head_seqs["ghost"] is None               # a catalog with no watermark -> None
    assert replay_freshness(db, stored) is ReplayFreshness.unverifiable   # ghost head None -> unverifiable
