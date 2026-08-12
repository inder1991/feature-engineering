"""Task 0.6 review FIX 1 — TABLE anchors get DERIVED read scope on asset detail + field corrections.

``build_graph`` never writes sensitivity on table nodes (``visible_requires = {}``), so after the
seam-1 table-anchor repair a fully-restricted table's node was world-visible to BOTH anchor loads:

* ``field_correction.apply_field_correction``'s anchor gate admitted ANY caller for a table anchor
  — a WORKING blind write (propose + confirm land evidence and project onto the hidden table);
* ``asset_detail`` returned the table's identity — an existence oracle over a catalog whose every
  column is hidden from the caller.

Search already derives table visibility from the columns (D11, seam 2). These tests pin the SAME
rule on the other two surfaces: a table anchor is visible iff the caller can see at least one of
its COLUMNS; column anchors are byte-identical to before.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.upload.asset_detail import build_asset_detail
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import (
    FieldCorrectionError,
    apply_field_correction,
    read_field_cas,
)
from featuregen.overlay.upload.graph import build_graph

# Platform-admin WITHOUT any sensitivity class (the guarantee at the anchor gate: admin role and
# sensitivity visibility are two separate axes — admin alone unlocks NO hidden node).
ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))
# The SAME two subjects holding restricted_reader — the privileged pair.
PRIV_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin", "restricted_reader"))
PRIV_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin", "restricted_reader"))

_SRC = "hr"
_TABLE_OBJ = "public.salaries"


def _seed_fully_restricted(db):
    """Table 'salaries' whose EVERY column is sensitivity-restricted."""
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "salaries", "emp_ref", "text", sensitivity="restricted"),
        CanonicalRow(_SRC, "salaries", "amount", "numeric", sensitivity="restricted"),
    ])


def _seed_mixed(db):
    """Table 'salaries' with one world-visible and one restricted column."""
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "salaries", "dept", "text"),
        CanonicalRow(_SRC, "salaries", "amount", "numeric", sensitivity="restricted"),
    ])


def _correct(db, object_ref, field, action, actor, idem, **kw):
    cas = read_field_cas(db, source=_SRC, object_ref=object_ref, field=field)
    return apply_field_correction(
        db, source=_SRC, object_ref=object_ref, field=field, action=action,
        actor=actor, idempotency_key=idem,
        expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **kw)


# ── the blind write: a fully-restricted table must refuse the whole correction flow ───────────────


def test_fully_restricted_table_correction_404s_for_admin_without_the_class(db):
    """The anchor gate must 404 a caller who can see NONE of the table's columns BEFORE any write —
    same disposition as the hidden-column gate, no existence oracle, no blind write path."""
    _seed_fully_restricted(db)
    with pytest.raises(FieldCorrectionError) as exc:
        _correct(db, _TABLE_OBJ, "domain", "propose_override", ADMIN_A, "p1",
                 replacement_value="payroll")
    assert exc.value.status_code == 404
    # Nothing landed: no human evidence row on any ref of this source.
    n = db.execute(
        "SELECT count(*) FROM field_evidence WHERE producer = 'human'").fetchone()[0]
    assert n == 0
    # And the display column never moved.
    row = db.execute(
        "SELECT domain FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (_SRC, _TABLE_OBJ)).fetchone()
    assert row is not None and row[0] is None


def test_fully_restricted_table_asset_detail_404s_for_unprivileged_caller(db):
    _seed_fully_restricted(db)
    # No sensitivity class (platform-admin included): hidden, indistinguishable from missing.
    assert build_asset_detail(db, source=_SRC, object_ref=_TABLE_OBJ,
                              roles=("platform-admin",), include=["identity"]) is None
    assert build_asset_detail(db, source=_SRC, object_ref=_TABLE_OBJ,
                              roles=(), include=["identity"]) is None


# ── privileged + mixed visibility: the derived rule never over-hides ──────────────────────────────


def test_privileged_caller_sees_and_corrects_the_restricted_table(db):
    _seed_fully_restricted(db)
    body = build_asset_detail(db, source=_SRC, object_ref=_TABLE_OBJ,
                              roles=("restricted_reader",), include=["identity"])
    assert body is not None and body["kind"] == "table"

    res = _correct(db, _TABLE_OBJ, "domain", "propose_override", PRIV_A, "p1",
                   replacement_value="payroll")
    assert res["accepted"] is True, res
    res2 = _correct(db, _TABLE_OBJ, "domain", "confirm_override", PRIV_B, "c1",
                    replacement_value="payroll")
    assert res2["accepted"] is True, res2
    row = db.execute(
        "SELECT domain FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (_SRC, _TABLE_OBJ)).fetchone()
    assert row[0] == "payroll"


def test_mixed_visibility_table_stays_visible_and_correctable(db):
    """One visible column keeps the table anchor visible — the derived rule matches search/catalogs."""
    _seed_mixed(db)
    body = build_asset_detail(db, source=_SRC, object_ref=_TABLE_OBJ,
                              roles=(), include=["identity"])
    assert body is not None and body["kind"] == "table"

    res = _correct(db, _TABLE_OBJ, "domain", "propose_override", ADMIN_A, "p1",
                   replacement_value="payroll")
    assert res["accepted"] is True, res


# ── column anchors: byte-identical behavior on both sides of the scope ────────────────────────────


def test_hidden_column_anchor_still_404s_and_visible_column_still_works(db):
    _seed_mixed(db)
    # The restricted COLUMN stays hidden from the unprivileged caller on both surfaces.
    assert build_asset_detail(db, source=_SRC, object_ref="public.salaries.amount",
                              roles=(), include=["identity"]) is None
    with pytest.raises(FieldCorrectionError) as exc:
        _correct(db, "public.salaries.amount", "definition", "propose_override", ADMIN_A, "p1",
                 replacement_value="x")
    assert exc.value.status_code == 404

    # The world-visible column keeps working for the same caller.
    assert build_asset_detail(db, source=_SRC, object_ref="public.salaries.dept",
                              roles=(), include=["identity"]) is not None
    res = _correct(db, "public.salaries.dept", "definition", "propose_override", ADMIN_A, "p2",
                   replacement_value="department name")
    assert res["accepted"] is True, res
