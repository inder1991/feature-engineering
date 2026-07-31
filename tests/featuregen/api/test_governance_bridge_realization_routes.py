"""Directional realization governance stays independent of symmetric-link review."""
from __future__ import annotations

from tests.featuregen.overlay.upload.test_bridge_store import _stored_production_realization


def test_list_returns_exact_direction_scope_metrics_and_independent_axes(
    client,
    conn,
    admin_headers,
) -> None:
    revision = _stored_production_realization(conn)

    response = client.get(
        "/sources/cib/governance/bridge-realizations",
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    (view,) = response.json()["realizations"]
    assert view["realization_revision_id"] == revision.realization_revision_id
    assert view["direction"] == {
        "from": "cib::public.customers",
        "to": "ftr::public.transactions",
    }
    assert view["cardinality"] == "many_to_one"
    assert view["cardinality_label"] == "N:1 — exact profile"
    assert view["cardinality_basis"] == "exact_profile"
    assert view["safety_status"] == "deterministically_validated"
    assert view["review_status"] == "unreviewed"
    assert view["execution_eligible"] is True
    assert view["review_controls_execution"] is False
    assert view["evidence_fresh"] is True
    assert view["metrics"][0]["max_right_matches_per_left_row"] == 1
    assert view["metrics"][0]["right_duplicate_row_count"] == 0


def test_realization_review_updates_only_review_and_is_cas_bound(
    client,
    conn,
    admin_headers,
) -> None:
    revision = _stored_production_realization(conn)
    body = {
        "realization_revision_id": revision.realization_revision_id,
        "expected_pointer_version": 2,
        "note": "direction and scope reviewed",
    }

    response = client.post(
        f"/governance/bridge-realizations/{revision.realization_id}/confirm",
        json=body,
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["review_status"] == "human_verified"
    assert result["safety_status"] == "deterministically_validated"
    assert result["execution_eligible"] is True
    assert result["review_controls_execution"] is False
    assert result["pointer_version"] == 3
    assert result["review_projection"] == "current_pointer_updated"
    assert result["authority"] == {
        "role": "platform-admin",
        "confirmation_count": 1,
        "dual": False,
    }

    stale = client.post(
        f"/governance/bridge-realizations/{revision.realization_id}/reject",
        json=body,
        headers=admin_headers,
    )
    assert stale.status_code == 409

    remove = client.post(
        f"/governance/bridge-realizations/{revision.realization_id}/reject",
        json={
            **body,
            "expected_pointer_version": 3,
            "note": "withdraw my endorsement",
        },
        headers=admin_headers,
    )
    assert remove.status_code == 200, remove.text
    assert remove.json()["review_status"] == "unreviewed"
    assert remove.json()["safety_status"] == "deterministically_validated"
    assert remove.json()["execution_eligible"] is True


def test_realization_routes_require_confirmer(client, conn, non_admin_headers) -> None:
    revision = _stored_production_realization(conn)
    assert client.get(
        "/sources/cib/governance/bridge-realizations",
        headers=non_admin_headers,
    ).status_code == 403
    assert client.post(
        f"/governance/bridge-realizations/{revision.realization_id}/confirm",
        json={
            "realization_revision_id": revision.realization_revision_id,
            "expected_pointer_version": 2,
        },
        headers=non_admin_headers,
    ).status_code == 403
