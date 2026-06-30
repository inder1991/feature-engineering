from dataclasses import dataclass
from typing import Any

from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.overlay.authority import (
    Authority,
    _actor_is_authority,
    proposer_ne_confirmer,
    resolve_authority,
)
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    display_object_ref,
)


class _Cat:
    """Minimal CatalogAdapter test double keyed on the display object_ref string."""

    def __init__(self, owners: dict[str, str] | None = None) -> None:
        self._owners = owners or {}

    def owner_of(self, ref: CatalogObjectRef) -> str | None:
        return self._owners.get(display_object_ref(ref))

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def list_objects(self):
        return []

    def fingerprint(self):
        return {}


@dataclass(frozen=True)
class _Evt:
    type: str
    payload: dict[str, Any]


def _orders() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _customers() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "sales", "customers")


def test_data_fact_resolves_to_data_owner(db):
    cat = _Cat({display_object_ref(_orders()): "user:alice"})
    auth = resolve_authority(db, cat, _orders(), "grain")
    assert auth.role == "data_owner"
    assert auth.gate == "OVERLAY_DATA_OWNER"
    assert auth.subjects == ("user:alice",)
    assert auth.governance_queue is False
    assert auth.eligible_assignees == {"role": "data_owner", "subject": "user:alice"}
    assert auth.task_assignees == ({"role": "data_owner", "subject": "user:alice"},)


def test_policy_tag_resolves_to_compliance(db):
    cat = _Cat({display_object_ref(_orders()): "user:alice"})
    auth = resolve_authority(db, cat, _orders(), "policy_tag")
    assert auth.role == "compliance"
    assert auth.gate == "OVERLAY_COMPLIANCE"
    assert auth.subjects == ()
    assert auth.eligible_assignees == {"role": "compliance"}
    assert auth.task_assignees == ({"role": "compliance"},)


def test_unknown_owner_routes_to_governance_not_submitter(db):
    cat = _Cat({})  # ownership not recorded
    auth = resolve_authority(db, cat, _orders(), "availability_time")
    assert auth.governance_queue is True
    assert auth.role == "platform-admin"
    assert auth.eligible_assignees == {"role": "platform-admin"}
    assert auth.task_assignees == ({"role": "platform-admin"},)
    assert "user:" not in str(auth.subjects)  # never the request submitter


def test_approved_join_two_distinct_owners_is_dual(db):
    a = _orders()
    b = _customers()
    cat = _Cat({display_object_ref(a): "user:alice", display_object_ref(b): "user:bob"})
    ref = ApprovedJoinRef(a, b, (ColumnPair("customer_id", "id"),), "N:1")
    auth = resolve_authority(db, cat, ref, "approved_join")
    assert auth.dual is True
    assert auth.governance_queue is False
    assert auth.subjects == ("user:alice", "user:bob")
    # one side-labelled task per side — never collapsed
    assert auth.task_assignees == (
        {"role": "data_owner", "subject": "user:alice", "side": "from"},
        {"role": "data_owner", "subject": "user:bob", "side": "to"},
    )


def test_approved_join_same_owner_both_sides_is_not_dual(db):
    a = _orders()
    b = _customers()
    cat = _Cat({display_object_ref(a): "user:alice", display_object_ref(b): "user:alice"})
    ref = ApprovedJoinRef(a, b, (ColumnPair("customer_id", "id"),), "N:1")
    auth = resolve_authority(db, cat, ref, "approved_join")
    assert auth.dual is False
    assert auth.subjects == ("user:alice", "user:alice")
    # same owner owns BOTH sides → a single (un-side-labelled) task is the ONLY collapse case
    assert auth.task_assignees == ({"role": "data_owner", "subject": "user:alice"},)


def test_approved_join_mixed_owner_routes_only_unknown_side_to_governance(db):
    a = _orders()
    b = _customers()
    cat = _Cat({display_object_ref(a): "user:alice"})  # b's owner unknown
    ref = ApprovedJoinRef(a, b, (ColumnPair("customer_id", "id"),), "N:1")
    auth = resolve_authority(db, cat, ref, "approved_join")
    assert auth.governance_queue is True
    assert auth.dual is True  # known owner + governance side = two distinct confirmations
    assert auth.subjects == ("user:alice", None)
    plans = auth.task_assignees
    assert {"role": "data_owner", "subject": "user:alice", "side": "from"} in plans
    assert {"role": "platform-admin", "side": "to"} in plans
    # the known owner is NEVER folded onto the governance task (decision 7)
    gov = next(p for p in plans if p["role"] == "platform-admin")
    assert "subject" not in gov
    assert gov["side"] == "to"


def test_approved_join_both_unknown_is_still_dual_two_governance_tasks(db):
    a = _orders()
    b = _customers()
    cat = _Cat({})  # neither owner recorded
    ref = ApprovedJoinRef(a, b, (ColumnPair("customer_id", "id"),), "N:1")
    auth = resolve_authority(db, cat, ref, "approved_join")
    assert auth.governance_queue is True
    assert auth.dual is True  # both-unknown STILL needs two distinct governance approvals
    assert auth.role == "platform-admin"
    assert auth.subjects == (None, None)
    plans = auth.task_assignees
    # two side-labelled governance tasks — never collapsed to one (finding 3 / decision 19)
    assert plans == (
        {"role": "platform-admin", "side": "from"},
        {"role": "platform-admin", "side": "to"},
    )
    assert len(plans) == 2


def test_actor_is_authority(db):
    cat = _Cat({display_object_ref(_orders()): "user:alice"})
    alice = build_human_identity(subject="user:alice", role_claims=("data_owner",))
    bob = build_human_identity(subject="user:bob", role_claims=("data_owner",))

    # data-owner fact: only the recorded owner is an authority (fine-grained owner-of-object)
    data_auth = resolve_authority(db, cat, _orders(), "grain")
    assert _actor_is_authority(data_auth, alice) is True
    assert _actor_is_authority(data_auth, bob) is False

    # policy_tag fact: only the compliance role is an authority
    comp_auth = resolve_authority(db, cat, _orders(), "policy_tag")
    carol = build_human_identity(subject="user:carol", role_claims=("compliance",))
    assert _actor_is_authority(comp_auth, carol) is True
    assert _actor_is_authority(comp_auth, alice) is False

    # governance-queue fact (unknown owner): the platform-admin role is accepted
    gov_auth = resolve_authority(db, _Cat({}), _orders(), "availability_time")
    admin = build_human_identity(subject="user:dan", role_claims=("platform-admin",))
    assert gov_auth.governance_queue is True
    assert _actor_is_authority(gov_auth, admin) is True
    assert _actor_is_authority(gov_auth, alice) is False


def test_actor_is_authority_approved_join_sides(db):
    a = _orders()
    b = _customers()
    alice = build_human_identity(subject="user:alice", role_claims=("data_owner",))
    bob = build_human_identity(subject="user:bob", role_claims=("data_owner",))
    admin = build_human_identity(subject="user:dan", role_claims=("platform-admin",))
    ref = ApprovedJoinRef(a, b, (ColumnPair("customer_id", "id"),), "N:1")

    # both owners known → each owner is an authority; a bare platform-admin is NOT
    both = resolve_authority(
        db,
        _Cat({display_object_ref(a): "user:alice", display_object_ref(b): "user:bob"}),
        ref,
        "approved_join",
    )
    assert _actor_is_authority(both, alice) is True
    assert _actor_is_authority(both, bob) is True
    assert _actor_is_authority(both, admin) is False

    # mixed → known owner OR platform-admin (for the governance side) are authorities
    mixed = resolve_authority(db, _Cat({display_object_ref(a): "user:alice"}), ref, "approved_join")
    assert _actor_is_authority(mixed, alice) is True
    assert _actor_is_authority(mixed, admin) is True
    assert _actor_is_authority(mixed, bob) is False


def test_proposer_ne_confirmer(db):
    alice = build_human_identity(subject="user:alice", role_claims=("data_owner",))
    bob = build_human_identity(subject="user:bob", role_claims=("data_owner",))
    svc = build_service_identity(
        subject="service:profiler", role_claims=("overlay",), attestation="sig"
    )
    human_proposed = [_Evt("OVERLAY_FACT_PROPOSED", {"proposed_by": "user:alice"})]
    svc_proposed = [_Evt("OVERLAY_FACT_PROPOSED", {"proposed_by": "service:profiler"})]
    assert proposer_ne_confirmer(human_proposed, alice) is False  # self-confirm blocked
    assert proposer_ne_confirmer(human_proposed, bob) is True
    assert proposer_ne_confirmer(svc_proposed, alice) is True  # service proposal, human confirm
    assert isinstance(Authority(role="x", gate="g", subjects=(), governance_queue=False), Authority)
    _ = svc  # service identity used only to anchor the four-eyes scenario above
