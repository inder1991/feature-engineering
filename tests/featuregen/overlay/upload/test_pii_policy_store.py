"""The PII allow-policy contract + store (D14; migration 1056).

The artifact `PERSONAL_DATA_POLICY_REQUIRED` names. What is asserted here, and why in this shape:

* **the registry decides what is licensable, not a list.** A protected characteristic is refused
  with words that say no policy can ever lift it; a non-pii concept is refused as needing none;
* **identity is content, provenance is not.** Two admins declaring the same purpose for the same
  concept resolve to the SAME revision, and who made it current lives on the pointer;
* **revocation is a NEW REVISION.** The approval it withdraws stays readable, and the gate's bulk
  reader stops returning the concept the instant the pointer moves;
* **absence is a refusal everywhere.** No row, a missing pointer and a revoked current revision are
  indistinguishable to `active_pii_use_policies`, which is the only property the gate leans on.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.pii_policy import (
    MAX_PURPOSE_LEN,
    PiiUsePolicyRevisionV1,
    PiiUsePolicyStatus,
    normalize_purpose,
    policy_eligible_concepts,
    validate_policy_concept,
)
from featuregen.overlay.upload.pii_policy_store import (
    active_pii_use_policies,
    approve_pii_use_policy,
    current_pii_use_policy,
    load_pii_use_policy_revision,
    pii_use_policy_states,
    revoke_pii_use_policy,
    set_current_pii_use_policy,
)
from featuregen.overlay.upload.source_selection import (
    PolicyStoreConflict,
    PolicyValidationError,
)

ADMIN = "admin@bank"
AML = "AML transaction monitoring"


# ── what may be licensed at all ─────────────────────────────────────────────────────────────────


def test_a_protected_characteristic_is_refused_with_wording_no_policy_can_lift():
    """The one refusal that must never read as a missing setting. `_use_gate` already refuses such
    an operand as `structurally_unsuitable`; if this surface offered to approve one, the two would
    disagree about the same column and the governance screen would be the one that was wrong."""
    for concept in ("protected_attribute", "special_category", "vulnerability_flag"):
        with pytest.raises(PolicyValidationError) as exc:
            validate_policy_concept(concept)
        assert "NO data-use policy can allow it" in str(exc.value)
        assert "protected characteristic" in str(exc.value)


def test_a_non_personal_data_concept_is_refused_as_needing_no_policy():
    """`segment` is not refused by the gate, so a policy about it would imply it was."""
    with pytest.raises(PolicyValidationError) as exc:
        validate_policy_concept("segment")
    assert "is not personal data" in str(exc.value)


def test_a_concept_the_registry_does_not_carry_cannot_be_licensed():
    with pytest.raises(PolicyValidationError) as exc:
        validate_policy_concept("not_a_concept_at_all")
    assert "not a concept in the registry" in str(exc.value)


def test_the_licensable_set_is_derived_from_the_registry_sensitivity():
    """Not a hand list: a pii concept added tomorrow is licensable the same day."""
    from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY, is_personal_data

    eligible = set(policy_eligible_concepts())
    assert eligible == {n for n in CONCEPT_REGISTRY if is_personal_data(n)}
    # the five A.34 recipe anchors are all in it — the acceptance set has somewhere to go
    assert {"pep_flag", "device_fingerprint", "geolocation", "pii",
            "beneficiary_name"} <= eligible


# ── the purpose bound ───────────────────────────────────────────────────────────────────────────


def test_purpose_is_bounded_at_both_ends():
    with pytest.raises(PolicyValidationError):
        normalize_purpose("   ")
    with pytest.raises(PolicyValidationError):
        normalize_purpose("AML")            # under the floor: a purpose that states nothing
    with pytest.raises(PolicyValidationError):
        normalize_purpose("x" * (MAX_PURPOSE_LEN + 1))
    assert normalize_purpose(f"  {AML}  ") == AML


def test_purpose_whitespace_is_collapsed_before_identity_is_taken():
    """Otherwise "AML  screening" and "AML screening" would be two revisions of one declaration."""
    a = PiiUsePolicyRevisionV1(concept_name="pep_flag", purpose="AML   screening  exposure")
    b = PiiUsePolicyRevisionV1(concept_name="pep_flag", purpose=" AML screening exposure ")
    assert a.revision_id == b.revision_id


def test_a_control_character_is_refused_in_a_governance_declaration():
    with pytest.raises(PolicyValidationError):
        normalize_purpose("AML monitoring\x00 and screening")


# ── identity ────────────────────────────────────────────────────────────────────────────────────


def test_the_revision_id_is_the_content_hash_under_the_pinned_prefix():
    revision = PiiUsePolicyRevisionV1(concept_name="pep_flag", purpose=AML)
    assert revision.revision_id == f"pup_{revision.content_hash}"
    assert len(revision.content_hash) == 64


def test_status_is_content_so_a_revocation_is_a_DIFFERENT_revision():
    active = PiiUsePolicyRevisionV1(concept_name="pep_flag", purpose=AML)
    revoked = PiiUsePolicyRevisionV1(concept_name="pep_flag", purpose=AML,
                                     status=PiiUsePolicyStatus.REVOKED)
    assert active.revision_id != revoked.revision_id
    assert active.active and not revoked.active


def test_the_approver_is_outside_identity(db):
    """The §6.2 house split. Two admins declaring identical content reuse the revision; WHO made it
    current is on the pointer, which is the row that changes."""
    first, v1 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    second, v2 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=v1,
        actor="other@bank")
    assert first == second
    assert (v1, v2) == (1, 2)
    _revision, pointer = current_pii_use_policy(db, "pep_flag")
    assert pointer.declared_by == "other@bank"
    assert db.execute(
        "SELECT approved_by FROM pii_use_policy_revision WHERE revision_id = %s",
        (first,)).fetchone() == (ADMIN,)


# ── the store ───────────────────────────────────────────────────────────────────────────────────


def test_approve_then_read_back(db):
    revision_id, version = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    assert version == 1
    revision, pointer = current_pii_use_policy(db, "pep_flag")
    assert revision.revision_id == revision_id
    assert revision.purpose == AML and revision.active
    assert pointer.pointer_version == 1 and pointer.declared_by == ADMIN
    assert load_pii_use_policy_revision(db, revision_id) == revision


def test_the_provenance_is_human_confirmed_active_because_the_person_IS_the_authority(db):
    """Single-approver (D14) means the declaration's authority is the human who made it — not an
    invented source attestation, and not an LLM proposal promoted by being written down."""
    revision_id, _v = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    revision = load_pii_use_policy_revision(db, revision_id)
    assert [(e.producer, e.strength, e.lifecycle) for e in revision.provenance.evidence] == [
        ("human", "confirmed", "active")]
    assert revision.provenance.evidence[0].producer_ref == ADMIN


def test_a_stale_pointer_version_is_a_conflict_not_a_silent_overwrite(db):
    _r, version = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    approve_pii_use_policy(
        db, concept_name="pep_flag", purpose="AML and sanctions screening",
        expected_pointer_version=version, actor=ADMIN)
    with pytest.raises(PolicyStoreConflict):
        approve_pii_use_policy(
            db, concept_name="pep_flag", purpose="something else entirely, third writer",
            expected_pointer_version=version, actor=ADMIN)


def test_first_write_is_claimed_with_version_zero_and_refused_if_someone_beat_you(db):
    approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    with pytest.raises(PolicyStoreConflict):
        approve_pii_use_policy(
            db, concept_name="pep_flag", purpose="a competing first declaration",
            expected_pointer_version=0, actor="other@bank")


def test_a_negative_pointer_version_is_a_validation_error_and_writes_nothing(db):
    with pytest.raises(PolicyValidationError):
        approve_pii_use_policy(
            db, concept_name="pep_flag", purpose=AML, expected_pointer_version=-1, actor=ADMIN)
    assert db.execute("SELECT count(*) FROM pii_use_policy_revision").fetchone()[0] == 0


def test_an_anonymous_write_is_refused(db):
    with pytest.raises(PolicyValidationError):
        approve_pii_use_policy(
            db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor="  ")
    assert db.execute("SELECT count(*) FROM pii_use_policy_revision").fetchone()[0] == 0


def test_a_pointer_at_a_missing_revision_raises_rather_than_serving_nothing(db):
    """Corruption is loud. The alternative — treating a dangling pointer as "no policy" — would be
    a store that silently forgets a declaration, and the forgetting would look like a refusal."""
    approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    db.execute("ALTER TABLE pii_use_policy_current DROP CONSTRAINT "
               "pii_use_policy_current_revision_id_fkey")
    db.execute("UPDATE pii_use_policy_current SET revision_id = %s", ("pup_" + "0" * 64,))
    with pytest.raises(PolicyStoreConflict):
        current_pii_use_policy(db, "pep_flag")


def test_an_edited_revision_fails_content_verification(db):
    """The tamper this hash exists to catch: a purpose (or a status) edited under the decisions that
    reference it. A flipped `revoked` -> `active` is the same edit and the same failure."""
    revision_id, _v = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    db.execute("UPDATE pii_use_policy_revision SET purpose = %s WHERE revision_id = %s",
               ("marketing to affluent customers", revision_id))
    with pytest.raises(PolicyStoreConflict):
        load_pii_use_policy_revision(db, revision_id)
    with pytest.raises(PolicyStoreConflict):
        active_pii_use_policies(db, ["pep_flag"])


# ── revocation ──────────────────────────────────────────────────────────────────────────────────


def test_revocation_is_a_new_revision_and_the_approval_stays_readable(db):
    approved, version = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    revoked, new_version = revoke_pii_use_policy(
        db, concept_name="pep_flag", expected_pointer_version=version, actor=ADMIN)

    assert revoked != approved and new_version == version + 1
    assert load_pii_use_policy_revision(db, approved).active         # still there, still readable
    revision, pointer = current_pii_use_policy(db, "pep_flag")
    assert revision.revision_id == revoked and not revision.active
    assert pointer.pointer_version == new_version
    # the withdrawn purpose rides along: what was withdrawn is what was declared
    assert revision.purpose == AML


def test_re_approving_the_same_purpose_resolves_back_to_the_original_revision(db):
    """Content-addressed identity working as designed — and the reason WHO made it current has to
    live on the pointer rather than in the revision id."""
    approved, v1 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    _revoked, v2 = revoke_pii_use_policy(
        db, concept_name="pep_flag", expected_pointer_version=v1, actor=ADMIN)
    again, v3 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=v2, actor="third@bank")

    assert again == approved
    assert v3 == 3
    assert db.execute(
        "SELECT count(*) FROM pii_use_policy_revision WHERE concept_name = 'pep_flag'"
    ).fetchone()[0] == 2
    assert active_pii_use_policies(db, ["pep_flag"]) == {"pep_flag": approved}


def test_revoking_what_was_never_declared_is_refused(db):
    with pytest.raises(PolicyValidationError) as exc:
        revoke_pii_use_policy(
            db, concept_name="pep_flag", expected_pointer_version=0, actor=ADMIN)
    assert "no data-use policy" in str(exc.value)


def test_revoking_twice_is_refused_rather_than_advancing_the_pointer_for_nothing(db):
    _a, v1 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    _r, v2 = revoke_pii_use_policy(
        db, concept_name="pep_flag", expected_pointer_version=v1, actor=ADMIN)
    with pytest.raises(PolicyValidationError):
        revoke_pii_use_policy(
            db, concept_name="pep_flag", expected_pointer_version=v2, actor=ADMIN)


# ── the gate's bulk reader ──────────────────────────────────────────────────────────────────────


def test_the_bulk_reader_returns_only_ACTIVE_current_policies(db):
    approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    device, _v = approve_pii_use_policy(
        db, concept_name="device_fingerprint", purpose="account takeover detection",
        expected_pointer_version=0, actor=ADMIN)
    revoke_pii_use_policy(db, concept_name="pep_flag", expected_pointer_version=1, actor=ADMIN)

    got = active_pii_use_policies(
        db, ["pep_flag", "device_fingerprint", "geolocation", "", None])   # type: ignore[list-item]
    assert got == {"device_fingerprint": device}


def test_the_bulk_reader_is_one_query_whatever_the_operand_count(db, monkeypatch):
    """No per-operand query storm: `_use_gate` runs on every candidate from every producer."""
    approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    calls: list[str] = []
    real = db.execute

    def counted(sql, params=None, **kwargs):
        calls.append(str(sql))
        return real(sql, params, **kwargs) if params is not None else real(sql, **kwargs)

    monkeypatch.setattr(db, "execute", counted)
    active_pii_use_policies(
        db, ["pep_flag", "device_fingerprint", "geolocation", "pii", "beneficiary_name"])
    assert len(calls) == 1, calls


def test_the_bulk_reader_answers_nothing_for_an_empty_ask_without_touching_the_database(db):
    assert active_pii_use_policies(db, []) == {}
    assert active_pii_use_policies(db, ["", "   "]) == {}


# ── the surface projection ──────────────────────────────────────────────────────────────────────


def test_the_state_listing_covers_every_licensable_concept_not_just_the_declared_ones(db):
    """A list of the rows that happen to exist would hide every concept a reviewer might approve."""
    states = pii_use_policy_states(db)
    assert tuple(s.concept_name for s in states) == policy_eligible_concepts()
    assert {s.status for s in states} == {"none"}
    assert all(s.pointer_version == 0 for s in states)


def test_the_state_listing_tells_never_declared_apart_from_revoked(db):
    """"Nobody decided" and "somebody withdrew a decision" are different facts; only the GATE
    collapses them, and it is the only thing that may."""
    approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    approve_pii_use_policy(
        db, concept_name="geolocation", purpose="impossible travel detection",
        expected_pointer_version=0, actor=ADMIN)
    revoke_pii_use_policy(db, concept_name="geolocation", expected_pointer_version=1, actor=ADMIN)

    by_name = {s.concept_name: s for s in pii_use_policy_states(db)}
    assert by_name["pep_flag"].status == "active"
    assert by_name["pep_flag"].revision.purpose == AML
    assert by_name["pep_flag"].approved_by == ADMIN
    assert by_name["pep_flag"].approved_at is not None
    assert by_name["geolocation"].status == "revoked"
    assert by_name["geolocation"].revision.purpose == "impossible travel detection"
    assert by_name["device_fingerprint"].status == "none"
    assert by_name["device_fingerprint"].revision is None


def test_a_pointer_advance_that_loses_the_race_reports_the_concept(db):
    revision = PiiUsePolicyRevisionV1(concept_name="pep_flag", purpose=AML)
    with pytest.raises(PolicyStoreConflict) as exc:
        set_current_pii_use_policy(
            db, concept_name="pep_flag", revision_id=revision.revision_id,
            expected_pointer_version=7, declared_by=ADMIN)
    assert "pep_flag" in str(exc.value)
