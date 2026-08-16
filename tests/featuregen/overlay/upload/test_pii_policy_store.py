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
    resolve_policy_provenance,
    revoke_pii_use_policy,
    set_current_pii_use_policy,
)
from featuregen.overlay.upload.source_selection import (
    PolicyStoreConflict,
    PolicyValidationError,
)

ADMIN = "admin@bank"
AML = "AML transaction monitoring"

#: The composite FK migration 1056 puts on the pointer. Dropping it is how a test reaches the
#: corrupt shapes the database now refuses outright — the probes below have to construct a state no
#: supported write path can produce, and saying so out loud is the point of naming the constraint.
_POINTER_FK = "pii_use_policy_current_concept_revision_fk"


def _unbolt_the_pointer_fk(db) -> None:
    db.execute(f"ALTER TABLE pii_use_policy_current DROP CONSTRAINT {_POINTER_FK}")


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
    _unbolt_the_pointer_fk(db)
    db.execute("UPDATE pii_use_policy_current SET revision_id = %s", ("pup_" + "0" * 64,))
    with pytest.raises(PolicyStoreConflict):
        current_pii_use_policy(db, "pep_flag")
    # and the GATE's reader agrees — a dangling pointer used to drop out of the bulk read silently,
    # which meant one surface shouted about corruption while the other quietly said "no policy"
    with pytest.raises(PolicyStoreConflict):
        active_pii_use_policies(db, ["pep_flag"])


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


# ── a pointer cannot license across concepts (review F1) ────────────────────────────────────────
#
# THE PROBE, in the reviewer's own shape: approve concept A, approve concept B, revoke A — then aim
# A's pointer at B's still-ACTIVE revision. Before the fix the gate's bulk reader joined on
# `revision_id` alone and keyed its answer off the REVISION's concept, so the corrupt pointer
# licensed B a second time while A — the concept somebody deliberately withdrew — went on being
# refused, and nothing anywhere said a word. Three layers refuse it now and all three are driven.


def _cross_concept_probe(db) -> tuple[str, str]:
    """Approve `pep_flag` and `geolocation`, revoke `pep_flag`. Returns (pep_rev, geo_rev)."""
    pep, v1 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    geo, _v = approve_pii_use_policy(
        db, concept_name="geolocation", purpose="impossible travel detection",
        expected_pointer_version=0, actor=ADMIN)
    revoke_pii_use_policy(db, concept_name="pep_flag", expected_pointer_version=v1, actor=ADMIN)
    return pep, geo


def test_the_STORE_refuses_to_point_one_concept_at_another_concepts_revision(db):
    """Layer 1: the write path names what is wrong, in words a reviewer can act on."""
    _pep, geo = _cross_concept_probe(db)
    with pytest.raises(PolicyValidationError) as exc:
        set_current_pii_use_policy(db, concept_name="pep_flag", revision_id=geo,
                                   expected_pointer_version=2, declared_by=ADMIN)
    assert "geolocation" in str(exc.value) and "pep_flag" in str(exc.value)
    # nothing moved: pep_flag is still revoked and still unlicensed
    assert active_pii_use_policies(db, ["pep_flag", "geolocation"]) == {"geolocation": geo}


def test_the_DATABASE_refuses_the_same_shape_even_with_the_store_out_of_the_way(db):
    """Layer 2: the composite FK. The store guard and the reader guard are both code somebody can
    later 'simplify'; this one is not, which is why it exists as well as them."""
    _pep, geo = _cross_concept_probe(db)
    with pytest.raises(Exception) as exc:   # psycopg ForeignKeyViolation, driver-typed
        db.execute("UPDATE pii_use_policy_current SET revision_id = %s "
                   "WHERE concept_name = 'pep_flag'", (geo,))
    assert "foreign key" in str(exc.value).lower() or _POINTER_FK in str(exc.value)


def test_BOTH_surfaces_refuse_the_corrupt_pointer_loudly_and_neither_licenses_it(db):
    """Layer 3, and the property that matters most: the governance listing and the gate's reader
    give the SAME verdict on the same corrupt record.

    A silent drop in the bulk reader would be safe-but-divergent — the panel would raise while the
    feature flow quietly said "no policy", and nobody reading either could tell which surface was
    describing reality. Both refuse, and the refusal names the concept."""
    _pep, geo = _cross_concept_probe(db)
    _unbolt_the_pointer_fk(db)                    # the shape no supported write path can produce
    db.execute("UPDATE pii_use_policy_current SET revision_id = %s "
               "WHERE concept_name = 'pep_flag'", (geo,))

    with pytest.raises(PolicyStoreConflict) as gate:
        active_pii_use_policies(db, ["pep_flag", "geolocation"])
    assert "pep_flag" in str(gate.value)
    with pytest.raises(PolicyStoreConflict):
        pii_use_policy_states(db)
    with pytest.raises(PolicyStoreConflict):
        current_pii_use_policy(db, "pep_flag")


# ── the approver record is tamper-evident (review F3) ───────────────────────────────────────────


def test_a_FORGED_approver_is_caught_on_every_read_path(db):
    """The reviewer's forge probe. `approved_by` is outside the content hash BY DESIGN (that is what
    makes re-approval resolve back to the original revision id), which left the one field the
    single-approver deviation rests on rewritable by a plain UPDATE. The attestation closes it
    without touching identity."""
    revision_id, _v = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    db.execute("UPDATE pii_use_policy_revision SET approved_by = 'someone-else@bank' "
               "WHERE revision_id = %s", (revision_id,))

    for read in (lambda: load_pii_use_policy_revision(db, revision_id),
                 lambda: current_pii_use_policy(db, "pep_flag"),
                 lambda: pii_use_policy_states(db),
                 lambda: active_pii_use_policies(db, ["pep_flag"])):
        with pytest.raises(PolicyStoreConflict) as exc:
            read()
        assert "APPROVER verification" in str(exc.value)


def test_a_FORGED_declarer_on_the_POINTER_is_caught_too(db):
    """"Who approved this content" and "who made it current" are two records and both are the
    control. Backdating `pointer_version` into the seal is what stops an older, legitimately
    attested declarer being replayed onto a later pointer state."""
    approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    db.execute("UPDATE pii_use_policy_current SET declared_by = 'someone-else@bank' "
               "WHERE concept_name = 'pep_flag'")

    for read in (lambda: current_pii_use_policy(db, "pep_flag"),
                 lambda: pii_use_policy_states(db),
                 lambda: active_pii_use_policies(db, ["pep_flag"])):
        with pytest.raises(PolicyStoreConflict) as exc:
            read()
        assert "DECLARER verification" in str(exc.value)


def test_a_REPLAYED_pointer_version_does_not_rescue_a_forged_declarer(db):
    """The seal covers the version, so a v1 attestation cannot be pasted onto a v2 pointer."""
    _a, v1 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    first = db.execute("SELECT attestation_hash FROM pii_use_policy_current "
                       "WHERE concept_name = 'pep_flag'").fetchone()[0]
    revoke_pii_use_policy(db, concept_name="pep_flag", expected_pointer_version=v1, actor="sam")
    db.execute("UPDATE pii_use_policy_current SET declared_by = %s, attestation_hash = %s "
               "WHERE concept_name = 'pep_flag'", (ADMIN, first))
    with pytest.raises(PolicyStoreConflict):
        current_pii_use_policy(db, "pep_flag")


def test_the_attestation_does_NOT_move_content_identity(db):
    """The property the whole fix had to preserve: approve -> revoke -> re-approve still resolves to
    the ORIGINAL revision id. Tamper-evidence rides beside identity, never inside it."""
    approved, v1 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    _revoked, v2 = revoke_pii_use_policy(
        db, concept_name="pep_flag", expected_pointer_version=v1, actor=ADMIN)
    again, v3 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=v2, actor="third@bank")

    assert again == approved and v3 == 3
    # the FIRST approver's attestation survives the re-approval untouched — ON CONFLICT DO NOTHING
    # means the revision row (and its seal) is the one the first declaration wrote
    assert load_pii_use_policy_revision(db, approved) is not None
    assert db.execute("SELECT approved_by FROM pii_use_policy_revision WHERE revision_id = %s",
                      (approved,)).fetchone() == (ADMIN,)
    # ...while the POINTER records the third person, verifiably
    _revision, pointer = current_pii_use_policy(db, "pep_flag")
    assert pointer.declared_by == "third@bank"


def test_an_unknown_stored_status_is_store_corruption_not_a_bad_request(db):
    """The 1056 CHECK forbids it, so a status this store has no meaning for can only have arrived by
    a route nothing supports — and the only safe reading of it is to refuse the read."""
    from featuregen.overlay.upload.pii_policy_store import _verified

    revision_id, _v = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    row = db.execute(
        "SELECT concept_name, purpose, provenance, content_hash, approved_by, approved_at, "
        "attestation_hash FROM pii_use_policy_revision WHERE revision_id = %s",
        (revision_id,)).fetchone()
    with pytest.raises(PolicyStoreConflict) as exc:
        _verified(row[0], row[1], "pending", row[2], row[3], revision_id, row[4], row[5], row[6])
    assert "unknown status" in str(exc.value)


# ── resolving a contract's recorded revision ids (review F11) ───────────────────────────────────


def test_resolve_policy_provenance_separates_the_revisions_STATUS_from_its_CURRENCY(db):
    """The convenience that stops `.active` being read as "still in force". An `active` revision that
    is no longer current is the NORMAL outcome of approve -> revoke -> re-approve, so a governed
    contract can truthfully name an `active` revision that licenses nothing right now."""
    approved, v1 = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose=AML, expected_pointer_version=0, actor=ADMIN)
    revoked, _v2 = revoke_pii_use_policy(
        db, concept_name="pep_flag", expected_pointer_version=v1, actor=ADMIN)

    by_id = {p.revision_id: p for p in resolve_policy_provenance(db, [approved, revoked])}
    assert by_id[approved].status == "active"        # its OWN declaration, forever
    assert by_id[approved].is_current is False       # ...and not what the concept says today
    assert by_id[approved].current_revision_id == revoked
    assert by_id[approved].approved_by == ADMIN
    assert by_id[revoked].status == "revoked" and by_id[revoked].is_current is True
    assert by_id[approved].concept_name == "pep_flag"
    assert resolve_policy_provenance(db, []) == ()


def test_resolve_policy_provenance_raises_on_an_id_the_store_cannot_produce(db):
    """Revisions are immutable and are never deleted, so an id a governed artifact recorded and the
    store cannot produce is corruption, not an empty answer."""
    with pytest.raises(PolicyStoreConflict):
        resolve_policy_provenance(db, ["pup_" + "7" * 64])


def test_a_pointer_advance_that_loses_the_race_reports_the_concept(db):
    revision = PiiUsePolicyRevisionV1(concept_name="pep_flag", purpose=AML)
    with pytest.raises(PolicyStoreConflict) as exc:
        set_current_pii_use_policy(
            db, concept_name="pep_flag", revision_id=revision.revision_id,
            expected_pointer_version=7, declared_by=ADMIN)
    assert "pep_flag" in str(exc.value)


def test_a_forged_producer_ref_in_the_provenance_json_is_caught_on_read(db):
    """Review residual R2: the provenance JSON duplicates the actor as producer_ref, outside the
    content hash. The revision attestation now seals the whole provenance payload, so the
    duplicate can no longer silently disagree with the sealed approved_by."""
    import pytest

    from featuregen.overlay.upload.pii_policy_store import PolicyStoreConflict
    revision_id, _version = approve_pii_use_policy(
        db, concept_name="pep_flag", purpose="AML screening exposure features",
        expected_pointer_version=0, actor="alice@bank")
    db.execute(
        "UPDATE pii_use_policy_revision "
        "SET provenance = jsonb_set(provenance, '{evidence,0,producer_ref}', '\"forged@bank\"') "
        "WHERE revision_id = %s", (revision_id,))
    with pytest.raises(PolicyStoreConflict):
        active_pii_use_policies(db, ["pep_flag"])
