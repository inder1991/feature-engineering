from psycopg.rows import dict_row
from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity

from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.contracts import Command
from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
from featuregen.overlay.catalog import register_catalog_adapter
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.security.audit import verify_chain


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
    from tests.featuregen.overlay._helpers import StubCatalog

    cat = StubCatalog()
    cat.set_owner(_orders(), "user:alice")
    register_catalog_adapter(cat)
    return cat


def test_data_owner_can_propose_and_confirm_via_execute_command(db, catalog):
    _wire(db, catalog)
    svc = mint_test_service_identity(subject="service:profiler", role_claims=("overlay",), attestation="sig")
    owner = mint_test_identity(subject="user:alice", role_claims=("data_owner",))

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
    mallory = mint_test_identity(subject="user:mallory", role_claims=("data_scientist",))
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


def _confirm_denied_count(db) -> int:
    return db.execute(
        "SELECT count(*) FROM security_audit "
        "WHERE event_type='COMMAND_DENIED' AND attempted_action='confirm_fact'"
    ).fetchone()[0]


def test_handler_authority_denial_is_audited(db, catalog):
    """F4: a fine-grained AUTHORITY denial inside the handler must write a tamper-evident
    COMMAND_DENIED row (the coarse PolicyAuthorizer only audits role/kind/scope denials). carol
    holds `compliance` -> PASSES coarse authz (confirm_fact has a compliance row) but is NOT the
    resolved data-owner authority for orders (alice is), so the handler denies in-line."""
    _wire(db, catalog)  # _orders() owned by user:alice; full execute_command path
    svc = mint_test_service_identity(
        subject="service:profiler", role_claims=("overlay",), attestation="sig"
    )
    proposed = execute_command(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain",
             "proposed_value": {"columns": ["order_id"], "is_unique": True}},
            svc,
            "ik-propose",
        ),
    )
    assert proposed.accepted is True, proposed.denied_reason
    draft = proposed.produced_event_ids[0]
    carol = mint_test_identity(subject="user:carol", role_claims=("compliance",))
    res = execute_command(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "target_event_id": draft},
            carol,
            "ik-wrong-authority",
        ),
    )
    assert res.accepted is False
    assert "authority" in res.denied_reason
    assert _confirm_denied_count(db) == 1  # PRE-FIX: 0 -> FAILS
    assert verify_chain(db) is True


def test_handler_four_eyes_denial_is_audited(db, catalog):
    """F4: a four-eyes (SoD) denial inside the handler must also be audited. alice owns orders and
    proposes the fact, then attempts to confirm her own proposal -> proposer != confirmer fails."""
    _wire(db, catalog)  # alice owns orders
    alice = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
    proposed = execute_command(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain",
             "proposed_value": {"columns": ["order_id"], "is_unique": True}},
            alice,
            "ik-propose-self",
        ),
    )
    assert proposed.accepted is True, proposed.denied_reason
    draft = proposed.produced_event_ids[0]
    res = execute_command(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "target_event_id": draft},
            alice,
            "ik-selfconfirm",
        ),
    )
    assert res.accepted is False and "four-eyes" in res.denied_reason
    assert _confirm_denied_count(db) == 1  # PRE-FIX: 0 -> FAILS
    assert verify_chain(db) is True


def test_handler_benign_cas_stale_denial_is_not_audited(db, catalog):
    """F4 guard: a BENIGN CAS-stale denial (concurrency/optimistic-lock, not an authority/SoD
    violation) must NOT be audited — the fix must not over-audit. The legit owner alice confirms
    against a bogus target_event_id, which is denied before any authority check."""
    _wire(db, catalog)  # alice owns orders
    svc = mint_test_service_identity(
        subject="service:profiler", role_claims=("overlay",), attestation="sig"
    )
    proposed = execute_command(
        db,
        Command(
            "propose_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain",
             "proposed_value": {"columns": ["order_id"], "is_unique": True}},
            svc,
            "ik-propose-benign",
        ),
    )
    assert proposed.accepted is True, proposed.denied_reason
    alice = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
    res = execute_command(
        db,
        Command(
            "confirm_fact",
            "overlay_fact",
            None,
            {"ref": _orders(), "fact_type": "grain", "target_event_id": "evt_bogus_superseded"},
            alice,
            "ik-cas-stale",
        ),
    )
    assert res.accepted is False and "stale" in res.denied_reason
    assert _confirm_denied_count(db) == 0  # benign denial stays unaudited


def _payments() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "table", "fin", "payments")


def test_platform_admin_clears_governance_queue_via_execute_command(db, catalog):
    """pin 13 (finding 3) — END-TO-END through the PUBLIC `execute_command` path: an unknown-owner
    fact opens a governance-queue task, and a platform-admin runs `confirm_fact` WITHOUT being
    denied by authz (proves the seeded `("confirm_fact","","platform-admin","human",None)` row, not
    just the in-handler `_actor_is_authority` check). `_payments` has NO owner recorded, so authority
    resolves to the platform-admin/governance queue."""
    _wire(db, catalog)  # only sets an owner for _orders(); _payments() owner stays unknown
    svc = mint_test_service_identity(subject="service:profiler", role_claims=("overlay",), attestation="sig")
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))

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
