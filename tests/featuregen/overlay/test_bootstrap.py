from psycopg.rows import dict_row

from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
from featuregen.overlay.catalog import register_catalog_adapter
from featuregen.overlay.identity import CatalogObjectRef, fact_key


class _Registry:
    """Stand-in HandlerRegistry; Phase 4 registers no runtime handlers."""

    def __init__(self):
        self.handlers = {}

    def register(self, handler):
        self.handlers[handler.name] = handler


def _orders():
    return CatalogObjectRef("pg:core", "table", "sales", "orders")


def _wire(db, catalog):
    register_overlay(_Registry())
    seed_authz_policy(db)
    seed_overlay_authz(db)
    register_command_authorizer(PolicyAuthorizer())
    from tests.featuregen.overlay.conftest import StubCatalog  # type: ignore

    cat = StubCatalog()
    cat.set_owner(_orders(), "user:alice")
    register_catalog_adapter(cat)
    return cat


def test_data_owner_can_propose_and_confirm_via_execute_command(db, catalog):
    _wire(db, catalog)
    svc = build_service_identity(subject="service:profiler", role_claims=("overlay",), attestation="sig")
    owner = build_human_identity(subject="user:alice", role_claims=("data_owner",))

    proposed = execute_command(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "proposed_value": {"columns": ["order_id"], "is_unique": True}},
            svc,
            "ik-propose",
        ),
    )
    assert proposed.accepted is True, proposed.denied_reason
    draft = proposed.produced_event_ids[0]

    confirmed = execute_command(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "target_event_id": draft},
            owner,
            "ik-confirm",
        ),
    )
    assert confirmed.accepted is True, confirmed.denied_reason
    key = fact_key(_orders(), "grain")
    n = db.execute(
        "SELECT count(*) FROM events WHERE overlay_fact_id=%s AND type='OVERLAY_FACT_CONFIRMED'", (key,)
    ).fetchone()[0]
    assert n == 1


def test_wrong_role_is_denied_and_audited(db, catalog):
    _wire(db, catalog)
    mallory = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))
    res = execute_command(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "proposed_value": {"columns": ["order_id"], "is_unique": True}},
            mallory,
            "ik-deny",
        ),
    )
    assert res.accepted is False
    assert res.denied_reason == "no matching authz policy"
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit "
            "WHERE event_type='COMMAND_DENIED' AND attempted_action='propose_fact'"
        )
        assert cur.fetchone()["n"] == 1


def _payments() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "fin", "payments")


def test_platform_admin_clears_governance_queue_via_execute_command(db, catalog):
    """pin 13 (finding 3) — END-TO-END through the PUBLIC `execute_command` path: an unknown-owner
    fact opens a governance-queue task, and a platform-admin runs `confirm_fact` WITHOUT being
    denied by authz (proves the seeded `("confirm_fact","","platform-admin","human",None)` row, not
    just the in-handler `_actor_is_authority` check). `_payments` has NO owner recorded, so authority
    resolves to the platform-admin/governance queue."""
    _wire(db, catalog)  # only sets an owner for _orders(); _payments() owner stays unknown
    svc = build_service_identity(subject="service:profiler", role_claims=("overlay",), attestation="sig")
    admin = build_human_identity(subject="user:admin", role_claims=("platform-admin",))

    proposed = execute_command(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _payments(), "fact_type": "grain", "proposed_value": {"columns": ["payment_id"], "is_unique": True}},
            svc,
            "ik-gov-propose",
        ),
    )
    assert proposed.accepted is True, proposed.denied_reason
    draft = proposed.produced_event_ids[0]
    key = fact_key(_payments(), "grain")
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT eligible_assignees FROM human_tasks WHERE fact_key=%s AND status='open'", (key,)
        )
        assert cur.fetchone()["eligible_assignees"] == {"role": "platform-admin"}

    confirmed = execute_command(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _payments(), "fact_type": "grain", "target_event_id": draft},
            admin,
            "ik-gov-confirm",
        ),
    )
    # NOT denied by authz ("no matching authz policy") — the platform-admin row admits the action.
    assert confirmed.accepted is True, confirmed.denied_reason
    n = db.execute(
        "SELECT count(*) FROM events WHERE overlay_fact_id=%s AND type='OVERLAY_FACT_CONFIRMED'", (key,)
    ).fetchone()[0]
    assert n == 1
