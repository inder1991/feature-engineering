from psycopg.rows import dict_row

from featuregen.contracts import IdentityEnvelope
from featuregen.overlay import facts
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.projection import (
    OverlayProjection,
    current_fact,
    dependents_of,
    read_proposal,
)
from featuregen.overlay.store import append_overlay_event
from featuregen.projections.runner import run_projection


def _human():
    return IdentityEnvelope(
        subject="owner_a", actor_kind="human", authenticated=True,
        auth_method="oidc", role_claims=("data_owner",),
    )


def _grain_fact(db):
    ref = CatalogObjectRef(catalog_source="pg:core", object_kind="table",
                           schema="core", table="transactions")
    fk = fact_key(ref, "grain")
    draft = append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED, actor=_human(), expected_version=0,
        payload={
            "catalog_object_ref": {"catalog_source": "pg:core", "object_kind": "table",
                                   "schema": "core", "table": "transactions"},
            "object_ref": "core.transactions", "fact_type": "grain",
            "proposed_value": {"columns": ["id"], "is_unique": True},
            "proposal_fingerprint": "fp1", "evidence_ref": "eviu_1",
            "proposed_by": "owner_a",  # actor subject STRING (pin 11)
        },
    )
    return fk, draft


def test_propose_creates_proposal_row_and_dependencies(db):
    fk, draft = _grain_fact(db)
    assert run_projection(db, OverlayProjection()) == 1
    prop = read_proposal(db, fk)
    assert prop["status"] == "DRAFT"
    assert prop["proposed_value"] == {"columns": ["id"], "is_unique": True}
    assert prop["proposal_fingerprint"] == "fp1"
    assert prop["draft_event_id"] == draft.event_id
    assert prop["object_ref"] == "core.transactions"
    assert prop["fact_type"] == "grain"
    assert current_fact(db, fk) is None  # not VERIFIED yet
    assert fk in dependents_of(db, "core.transactions")
    assert fk in dependents_of(db, "core.transactions.id")


def test_confirm_advances_fact_state_to_verified(db):
    fk, draft = _grain_fact(db)
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED, actor=_human(), expected_version=1,
        payload={
            "value": {"columns": ["id"], "is_unique": True},
            "confirmers": [{"subject": "owner_a", "role": "data_owner"}],
            "expires_at": "2026-12-31T00:00:00+00:00", "confirms_event_id": draft.event_id,
        },
    )
    applied = run_projection(db, OverlayProjection())
    assert applied == 2
    fact = current_fact(db, fk)
    assert fact["status"] == "VERIFIED"
    assert fact["value"] == {"columns": ["id"], "is_unique": True}
    assert fact["confirmers"] == [{"subject": "owner_a", "role": "data_owner"}]
    assert fact["object_ref"] == "core.transactions"
    assert fact["fact_type"] == "grain"
    # checkpoint advanced
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name='overlay'")
        assert cur.fetchone()["checkpoint_seq"] > 0


def test_expiry_moves_value_to_prior_value_and_status_reverify(db):
    fk, draft = _grain_fact(db)
    confirmed = append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED, actor=_human(), expected_version=1,
        payload={
            "value": {"columns": ["id"], "is_unique": True},
            "confirmers": [{"subject": "owner_a", "role": "data_owner"}],
            "expires_at": "2026-12-31T00:00:00+00:00", "confirms_event_id": draft.event_id,
        },
    )
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_EXPIRED, actor=_human(), expected_version=2,
        payload={"expires_confirmed_event_id": confirmed.event_id},
    )
    run_projection(db, OverlayProjection())
    fact = current_fact(db, fk)
    assert fact["status"] == "REVERIFY"
    assert fact["value"] is None
    assert fact["prior_value"] == {"columns": ["id"], "is_unique": True}
    # the in-flight proposal row also carries the retired value + the re-verify CAS target so the
    # re-verify task / get_task_proposal can surface prior_value (P2 finding 7)
    prop = read_proposal(db, fk)
    assert prop["status"] == "REVERIFY"
    assert prop["prior_value"] == {"columns": ["id"], "is_unique": True}
    assert prop["target_event_id"] == confirmed.event_id


def test_approved_join_indexes_both_tables_and_all_paired_columns(db):
    from featuregen.overlay.identity import ApprovedJoinRef, ColumnPair

    tx = CatalogObjectRef(catalog_source="pg:core", object_kind="table",
                          schema="core", table="transactions")
    cust = CatalogObjectRef(catalog_source="pg:core", object_kind="table",
                            schema="core", table="customers")
    join_ref = ApprovedJoinRef(
        from_ref=tx, to_ref=cust,
        column_pairs=(ColumnPair("customer_id", "id"), ColumnPair("region", "region")),
        cardinality="N:1",
    )
    fk = fact_key(join_ref, "approved_join")
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED, actor=_human(), expected_version=0,
        payload={
            "catalog_object_ref": {"kind": "relation"},
            "object_ref": "core.transactions -> core.customers",  # display string — NEVER parsed
            "fact_type": "approved_join",
            "proposed_value": {
                "from_ref": {"catalog_source": "pg:core", "object_kind": "table",
                             "schema": "core", "table": "transactions"},
                "to_ref": {"catalog_source": "pg:core", "object_kind": "table",
                           "schema": "core", "table": "customers"},
                "column_pairs": [{"from_col": "customer_id", "to_col": "id"},
                                 {"from_col": "region", "to_col": "region"}],
                "cardinality": "N:1",
            },
            "proposal_fingerprint": "fpj", "evidence_ref": None,
            "proposed_by": "owner_a",  # actor subject STRING (pin 11)
        },
    )
    assert run_projection(db, OverlayProjection()) == 1
    # BOTH tables AND all paired columns on BOTH sides get a dependency row, read from the
    # STRUCTURED value (finding 4b)...
    for ref_object in (
        "core.transactions", "core.customers",
        "core.transactions.customer_id", "core.customers.id",
        "core.transactions.region", "core.customers.region",
    ):
        assert fk in dependents_of(db, ref_object), ref_object
    # ...and the synthetic "from -> to" display relation string is NEVER indexed.
    assert dependents_of(db, "core.transactions -> core.customers") == []


def test_reproposal_after_rejected_only_keeps_new_dependency_rows(db):
    # Pin 18: re-proposing after REJECTED with DIFFERENT referenced columns must leave ONLY the new
    # dependency rows — apply() DELETEs the fact's dependency rows before re-inserting the fresh set,
    # so the stale column row from the rejected proposal does not linger.
    ref = CatalogObjectRef(catalog_source="pg:core", object_kind="table",
                           schema="core", table="transactions")
    fk = fact_key(ref, "grain")
    base = {
        "catalog_object_ref": {"catalog_source": "pg:core", "object_kind": "table",
                               "schema": "core", "table": "transactions"},
        "object_ref": "core.transactions", "fact_type": "grain",
        "proposed_by": "owner_a", "evidence_ref": None,
    }
    draft = append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED, actor=_human(), expected_version=0,
        payload={**base, "proposed_value": {"columns": ["id"], "is_unique": True},
                 "proposal_fingerprint": "fp1"},
    )
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_REJECTED, actor=_human(), expected_version=1,
        payload={"rejected_by": "owner_a", "reason": "wrong", "target_event_id": draft.event_id},
    )
    # Re-propose after REJECTED with a DIFFERENT grain column (txn_id, not id) + a fresh fingerprint.
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED, actor=_human(), expected_version=2,
        payload={**base, "proposed_value": {"columns": ["txn_id"], "is_unique": True},
                 "proposal_fingerprint": "fp2"},
    )
    assert run_projection(db, OverlayProjection()) == 3
    # The table itself and the NEW column are indexed; the stale `id` column row is gone.
    assert fk in dependents_of(db, "core.transactions")
    assert fk in dependents_of(db, "core.transactions.txn_id")
    assert dependents_of(db, "core.transactions.id") == []
