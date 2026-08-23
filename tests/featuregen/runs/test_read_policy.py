"""Object-level run visibility (spec §11).

These are pure predicates over an identity envelope — no database and no verifier — so the
envelopes are built directly rather than minted through `mint_test_identity`."""
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.read_policy import is_platform_admin, visibility_where


def _env(subject, *roles):
    return IdentityEnvelope(subject=subject, actor_kind="human", authenticated=True,
                            auth_method="test", role_claims=tuple(roles))


def test_admin_sees_everything():
    assert is_platform_admin(_env("a", "platform_admin"))
    frag, params = visibility_where(_env("a", "platform_admin"))
    assert frag == "TRUE" and params == []


def test_owner_predicate_covers_identity_and_pre_spine_actor():
    frag, params = visibility_where(_env("priya", "feature_engineer"))
    assert "fri.owner_subject" in frag and "fgr.actor" in frag
    assert params == ["priya"]


def test_hyphenated_confirmer_claim_is_not_the_functional_role():
    assert not is_platform_admin(_env("a", "platform-admin"))
