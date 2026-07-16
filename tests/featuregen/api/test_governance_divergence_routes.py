"""Governed-join divergence surface on the governance router: the GET joins list carries an
additive `divergences` field (OPEN rows only) and POST
/governance/joins/divergences/{id}/acknowledge stamps acknowledged_at/by (platform-admin gated,
404 on an unknown id). Rows are seeded directly into `governed_join_divergence` — the detection
path itself is covered in tests/featuregen/overlay/upload/test_join_drift.py.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.catalog import _clear_catalog_adapter

_NOW = datetime(2026, 7, 15, tzinfo=UTC)


def _h(user: str, roles: str = "platform-admin") -> dict:
    return {"X-User": user, "X-Roles": roles}


@pytest.fixture(autouse=True)
def _clean_process_globals():
    """The GET route self-registers the upload-context adapter (a process global); clear it after
    every test so it never leaks into a suite expecting the fail-closed RuntimeError."""
    yield
    _clear_catalog_adapter()


def _seed_divergence(conn, *, source="src", from_ref="public.transactions.acct_id",
                     verified_to="public.accounts.account_id", declared_to=None,
                     kind="dropped") -> int:
    return conn.execute(
        "INSERT INTO governed_join_divergence (catalog_source, from_ref, verified_to_ref,"
        " declared_to_ref, kind, detected_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (source, from_ref, verified_to, declared_to, kind, _NOW)).fetchone()[0]


def test_joins_list_carries_open_divergences_additively(client, conn):
    _seed_divergence(conn)
    _seed_divergence(conn, from_ref="public.transactions.card_id",
                     verified_to="public.cards.card_id",
                     declared_to="public.plastic.card_id", kind="retargeted")
    r = client.get("/sources/src/governance/joins", headers=_h("priya"))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "src" and "proposals" in body          # existing fields intact
    kinds = {d["kind"] for d in body["divergences"]}
    assert kinds == {"dropped", "retargeted"}
    dropped = next(d for d in body["divergences"] if d["kind"] == "dropped")
    assert dropped["from_ref"] == "public.transactions.acct_id"
    assert dropped["verified_to_ref"] == "public.accounts.account_id"
    assert dropped["declared_to_ref"] is None and dropped["detected_at"]


def test_joins_list_excludes_acknowledged_and_other_sources(client, conn):
    _seed_divergence(conn, source="othersrc")
    acked = _seed_divergence(conn, from_ref="public.t.a", verified_to="public.p.a")
    conn.execute("UPDATE governed_join_divergence SET acknowledged_at = %s,"
                 " acknowledged_by = 'user:x' WHERE id = %s", (_NOW, acked))
    r = client.get("/sources/src/governance/joins", headers=_h("priya"))
    assert r.status_code == 200 and r.json()["divergences"] == []


def test_acknowledge_stamps_subject_and_clears_from_the_open_list(client, conn):
    div_id = _seed_divergence(conn)
    r = client.post(f"/governance/joins/divergences/{div_id}/acknowledge", headers=_h("priya"))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == div_id and body["acknowledged_by"] == "user:priya"
    assert body["acknowledged_at"] is not None and body["kind"] == "dropped"
    assert client.get("/sources/src/governance/joins",
                      headers=_h("priya")).json()["divergences"] == []


def test_acknowledge_unknown_id_is_404(client):
    r = client.post("/governance/joins/divergences/999999999/acknowledge", headers=_h("priya"))
    assert r.status_code == 404


def test_acknowledge_requires_platform_admin(client, conn):
    div_id = _seed_divergence(conn)
    r = client.post(f"/governance/joins/divergences/{div_id}/acknowledge",
                    headers=_h("dana", roles="data_owner"))
    assert r.status_code == 403
