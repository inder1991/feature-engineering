"""A generation may be authorized for a RULE-BASED label, not only a catalog column.

The registry was an island: a person could author and register a derived label and nothing in the
platform could then be trained against it. This is the seam that connects it — chosen to sit BESIDE
`target_ref` rather than replace it, because a bare column target fits neither rule shape and
"superseding" it would mean inventing a passthrough shape purely to migrate old rows.

The hazard this suite exists for: `revision_id` is content-addressed over `identity_payload`, and
`verification_attempt` is keyed on it. A new key added unconditionally would re-mint every
authorization ever recorded and silently orphan its verifications.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.generation_authorization import (
    GenerationAuthorizationV1,
    load_generation_authorization,
    record_generation_authorization,
)
from featuregen.overlay.upload.selection_revisions import TargetModeV1

#: Captured from the code BEFORE `target_definition_id` existed. These are the anchors: if either
#: moves, every authorization in the database has been re-minted and every `verification_attempt`
#: keyed on one now points at nothing.
_COLUMN_TARGET_ID = "b87d306cd5dfd719c3c810dd93986665a88d8db1ce87bff6acd3cf340eaa8ab0"
_EXPLORATION_ID = "9eb0e5c23cbcd459497533ef29f04dd08015f0ac014d29fd875ee76968940b1f"


def _auth(**over) -> GenerationAuthorizationV1:
    base = dict(environment_id="sandbox", logical_group_name="grp",
                build_set_revision_id="bs1", target_mode=TargetModeV1.PREDICTION,
                target_ref="public.accounts.churned")
    return GenerationAuthorizationV1(**{**base, **over})


# ══ the identity must not move ═══════════════════════════════════════════════════════════════════

def test_a_COLUMN_target_authorization_keeps_the_id_it_always_had():
    """The whole reason the new key is conditional. An unconditional one would change this."""
    assert _auth().revision_id == _COLUMN_TARGET_ID


def test_an_EXPLORATION_authorization_keeps_the_id_it_always_had():
    assert _auth(target_mode=TargetModeV1.EXPLORATION,
                 target_ref=None).revision_id == _EXPLORATION_ID


def test_the_new_key_is_ABSENT_from_the_payload_when_there_is_no_rule_target():
    """Stated directly, so the mechanism is pinned and not just its consequence."""
    assert "target_definition_id" not in _auth().identity_payload()


# ══ a rule-based label satisfies PREDICTION ══════════════════════════════════════════════════════

def test_a_prediction_may_be_authorized_for_a_RULE_instead_of_a_column():
    auth = _auth(target_ref=None, target_definition_id="def-abc")
    assert auth.identity_payload()["target_definition_id"] == "def-abc"


def test_two_different_labels_are_two_different_authorizations():
    """Otherwise re-authorizing against a different label would silently reuse the first's id."""
    one = _auth(target_ref=None, target_definition_id="def-abc")
    two = _auth(target_ref=None, target_definition_id="def-xyz")
    assert one.revision_id != two.revision_id


def test_a_rule_target_is_NOT_confusable_with_a_column_target_of_the_same_text():
    """The two live in different keys, so an id collision between them cannot happen."""
    assert _auth(target_ref="def-abc").revision_id != \
        _auth(target_ref=None, target_definition_id="def-abc").revision_id


# ══ the invariant, restated for two kinds of target ══════════════════════════════════════════════

def test_a_prediction_with_NEITHER_kind_of_target_is_refused():
    with pytest.raises(ValueError, match="predict something nobody named"):
        _auth(target_ref=None)


def test_an_exploration_carrying_a_rule_target_is_refused():
    """An exploration build HAS no target — that is what the mode means."""
    with pytest.raises(ValueError, match="disagree"):
        _auth(target_mode=TargetModeV1.EXPLORATION, target_ref=None,
              target_definition_id="def-abc")


def test_BOTH_kinds_of_target_at_once_is_refused():
    """Two fields that can disagree eventually do — and here they would name two different things
    to predict in one authorization."""
    with pytest.raises(ValueError, match="one kind of target"):
        _auth(target_definition_id="def-abc")


# ══ it survives the database ═════════════════════════════════════════════════════════════════════

def _build_set(db) -> None:
    """The authorization's foreign keys — an authorization covers a real, declared build set."""
    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, "
               "redacted_hypothesis) VALUES ('int-1','h','hypothesis','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
               "VALUES ('trr-1','int-1','exploration','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
               "declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
               "VALUES ('bs1','trr-1','dh','{}'::jsonb,'bs1','user:ops','2026-09-03') "
               "ON CONFLICT DO NOTHING")


def _register(db) -> str:
    from featuregen.overlay.upload.target_contract import StateChangeRuleV1, TargetHeaderV1
    from featuregen.overlay.upload.target_store import register_target
    _build_set(db)
    rule = StateChangeRuleV1(
        header=TargetHeaderV1(
            name="tgt_npe_90d", entity="customer", anchor_catalog="cib",
            grain_ref="public.bo_cib_customer.cust_num",
            as_of_ref="public.bo_cib_customer.business_dt", window_days=90,
            as_of_frequency="monthly", label_type="binary", operator=">=", threshold=1),
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Non-performing",))
    return register_target(db, rule, description="d", registered_by="analyst")


def test_a_rule_target_survives_a_round_trip(db):
    definition_id = _register(db)
    auth = _auth(target_ref=None, target_definition_id=definition_id)
    record_generation_authorization(db, auth, authorized_by="analyst", authorized_at="2026-09-03")
    loaded = load_generation_authorization(db, auth.revision_id)
    assert loaded.target_definition_id == definition_id
    assert loaded.target_ref is None


def test_the_DATABASE_also_refuses_both_kinds_at_once(db):
    """Defence at the constraint, not only in Python — a direct writer bypasses the dataclass."""
    definition_id = _register(db)
    with pytest.raises(Exception, match="target_matches_mode"):
        db.execute(
            "INSERT INTO generation_authorization (revision_id, environment_id, "
            "logical_group_name, build_set_revision_id, target_mode, target_ref, "
            "target_definition_id, authorized_by, authorized_at) "
            "VALUES ('r1','sandbox','grp','bs1','prediction','public.a.b',%s,'x','y')",
            (definition_id,))


def test_the_DATABASE_refuses_a_prediction_with_no_target_of_either_kind(db):
    with pytest.raises(Exception, match="target_matches_mode"):
        db.execute(
            "INSERT INTO generation_authorization (revision_id, environment_id, "
            "logical_group_name, build_set_revision_id, target_mode, authorized_by, "
            "authorized_at) "
            "VALUES ('r2','sandbox','grp','bs1','prediction','x','y')")


def test_a_rule_target_must_REFERENCE_a_real_definition(db):
    """A label that was never registered cannot be trained against."""
    with pytest.raises(Exception, match="foreign key|violates"):
        db.execute(
            "INSERT INTO generation_authorization (revision_id, environment_id, "
            "logical_group_name, build_set_revision_id, target_mode, target_definition_id, "
            "authorized_by, authorized_at) "
            "VALUES ('r3','sandbox','grp','bs1','prediction','never-registered','x','y')")


# ══ the consumer record — §9's first job ═════════════════════════════════════════════════════════

def test_authorizing_a_generation_RECORDS_the_label_as_consumed(db):
    """§9: "tran_crncy is being retired — which labels break, and who is training on them?" The
    table existed from the first migration and nothing ever wrote to it, so the question had no
    answer. Authorization is the moment a label acquires a consumer."""
    definition_id = _register(db)
    auth = _auth(target_ref=None, target_definition_id=definition_id)
    record_generation_authorization(db, auth, authorized_by="analyst", authorized_at="2026-09-03")
    rows = db.execute("SELECT consumer_ref FROM target_consumer WHERE definition_id = %s",
                      (definition_id,)).fetchall()
    assert [r[0] for r in rows] == [f"generation_authorization:{auth.revision_id}"]


def test_recording_the_same_authorization_twice_yields_ONE_consumer(db):
    """Authorization is idempotent on content; its consumer record must be too."""
    definition_id = _register(db)
    auth = _auth(target_ref=None, target_definition_id=definition_id)
    for _ in range(2):
        record_generation_authorization(db, auth, authorized_by="analyst",
                                        authorized_at="2026-09-03")
    count = db.execute("SELECT count(*) FROM target_consumer WHERE definition_id = %s",
                       (definition_id,)).fetchone()[0]
    assert count == 1


def test_a_COLUMN_target_authorization_records_no_consumer(db):
    """There is no definition to consume — and writing a row keyed on a NULL would fail loudly."""
    _build_set(db)
    record_generation_authorization(db, _auth(), authorized_by="analyst",
                                    authorized_at="2026-09-03")
    assert db.execute("SELECT count(*) FROM target_consumer").fetchone()[0] == 0
