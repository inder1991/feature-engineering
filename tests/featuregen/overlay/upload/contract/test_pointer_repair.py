"""Delivery H2d — deterministic pointer repair + one-time legacy backfill.

``repair_feature_pointer`` rebuilds a feature's ``feature_current_contract`` pointer from the highest
VALID confirmed contract version (the max version not SUPERSEDED-away) + refreshes the feature/derives
compat projection from that version's IMMUTABLE input lineage — deterministic + idempotent, under the
H2b per-feature advisory lock. ``backfill_feature_pointers`` installs a pointer for a legacy feature that
HAS a contract but NO pointer (reading ``legacy_unassessed``, fabricating no lineage rows); a
contract-less directly-registered feature keeps NO pointer.

Backfill is a GLOBAL sweep, so its assertions are made robust to committed rows leaked by other suites in
the shared session cluster by asserting the FEATURE-SCOPED outcome + the drain-invariant "after a full
backfill, the next backfill is a 0 no-op" rather than a global count.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    confirm_contract,
    contract_read_status,
    feature_contract_lock_key,
)
from featuregen.overlay.upload.contract.pointer_repair import (
    backfill_feature_pointers,
    repair_feature_pointer,
)
from featuregen.overlay.upload.features import FeatureSpec, register_feature
from featuregen.overlay.upload.graph import build_graph


def _insert_legacy_contract(conn, feature_id, feature_name, *, version=1, contract_id=None):
    """A PRE-H2b legacy contract: a raw ``contract`` row with NO ``contract_input_column`` lineage and NO
    pointer (that table + the pointer postdate the legacy confirm). Used to exercise the repair/backfill
    legacy paths without going through ``confirm_contract`` (which would write input rows + a pointer)."""
    cid = contract_id or mint_id("contract")
    conn.execute("INSERT INTO contract (contract_id, feature_id, feature_name, definition, version) "
                 "VALUES (%s, %s, %s, %s, %s)", (cid, feature_id, feature_name, "Legacy def.", version))
    return cid


def _bank(conn, source="bank"):
    build_graph(conn, source, [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "accounts", "posted_at", "timestamp", as_of=True)])


def _draft(name="avg_balance_90d", source="bank"):
    return ContractDraft(name, "Average 90-day ledger balance.", "accounts", "avg_90d", "posted_at",
                         ["public.accounts.balance"],
                         derives_pairs=((source, "public.accounts.balance"),))


def _pointer(conn, feature_id):
    return conn.execute(
        "SELECT contract_id, pointer_version FROM feature_current_contract WHERE feature_id = %s",
        (feature_id,)).fetchone()


# ── REPAIR ───────────────────────────────────────────────────────────────────────────────────────────
def test_repair_restores_pointer_to_highest_valid_version_and_refreshes_compat(db):
    _bank(db)
    c1 = confirm_contract(db, _draft(), actor="ds1")
    c2 = confirm_contract(db, _draft(), actor="ds1")   # v2; v1 superseded; pointer -> (c2, 2)
    assert _pointer(db, c2.feature_id) == (c2.contract_id, 2)

    # CORRUPT the pointer: repoint it at the SUPERSEDED v1 (FK-valid — v1 belongs to this feature).
    db.execute("UPDATE feature_current_contract SET contract_id = %s WHERE feature_id = %s",
               (c1.contract_id, c2.feature_id))
    # also scramble the compat projection to prove repair rebuilds it from the input lineage.
    db.execute("DELETE FROM feature_derives_from WHERE feature_id = %s", (c2.feature_id,))

    assert repair_feature_pointer(db, c2.feature_id) is True
    ptr = _pointer(db, c2.feature_id)
    assert ptr[0] == c2.contract_id            # restored to the highest VALID (non-superseded) version
    assert ptr[1] == 3                         # pointer_version advanced monotonically (2 -> 3)

    # compat projection rebuilt from v2's immutable contract_input_column lineage.
    pair = db.execute("SELECT catalog_source, object_ref FROM feature_derives_from "
                      "WHERE feature_id = %s", (c2.feature_id,)).fetchone()
    assert pair == ("bank", "public.accounts.balance")
    feat = db.execute("SELECT grain_table, as_of_column, verification FROM feature WHERE feature_id = %s",
                      (c2.feature_id,)).fetchone()
    assert feat == ("accounts", "posted_at", "DESIGN-CHECKED")


def test_repair_is_idempotent_noop_when_pointer_already_correct(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    before = _pointer(db, c.feature_id)
    # pointer already correct -> repair is a NO-OP: returns False, no pointer_version bump.
    assert repair_feature_pointer(db, c.feature_id) is False
    assert _pointer(db, c.feature_id) == before
    # deterministic + idempotent across repeated runs.
    assert repair_feature_pointer(db, c.feature_id) is False
    assert _pointer(db, c.feature_id) == before


def test_repair_reinstalls_a_cleared_pointer(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    db.execute("DELETE FROM feature_current_contract WHERE feature_id = %s", (c.feature_id,))
    assert _pointer(db, c.feature_id) is None
    assert repair_feature_pointer(db, c.feature_id) is True
    assert _pointer(db, c.feature_id) == (c.contract_id, 1)   # fresh install at version 1


def test_repair_holds_the_feature_advisory_lock(db, _dsn):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    repair_feature_pointer(db, c.feature_id)   # takes the lock unconditionally (held until tx end)
    key = feature_contract_lock_key("avg_balance_90d")
    with psycopg.connect(_dsn, autocommit=True) as probe:
        assert probe.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0] is False
    db.rollback()   # transaction-scoped: releasing the tx frees the lock
    with psycopg.connect(_dsn, autocommit=True) as probe:
        assert probe.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()[0] is True


def test_repair_unknown_feature_raises(db):
    with pytest.raises(KeyError):
        repair_feature_pointer(db, "feat_does_not_exist")


def test_repair_on_legacy_zero_input_contract_preserves_compat_and_sets_pointer(db):
    """I-1dm: a pre-H2b legacy contract has ZERO ``contract_input_column`` rows. Repair must set the
    pointer but must NOT rebuild the compat projection from that empty set — doing so would NULL
    ``grain_table``/``as_of_column`` and DELETE every ``feature_derives_from`` row with nothing to
    re-insert, BLINDING the feature's real lineage. The existing compat rows stay intact (pointer-only,
    mirroring backfill's legacy posture)."""
    # a legacy feature with REAL compat lineage on `feature`/`feature_derives_from`, a contract row, but
    # no input rows and no pointer.
    fid = register_feature(db, FeatureSpec(
        name="legacy_zero_input", description="legacy", grain_table="accounts",
        as_of_column="posted_at", derives_from=[("bank", "public.accounts.balance")],
        verification="DESIGN-CHECKED"))
    cid = _insert_legacy_contract(db, fid, "legacy_zero_input")
    assert _pointer(db, fid) is None
    assert db.execute("SELECT count(*) FROM contract_input_column WHERE contract_id = %s",
                      (cid,)).fetchone()[0] == 0

    compat_before = db.execute("SELECT grain_table, as_of_column FROM feature WHERE feature_id = %s",
                               (fid,)).fetchone()
    derives_before = db.execute("SELECT catalog_source, object_ref FROM feature_derives_from "
                                "WHERE feature_id = %s ORDER BY object_ref", (fid,)).fetchall()
    assert compat_before == ("accounts", "posted_at") and derives_before   # legacy compat present

    assert repair_feature_pointer(db, fid) is True          # pointer installed
    assert _pointer(db, fid) == (cid, 1)                     # points at the legacy contract
    # compat rows UNTOUCHED — NOT blinded by the empty input set.
    assert db.execute("SELECT grain_table, as_of_column FROM feature WHERE feature_id = %s",
                      (fid,)).fetchone() == compat_before
    assert db.execute("SELECT catalog_source, object_ref FROM feature_derives_from WHERE feature_id = %s "
                      "ORDER BY object_ref", (fid,)).fetchall() == derives_before


def test_repair_and_backfill_deterministic_under_a_version_tie(db):
    """I-2dm: per-feature version uniqueness is NOT schema-enforced (0961 keys on ``feature_name``, and
    nothing ties ``contract.feature_name`` to ``feature_id``), so two contracts can tie at one version for
    a feature_id. Repair AND backfill must resolve the tie IDENTICALLY on every run via the total order
    ``ORDER BY version DESC, contract_id DESC``."""
    fid = register_feature(db, FeatureSpec(name="tie_feat", description="tie"))
    # two contracts, SAME feature_id + version, distinct feature_name (dodges 0961's unique) + distinct id.
    _insert_legacy_contract(db, fid, "tie_feat", version=1, contract_id="contract_aaa_lo")
    _insert_legacy_contract(db, fid, "tie_feat_alt", version=1, contract_id="contract_zzz_hi")
    expected = "contract_zzz_hi"                             # contract_id DESC tie-break winner

    assert repair_feature_pointer(db, fid) is True
    assert _pointer(db, fid)[0] == expected                 # repair picks the tie-break winner
    db.execute("DELETE FROM feature_current_contract WHERE feature_id = %s", (fid,))
    backfill_feature_pointers(db)
    assert _pointer(db, fid)[0] == expected                 # backfill picks the SAME winner


def test_backfill_releases_advisory_locks_per_feature_not_held_to_commit(db):
    """M-b: the sweep takes a SESSION advisory lock per feature and RELEASES it immediately, so a large
    backfill never accumulates one advisory lock per feature until the single final commit ('out of shared
    memory'). Two legacy features are created WITHOUT confirm (so no confirm-time xact lock confounds the
    count); after the sweep the connection holds NO NET advisory locks — proof each per-feature lock was
    released as the sweep ran (a per-feature xact lock would still be held here, un-committed)."""
    def held():
        return db.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                          "AND pid = pg_backend_pid()").fetchone()[0]

    for name in ("legacy_lock_a", "legacy_lock_b"):
        fid = register_feature(db, FeatureSpec(name=name, description="legacy"))
        _insert_legacy_contract(db, fid, name)
    before = held()
    n = backfill_feature_pointers(db)
    assert n >= 2                              # both legacy features (+ any leaked pointer-less) got one
    assert held() == before                    # backfill released EVERY per-feature lock it took


# ── BACKFILL ───────────────────────────────────────────────────────────────────────────────────────
def test_backfill_legacy_feature_gets_pointer_at_legacy_unassessed_no_fabrication(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    # Simulate a PRE-H2b legacy feature: a contract exists but the pointer + C4 projection state predate
    # the pointer model. Drop BOTH (a legacy contract has neither); DO NOT touch the immutable contract /
    # input rows.
    db.execute("DELETE FROM feature_current_contract WHERE feature_id = %s", (c.feature_id,))
    db.execute("DELETE FROM feature_contract_validation_state WHERE contract_id = %s", (c.contract_id,))
    reqs_before = db.execute(
        "SELECT count(*) FROM feature_validation_requirement WHERE contract_id = %s",
        (c.contract_id,)).fetchone()[0]

    backfill_feature_pointers(db)   # a GLOBAL sweep — also drains any leaked pointer-less features

    assert _pointer(db, c.feature_id) == (c.contract_id, 1)   # pointer at the latest existing contract
    # reads legacy_unassessed effectively (no projection state row; nothing fabricated).
    assert contract_read_status(db, c.contract_id) == ("legacy_unassessed", "UNVERIFIED")
    # NO state row + NO new requirement rows were fabricated for the legacy contract.
    assert db.execute("SELECT count(*) FROM feature_contract_validation_state WHERE contract_id = %s",
                      (c.contract_id,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM feature_validation_requirement WHERE contract_id = %s",
                      (c.contract_id,)).fetchone()[0] == reqs_before


def test_backfill_is_idempotent_second_full_sweep_is_a_noop(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")   # confirm already installed a pointer
    backfill_feature_pointers(db)                      # drain any pre-existing leaked features
    assert _pointer(db, c.feature_id) == (c.contract_id, 1)   # confirm's pointer is untouched

    # Now clear MY pointer to simulate a legacy feature, and backfill it.
    db.execute("DELETE FROM feature_current_contract WHERE feature_id = %s", (c.feature_id,))
    n1 = backfill_feature_pointers(db)
    assert n1 >= 1                                     # at least my feature was backfilled
    assert _pointer(db, c.feature_id) == (c.contract_id, 1)
    # DRAIN-INVARIANT: after a full backfill nothing is left, so the next sweep is a 0 no-op.
    assert backfill_feature_pointers(db) == 0
    assert _pointer(db, c.feature_id) == (c.contract_id, 1)


def test_backfill_leaves_contractless_feature_without_pointer(db):
    fid = register_feature(db, FeatureSpec(name="direct_feat_h2d", description="registered directly"))
    backfill_feature_pointers(db)
    assert _pointer(db, fid) is None                   # no contract -> stays pointer-less (UNVERIFIED)
