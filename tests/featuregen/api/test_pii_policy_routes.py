"""The PII allow-policy routes (D14; A.34).

Four things are asserted, and the shape of each is the argument:

* **GET is read-scoped on purpose.** The refusal these policies answer is shown to any catalog
  reader; a governance-only read gate would point that reader at a page they cannot open. The
  listing carries registry concepts and governance decisions — never a column, a catalog or a
  value — so opening it exposes nothing read-scoped.
* **The writes are platform-admin, and they are SINGLE actions.** No propose/confirm pair, by
  explicit user decision (D14). The test pins the deviation so re-adding four-eyes has to be a
  choice somebody makes on purpose.
* **A protected characteristic is refused with words no policy can lift** — the route must not
  offer to approve what `_use_gate` calls structurally unsuitable, or the governance screen and the
  gate would disagree about the same column.
* **CAS is required and fails closed**, and revocation is a new revision rather than a delete.
"""
from __future__ import annotations

from tests.featuregen.api._helpers import VIEWER, upload_csv

_HEADER = ("source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,"
           "additivity,unit,currency,entity\n")
BANK_CSV = _HEADER + (
    "bank,cust,cif_id,text,y,,customer key,,,,,,,Customer\n"
    "bank,cust,pep_ind,text,,,pep marker,pii,,,,,,\n"
)

ADMIN = {"X-User": "priya", "X-Roles": "platform-admin,platform_admin"}
ADMIN2 = {"X-User": "sam", "X-Roles": "platform-admin,platform_admin"}
#: A caller with catalog:read and NO admin claim — the reader the refusal is written for.
READER = {"X-User": "ravi", "X-Roles": "catalog_viewer"}

_BASE = "/governance/data-use-policies"
_PEP = f"{_BASE}/pep_flag"
AML = "AML transaction monitoring"


def _seeded(client):
    assert upload_csv(client, "bank", BANK_CSV, headers=ADMIN).status_code == 200
    return client


def _by_name(body: dict) -> dict:
    return {c["concept_name"]: c for c in body["concepts"]}


# ── the listing ─────────────────────────────────────────────────────────────────────────────────


def test_the_listing_covers_every_licensable_concept_and_frames_absence_as_undecided(client):
    res = client.get(_BASE, headers=ADMIN)
    assert res.status_code == 200, res.text
    body = res.json()
    concepts = _by_name(body)

    assert {"pep_flag", "device_fingerprint", "geolocation", "pii",
            "beneficiary_name"} <= set(concepts)
    # NO-BLOCKED FRAMING: an undeclared concept is `none` with a 0 pointer — the version a first
    # approve must carry — never an error, a gap or a failure.
    assert concepts["pep_flag"]["status"] == "none"
    assert concepts["pep_flag"]["pointer_version"] == 0
    assert concepts["pep_flag"]["purpose"] is None
    # the registry's own words ride along so the approver reads what they are licensing
    assert concepts["pep_flag"]["description"]
    assert body["purpose_bounds"]["max"] >= body["purpose_bounds"]["min"] > 0


def test_a_protected_characteristic_is_not_offered_in_the_listing_at_all(client):
    """It is not a thing anybody may approve, so it must not appear as an un-approved row — a row
    reading "not approved for feature use" would imply approving it is the missing step."""
    concepts = _by_name(client.get(_BASE, headers=ADMIN).json())
    assert not ({"protected_attribute", "special_category", "vulnerability_flag"} & set(concepts))


def test_any_catalog_reader_may_SEE_the_policy_state(client):
    """Transparency is the point: the refusal names a policy, so the person it is shown to has to
    be able to check whether one exists and who approved it."""
    assert client.get(_BASE, headers=READER).status_code == 200
    assert client.get(_BASE, headers=VIEWER).status_code == 200


def test_an_unauthenticated_caller_may_not(client):
    assert client.get(_BASE).status_code in (401, 403)


# ── approving ───────────────────────────────────────────────────────────────────────────────────


def test_approve_then_read_back(client):
    res = client.post(f"{_PEP}/approve", headers=ADMIN,
                      json={"expected_pointer_version": 0, "purpose": AML})
    assert res.status_code == 200, res.text
    assert res.json()["revision_id"].startswith("pup_")
    assert res.json()["pointer_version"] == 1
    assert res.json()["status"] == "active"

    state = _by_name(client.get(_BASE, headers=READER).json())["pep_flag"]
    assert state["status"] == "active"
    assert state["purpose"] == AML
    assert state["approved_by"] == "user:priya"     # the authenticated subject, never a body field
    assert state["declared_by"] == "user:priya"
    assert state["approved_at"] and state["updated_at"]


def test_only_a_platform_admin_may_approve_or_revoke(client):
    for headers in (READER, VIEWER):
        assert client.post(f"{_PEP}/approve", headers=headers,
                           json={"expected_pointer_version": 0, "purpose": AML}
                           ).status_code == 403
        assert client.post(f"{_PEP}/revoke", headers=headers,
                           json={"expected_pointer_version": 0}).status_code == 403


def test_approval_is_a_SINGLE_action_with_no_confirm_step(client):
    """D14, pinned as a route shape. The policy is ACTIVE the moment approve returns — there is no
    second endpoint to call and no pending state to wait in. Re-adding four-eyes has to change this
    test, which is exactly the intent: the deviation was an explicit user decision."""
    approve = client.post(f"{_PEP}/approve", headers=ADMIN,
                          json={"expected_pointer_version": 0, "purpose": AML})
    assert approve.json()["status"] == "active"
    assert _by_name(client.get(_BASE, headers=ADMIN).json())["pep_flag"]["status"] == "active"

    paths = {p for p in client.get("/openapi.json").json()["paths"] if p.startswith(_BASE)}
    assert paths == {_BASE, f"{_BASE}/{{concept_name}}/approve",
                     f"{_BASE}/{{concept_name}}/revoke"}, paths


def test_a_protected_characteristic_is_refused_with_wording_no_policy_can_lift(client):
    res = client.post(f"{_BASE}/protected_attribute/approve", headers=ADMIN,
                      json={"expected_pointer_version": 0, "purpose": "credit scoring"})
    assert res.status_code == 400
    assert "NO data-use policy can allow it" in res.json()["detail"]


def test_a_non_personal_data_concept_is_refused_as_needing_no_policy(client):
    res = client.post(f"{_BASE}/segment/approve", headers=ADMIN,
                      json={"expected_pointer_version": 0, "purpose": AML})
    assert res.status_code == 400
    assert "is not personal data" in res.json()["detail"]


def test_the_purpose_is_required_and_bounded(client):
    """ONE bound, BOTH ends, one status code and one readable sentence.

    422 is reserved for a request there is nothing to normalize in — `purpose` absent, or empty.
    Everything the bound itself refuses is a 400 that says what a purpose has to be, because the
    caller is a governance surface reporting the server's own words to a human. The 422 that used
    to answer an over-long purpose (a pydantic `max_length` on the RAW body) is DELIBERATELY
    retired: it answered one end of one bound in a different voice from the other, and it measured
    the text before whitespace collapse, so a purpose whose only excess was spaces was refused for
    a length its declaration does not have."""
    assert client.post(f"{_PEP}/approve", headers=ADMIN,
                       json={"expected_pointer_version": 0}).status_code == 422
    assert client.post(f"{_PEP}/approve", headers=ADMIN,
                       json={"expected_pointer_version": 0, "purpose": ""}).status_code == 422
    short = client.post(f"{_PEP}/approve", headers=ADMIN,
                        json={"expected_pointer_version": 0, "purpose": "AML"})
    assert short.status_code == 400
    assert "at least" in short.json()["detail"]

    long = client.post(f"{_PEP}/approve", headers=ADMIN,
                       json={"expected_pointer_version": 0, "purpose": "x" * 5000})
    assert long.status_code == 400, long.text
    assert "at most" in long.json()["detail"]


def test_the_bound_is_measured_on_the_NORMALIZED_purpose_not_the_raw_body(client):
    """Whitespace is collapsed before the length is taken — it is collapsed before the content hash
    is taken too, so a purpose padded with spaces is the SAME declaration, not an over-long one."""
    padded = "  AML   transaction    monitoring  " + " " * 400
    res = client.post(f"{_PEP}/approve", headers=ADMIN,
                      json={"expected_pointer_version": 0, "purpose": padded})
    assert res.status_code == 200, res.text
    assert _by_name(client.get(_BASE, headers=ADMIN).json())["pep_flag"]["purpose"] == AML


def test_the_cas_version_is_required_and_fails_closed(client):
    assert client.post(f"{_PEP}/approve", headers=ADMIN,
                       json={"purpose": AML}).status_code == 422
    assert client.post(f"{_PEP}/approve", headers=ADMIN,
                       json={"expected_pointer_version": 0, "purpose": AML}).status_code == 200
    stale = client.post(f"{_PEP}/approve", headers=ADMIN2,
                        json={"expected_pointer_version": 0, "purpose": "marketing analytics"})
    assert stale.status_code == 409, stale.text
    # nothing was overwritten
    assert _by_name(client.get(_BASE, headers=ADMIN).json())["pep_flag"]["purpose"] == AML


# ── revoking ────────────────────────────────────────────────────────────────────────────────────


def test_revocation_is_a_new_revision_and_the_state_says_revoked_not_absent(client):
    approve = client.post(f"{_PEP}/approve", headers=ADMIN,
                          json={"expected_pointer_version": 0, "purpose": AML}).json()
    res = client.post(f"{_PEP}/revoke", headers=ADMIN2,
                      json={"expected_pointer_version": approve["pointer_version"]})
    assert res.status_code == 200, res.text
    assert res.json()["revision_id"] != approve["revision_id"]
    assert res.json()["pointer_version"] == 2

    state = _by_name(client.get(_BASE, headers=READER).json())["pep_flag"]
    # `revoked`, NOT `none`: somebody withdrew a specific declaration, and that is a different fact
    # from nobody ever having made one.
    assert state["status"] == "revoked"
    assert state["purpose"] == AML          # what was withdrawn is what was declared
    assert state["declared_by"] == "user:sam"


def test_revoking_what_was_never_declared_is_a_400_not_a_500(client):
    res = client.post(f"{_BASE}/geolocation/revoke", headers=ADMIN,
                      json={"expected_pointer_version": 0})
    assert res.status_code == 400
    assert "no data-use policy" in res.json()["detail"]


def test_an_unknown_concept_is_refused_before_anything_is_written(client):
    res = client.post(f"{_BASE}/not_a_concept/approve", headers=ADMIN,
                      json={"expected_pointer_version": 0, "purpose": AML})
    assert res.status_code == 400
    assert "not a concept in the registry" in res.json()["detail"]
    assert "not_a_concept" not in _by_name(client.get(_BASE, headers=ADMIN).json())


def test_the_surface_needs_no_catalog_to_exist_but_survives_one(client):
    """The policy is about a CONCEPT, so it is declarable before any catalog carries the concept —
    and the answer does not change when one does."""
    before = _by_name(client.get(_BASE, headers=ADMIN).json())["pep_flag"]["status"]
    _seeded(client)
    after = _by_name(client.get(_BASE, headers=ADMIN).json())["pep_flag"]["status"]
    assert before == after == "none"
    assert client.post(f"{_PEP}/approve", headers=ADMIN,
                       json={"expected_pointer_version": 0, "purpose": AML}).status_code == 200
