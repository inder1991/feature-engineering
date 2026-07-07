"""RBAC: the 5 functional roles map to permissions, and the routes enforce those permissions."""
from ._helpers import DEPOSITS_CSV, ENGINEER, OWNER, VIEWER, upload_csv


def _feature_body():
    return {"name": "boundary_feat", "derives_from": []}


def test_permission_map():
    from featuregen.identity.permissions import (
        CATALOG_READ,
        CATALOG_WRITE,
        FEATURE_GENERATE,
        FEATURE_READ,
        IAM_MANAGE,
        has_permission,
        permissions_for,
        roles_granting,
    )
    assert permissions_for(["catalog_viewer"]) == {CATALOG_READ, FEATURE_READ}
    assert permissions_for(["data_owner"]) == {CATALOG_READ, CATALOG_WRITE}
    assert permissions_for(["feature_engineer"]) == {CATALOG_READ, FEATURE_READ, FEATURE_GENERATE}
    assert permissions_for(["access_admin"]) == {IAM_MANAGE}
    assert has_permission(["platform_admin"], IAM_MANAGE)
    assert not has_permission(["catalog_viewer"], CATALOG_WRITE)
    assert permissions_for(["nonsense"]) == set()          # unknown role grants nothing
    assert permissions_for(["engineer", "data_owner"]) == {CATALOG_READ, CATALOG_WRITE}  # union; 'engineer' unknown
    assert roles_granting(IAM_MANAGE) == ["access_admin", "platform_admin"]


def test_data_owner_can_upload_but_not_read_features_or_generate(client):
    assert upload_csv(client, "deposits", DEPOSITS_CSV, headers=OWNER).status_code == 200
    assert client.get("/features", headers=OWNER).status_code == 403          # no feature:read
    assert client.post("/features", json=_feature_body(), headers=OWNER).status_code == 403  # no feature:generate


def test_feature_engineer_can_generate_and_read_but_not_upload(client):
    assert client.get("/features", headers=ENGINEER).status_code == 200       # feature:read
    assert client.post("/features", json=_feature_body(), headers=ENGINEER).status_code == 200  # feature:generate
    assert upload_csv(client, "deposits", DEPOSITS_CSV,
                      headers=ENGINEER).status_code == 403                      # no catalog:write


def test_catalog_viewer_is_read_only(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)                               # seed as platform_admin
    assert client.get("/search", params={"q": "acct"}, headers=VIEWER).status_code == 200   # catalog:read
    assert client.get("/features", headers=VIEWER).status_code == 200         # feature:read
    assert upload_csv(client, "deposits", DEPOSITS_CSV,
                      headers=VIEWER).status_code == 403                        # no catalog:write
    assert client.post("/features", json=_feature_body(),
                       headers=VIEWER).status_code == 403                       # no feature:generate


def test_no_functional_role_is_denied(client):
    norole = {"X-User": "n", "X-Roles": ""}
    assert client.get("/search", params={"q": "x"}, headers=norole).status_code == 403
    assert upload_csv(client, "deposits", DEPOSITS_CSV, headers=norole).status_code == 403


def test_write_denied_audit_records_a_tamper_evident_row(db):
    # The denial-audit record: an ACCESS_DENIED row on the security_audit chain naming who + what.
    from tests.featuregen._helpers import mint_test_identity

    from featuregen.api.deps import _write_denied_audit
    ident = mint_test_identity(subject="user:priya", role_claims=["catalog_viewer"])
    _write_denied_audit(db, ident, "catalog:write on POST /uploads")
    row = db.execute(
        "SELECT event_type, decision, actor->>'subject', attempted_action "
        "FROM security_audit WHERE event_type = 'ACCESS_DENIED'").fetchone()
    assert row == ("ACCESS_DENIED", "denied", "user:priya", "catalog:write on POST /uploads")
