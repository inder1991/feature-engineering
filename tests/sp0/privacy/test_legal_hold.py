from sp0.contracts import IdentityEnvelope
from sp0.privacy.legal_hold import is_under_legal_hold, place_legal_hold, release_legal_hold

ACTOR = IdentityEnvelope(
    subject="user:legal", actor_kind="human", authenticated=True,
    auth_method="oidc", role_claims=("compliance",),
)


def test_place_then_release_toggles_active_hold(db):
    assert is_under_legal_hold(db, "blob", "blob_h") is False
    place_legal_hold(db, hold_id="hold_1", scope_kind="blob", scope_ref="blob_h",
                     reason="litigation", placed_by=ACTOR)
    assert is_under_legal_hold(db, "blob", "blob_h") is True
    assert is_under_legal_hold(db, "blob", "blob_other") is False
    release_legal_hold(db, "hold_1")
    assert is_under_legal_hold(db, "blob", "blob_h") is False
