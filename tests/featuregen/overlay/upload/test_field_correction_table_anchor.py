"""Task 0.6 Seam 1 — a field decision on a TABLE anchor must actually land (the silent no-op bug).

``logical_ref_of`` used to parse a 2-part TABLE graph ref (`public.orders`) as
(table='public', column='orders'), so a four-eyes correction issued against a table asset computed
its CAS on a PHANTOM ref, appended its evidence there, and ``resolve_and_project`` UPDATEd zero
``graph_node`` rows — the command reported success while the catalog showed nothing. These tests
drive the REAL field-correction path against a table anchor and assert the evidence keys and the
display projection both land on the true table identity.
"""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))

_SRC = "bank"
_TABLE_OBJECT_REF = "public.orders"                       # the graph's TABLE node key
_TABLE_REF = normalize_ref(_SRC, None, "orders")          # the TRUE table logical ref
_PHANTOM_REF = normalize_ref(_SRC, "public", "public", "orders")   # what the bug produced


def _seed_graph(db):
    build_graph(db, _SRC, [CanonicalRow(_SRC, "orders", "amount", "numeric")])


def _correct(db, field, action, actor, idem, **kw):
    cas = read_field_cas(db, source=_SRC, object_ref=_TABLE_OBJECT_REF, field=field)
    res = apply_field_correction(
        db, source=_SRC, object_ref=_TABLE_OBJECT_REF, field=field, action=action,
        actor=actor, idempotency_key=idem,
        expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **kw)
    assert res["accepted"] is True, res
    return res


def test_table_field_correction_lands_evidence_at_the_real_table_ref(db):
    _seed_graph(db)
    _correct(db, "domain", "propose_override", ADMIN_A, "d1-p", replacement_value="payments")
    _correct(db, "domain", "confirm_override", ADMIN_B, "d1-c", replacement_value="payments")

    refs = {r[0] for r in db.execute(
        "SELECT DISTINCT logical_ref FROM field_evidence WHERE field_name = 'domain'").fetchall()}
    assert refs == {_TABLE_REF}
    assert _PHANTOM_REF not in refs


def test_table_field_correction_projects_onto_the_graph_node_table_row(db):
    """The silent no-op: with the phantom ref the confirm's re-projection matched ZERO graph rows.
    Post-fix the confirmed value + its decision link must be visible on the table node itself."""
    _seed_graph(db)
    _correct(db, "domain", "propose_override", ADMIN_A, "d2-p", replacement_value="payments")
    _correct(db, "domain", "confirm_override", ADMIN_B, "d2-c", replacement_value="payments")

    row = db.execute(
        "SELECT domain, domain_decision_id FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
        (_SRC, _TABLE_OBJECT_REF)).fetchone()
    assert row is not None
    domain, decision_id = row
    assert domain == "payments"
    assert decision_id is not None
