from sp0.attempt_memory.store import record_attempt
from sp0.contracts import IdentityEnvelope
from sp0.privacy.crypto_shred import ErasureOutcome, crypto_shred
from sp0.privacy.legal_hold import place_legal_hold

ACTOR = IdentityEnvelope(
    subject="user:dpo", actor_kind="human", authenticated=True,
    auth_method="oidc", role_claims=("privacy",),
)


class FakeKeyManager:
    def __init__(self):
        self.destroyed: set[str] = set()

    def destroy(self, kms_key_id):
        self.destroyed.add(kms_key_id)

    def rotate(self, old_kms_key_id, object_key):  # pragma: no cover - unused here
        return old_kms_key_id + "_v2"


def _blob(db, blob_id, classification, key):
    db.execute(
        "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, kms_key_id, status) "
        "VALUES (%s, %s, %s, %s, %s, 'live')",
        (blob_id, "k/" + blob_id, "sha256:x", classification, key),
    )


def _status(db, blob_id):
    return db.execute("SELECT status FROM blob_index WHERE blob_id = %s", (blob_id,)).fetchone()[0]


def test_crypto_shred_targets_pii_erasable_and_retains_the_rest(db):
    _blob(db, "blob_p", "pii-erasable", "k1")
    _blob(db, "blob_g", "governance-retained", "k2")
    _blob(db, "blob_h", "pii-erasable", "k3")
    place_legal_hold(db, hold_id="hold_h", scope_kind="blob", scope_ref="blob_h",
                     reason="audit", placed_by=ACTOR)
    record_attempt(db, definition_hash="keep_me", disposition="rejected", feature_id="feat_1")
    db.execute(
        "INSERT INTO security_audit (security_event_id, event_type, actor, attempted_action, decision, entry_hash) "
        "VALUES ('sec_keep', 'COMMAND_DENIED', '{}'::jsonb, 'create_run', 'denied', 'h0')"
    )

    km = FakeKeyManager()
    outcomes = crypto_shred(
        db, ["blob_p", "blob_g", "blob_h", "blob_missing"],
        reason="gdpr erasure", requested_by=ACTOR, key_manager=km,
    )
    by_id = {o.blob_id: o.outcome for o in outcomes}
    assert isinstance(outcomes[0], ErasureOutcome)
    assert by_id == {
        "blob_p": "shredded",
        "blob_g": "retained_governance",
        "blob_h": "retained_legal_hold",
        "blob_missing": "not_found",
    }
    assert km.destroyed == {"k1"}
    assert _status(db, "blob_p") == "shredded"
    assert _status(db, "blob_g") == "live"
    assert _status(db, "blob_h") == "live"

    # audited: one erasure_audit row per blob, with the outcome recorded
    assert db.execute("SELECT count(*) FROM erasure_audit").fetchone()[0] == 4
    # security stream + attempt-memory are exempt and untouched
    assert db.execute("SELECT count(*) FROM security_audit WHERE security_event_id='sec_keep'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM attempt_memory WHERE definition_hash='keep_me'").fetchone()[0] == 1


def test_governance_retained_body_of_ungoverned_version_is_erasable(db):
    # §9: retention is driven by the OWNING VERSION's governance status, not classification alone.
    # A governance-retained body whose feature_version is no longer active/governed becomes erasable.
    _blob(db, "blob_old_gov", "governance-retained", "k9")
    km = FakeKeyManager()
    outcomes = crypto_shred(
        db, ["blob_old_gov"],
        reason="owning version deprecated + erasure request", requested_by=ACTOR, key_manager=km,
        governance_active=lambda conn, blob_id: False,  # owning version no longer active/governed
    )
    assert outcomes[0].outcome == "shredded"
    assert km.destroyed == {"k9"}
    assert _status(db, "blob_old_gov") == "shredded"
