"""BR-23 — the review routes: attributable decisions, optimistic concurrency, named gaps."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

RECIPE_ID = "posted_debit_amount"
PATH = f"/recipes/{RECIPE_ID}/reviews"
LIVE_HASH = canonical_recipe_v2_hash(v2_recipe_by_id(RECIPE_ID))


def _governor(user: str = "sme-lead") -> dict:
    return {"X-User": user, "X-Roles": "platform_admin"}


def _reader() -> dict:
    return {"X-User": "viewer", "X-Roles": "catalog_viewer"}


def _decision(**over) -> dict:
    body = {"decision": "approved", "reviewer_role": "banking_sme",
            "reviewed_revision_hash": LIVE_HASH, "rationale": "shape is right"}
    body.update(over)
    return body


def test_the_read_returns_validity_with_named_gaps(client):
    r = client.get(PATH, headers=_reader())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe_revision_hash"] == LIVE_HASH
    assert body["validity"]["current"] is False
    assert body["validity"]["required_roles"] == [
        "banking_sme", "data_semantic_owner", "formula_engineering"]
    assert body["validity"]["missing_roles"] == body["validity"]["required_roles"]


def test_a_decision_is_recorded_attributably_from_the_session(client):
    r = client.post(PATH, headers=_governor("priya"), json=_decision())
    assert r.status_code == 201, r.text
    events = client.get(PATH, headers=_reader()).json()["events"]
    assert events[-1]["reviewer"] == "user:priya"       # the SESSION subject, never the body
    assert events[-1]["decision"] == "approved"


def test_the_write_requires_the_governance_permission(client):
    r = client.post(PATH, headers=_reader(), json=_decision())
    assert r.status_code == 403


def test_a_stale_revision_hash_is_a_409(client):
    r = client.post(PATH, headers=_governor(),
                    json=_decision(reviewed_revision_hash="0" * 64))
    assert r.status_code == 409
    assert "changed since you reviewed it" in r.json()["detail"]


def test_an_unknown_recipe_is_a_404_and_bad_vocabulary_a_422(client):
    r = client.get("/recipes/not_a_recipe/reviews", headers=_reader())
    assert r.status_code == 404
    r = client.post(PATH, headers=_governor(), json=_decision(reviewer_role="vibes"))
    assert r.status_code == 422


def test_full_approval_folds_current_with_two_identities(client):
    recipe = v2_recipe_by_id(RECIPE_ID)
    from featuregen.overlay.upload.recipe_review_validity import required_reviewer_roles

    reviewers = iter(("priya", "omar", "lin"))
    for role in required_reviewer_roles(recipe):
        r = client.post(PATH, headers=_governor(next(reviewers)),
                        json=_decision(reviewer_role=role))
        assert r.status_code == 201, r.text
    validity = client.get(PATH, headers=_reader()).json()["validity"]
    assert validity["current"] is True
    assert validity["missing_roles"] == []
