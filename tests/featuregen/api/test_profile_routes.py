"""Release-A profile Task 3 — upload part, catalog-narrative routes, asset-profile routes, flag.

The flag law (D8) is the spine of this suite: with ``FEATUREGEN_DATASET_PROFILES`` OFF every new
surface 404s, the upload part is ignored with a warning, and every existing payload is
byte-identical; ON, the narrative rides the ingest transaction atomically, the PUTs carry their
CAS anchors (pointer version IN THE BODY; aggregate ``dataset_profile_hash``), proposals are
visible-and-labeled immediately (no-blocked rule), and promotion to load-bearing still takes the
existing four-eyes confirm with a distinct subject.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api._helpers import AUTH, OWNER, VIEWER, upload_csv

_HEADER = ("source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,"
           "additivity,unit,currency,entity\n")
BANK_CSV = _HEADER + (
    "bank,orders,id,integer,y,,order key,,,,,,,Account\n"
    "bank,orders,amount,numeric,,,order amount,,,,,,,\n"
)
ADMIN2 = {"X-User": "sam", "X-Roles": "platform-admin"}
CONFIRMER = {"X-User": "priya", "X-Roles": "platform-admin"}

_PROFILE_JSON = ('{"display_name": "Bank Core", "description": "Core banking catalog.", '
                 '"business_domains": ["retail"]}')


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")


def _upload_with_profile(client, source, csv, profile_json, headers=AUTH):
    return client.post(
        "/uploads",
        data={"source": source, "catalog_profile_json": profile_json},
        files={"file": (f"{source}.csv", csv.encode(), "text/csv")},
        headers=headers)


def _pointer(conn, source):
    return conn.execute("SELECT revision_id, pointer_version FROM catalog_profile_current "
                        "WHERE catalog_source = %s", (source,)).fetchone()


# ── flag OFF: surfaces hidden, payloads byte-identical, part ignored-with-warning ────────────────


def test_flag_off_hides_every_new_surface(client, conn):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    assert client.get("/catalogs/bank/profile", headers=AUTH).status_code == 404
    assert client.put("/catalogs/bank/profile", headers=AUTH,
                      json={"expected_pointer_version": 0}).status_code == 404
    assert client.get("/catalog/asset-profiles/bank/public.orders",
                      headers=AUTH).status_code == 404
    assert client.put("/catalog/asset-profiles/bank/public.orders", headers=AUTH,
                      json={"expected_dataset_profile_hash": "x",
                            "authority_role": "derived"}).status_code == 404


def test_flag_off_catalog_listing_is_byte_identical(client):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    body = client.get("/catalogs", headers=AUTH).json()
    assert set(body["catalogs"][0]) == {"source", "tables", "columns"}   # no new keys


def test_flag_off_upload_part_is_ignored_with_no_write_and_identical_body(client, conn):
    with_part = _upload_with_profile(client, "bank", BANK_CSV, _PROFILE_JSON)
    assert with_part.status_code == 200
    assert with_part.json()["status"] == "ingested"
    assert _pointer(conn, "bank") is None                       # nothing persisted
    without_part = upload_csv(client, "bank2", BANK_CSV.replace("bank,", "bank2,"))
    assert set(with_part.json()) == set(without_part.json())    # same response shape


# ── upload part (flag ON): atomic with a successful ingest ───────────────────────────────────────


def test_upload_part_commits_narrative_with_a_successful_ingest(flag_on, client, conn):
    res = _upload_with_profile(client, "bank", BANK_CSV, _PROFILE_JSON)
    assert res.status_code == 200 and res.json()["status"] == "ingested"
    pointer = _pointer(conn, "bank")
    assert pointer is not None and pointer[1] == 1
    row = conn.execute(
        "SELECT display_name, producer, strength, ingestion_run_id "
        "FROM catalog_profile_revision WHERE revision_id = %s", (pointer[0],)).fetchone()
    assert row[0] == "Bank Core"
    assert (row[1], row[2]) == ("human", "proposed")            # authored, labeled, not confirmed
    assert row[3] == res.headers["X-Ingestion-Run-Id"]          # provenance, not identity


def test_upload_part_invalid_json_rejects_before_any_write(flag_on, client, conn):
    res = _upload_with_profile(client, "bank", BANK_CSV, '{"display_name": ')
    assert res.status_code == 400
    assert _pointer(conn, "bank") is None
    assert conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source = 'bank'"
                        ).fetchone()[0] == 0                    # the catalog was not ingested


def test_upload_part_unknown_key_rejects(flag_on, client):
    res = _upload_with_profile(client, "bank", BANK_CSV, '{"owner": "x"}')
    assert res.status_code == 400
    assert "unknown catalog profile keys" in res.json()["detail"]


def test_failed_ingest_leaves_the_pointer_untouched(flag_on, client, conn):
    empty = _HEADER   # header-only: a structural "empty upload" rejection, not an HTTP error
    res = _upload_with_profile(client, "bank", empty, _PROFILE_JSON)
    assert res.status_code == 200 and res.json()["status"] == "rejected"
    assert _pointer(conn, "bank") is None


def test_missing_profile_never_rejects_a_valid_catalog(flag_on, client):
    assert upload_csv(client, "bank", BANK_CSV).json()["status"] == "ingested"


# ── catalog narrative routes (flag ON) ───────────────────────────────────────────────────────────


def test_catalog_profile_put_get_roundtrip_with_body_cas(flag_on, client):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    first = client.put("/catalogs/bank/profile", headers=OWNER,
                       json={"expected_pointer_version": 0, "display_name": "Bank Core",
                             "business_domains": ["retail"]})
    assert first.status_code == 200 and first.json()["pointer_version"] == 1
    got = client.get("/catalogs/bank/profile", headers=AUTH).json()
    assert got["pointer_version"] == 1
    assert got["profile"]["display_name"] == "Bank Core"
    assert got["profile"]["producer"] == "human" and got["profile"]["strength"] == "proposed"
    # Stale expected version fails closed (bridge_store CAS), 409.
    stale = client.put("/catalogs/bank/profile", headers=OWNER,
                       json={"expected_pointer_version": 0, "display_name": "Other"})
    assert stale.status_code == 409
    second = client.put("/catalogs/bank/profile", headers=OWNER,
                        json={"expected_pointer_version": 1, "display_name": "Bank Core 2"})
    assert second.status_code == 200 and second.json()["pointer_version"] == 2


def test_catalog_profile_bounds_and_authz(flag_on, client):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    too_long = client.put("/catalogs/bank/profile", headers=OWNER,
                          json={"expected_pointer_version": 0, "display_name": "x" * 300})
    assert too_long.status_code == 400
    read_only = client.put("/catalogs/bank/profile", headers=VIEWER,
                           json={"expected_pointer_version": 0, "display_name": "N"})
    assert read_only.status_code == 403                         # catalog:write required
    assert client.get("/catalogs/ghost/profile", headers=AUTH).status_code == 404


def test_catalog_listing_gains_display_name_and_presence(flag_on, client):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    assert upload_csv(client, "plain", BANK_CSV.replace("bank,", "plain,")).status_code == 200
    client.put("/catalogs/bank/profile", headers=OWNER,
               json={"expected_pointer_version": 0, "display_name": "Bank Core"})
    by_source = {c["source"]: c for c in client.get("/catalogs", headers=AUTH).json()["catalogs"]}
    assert by_source["bank"]["display_name"] == "Bank Core"
    assert by_source["bank"]["has_profile"] is True
    assert by_source["plain"]["display_name"] is None
    assert by_source["plain"]["has_profile"] is False


# ── asset profile routes (flag ON) ───────────────────────────────────────────────────────────────


def test_asset_profile_get_shapes_the_effective_profile(flag_on, client):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    res = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["dataset_logical_ref"] == "bank::public.orders"
    assert body["dataset_profile_hash"]
    assert body["business_context"]["state"] == "no_evidence"
    assert body["business_context"]["unresolved_family"] == "undecided"
    # A TECHNICAL upload writes no table-level narrative (review D: only glossary table terms do)
    # — the profile says so honestly instead of pretending the connector attested one.
    assert body["description"]["display"] is None
    assert body["description"]["state"] == "no_evidence"
    assert client.get("/catalog/asset-profiles/bank/public.ghost", headers=AUTH
                      ).status_code == 404
    col = client.get("/catalog/asset-profiles/bank/public.orders.amount", headers=AUTH)
    assert col.status_code == 400                               # table-scoped surface


def test_asset_profile_put_proposes_and_four_eyes_promotes(flag_on, client, conn):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    current = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH).json()

    put = client.put("/catalog/asset-profiles/bank/public.orders", headers=OWNER,
                     json={"expected_dataset_profile_hash": current["dataset_profile_hash"],
                           "business_context": "Branch-booked retail orders.",
                           "authority_role": "SYSTEM_OF_RECORD",   # normalized server-side
                           "temporal_storage_model": "scd2"})
    assert put.status_code == 200, put.text
    body = put.json()
    assert sorted(body["written"]) == ["authority_role", "business_context",
                                       "temporal_storage_model"]
    assert body["dataset_profile_hash"] != current["dataset_profile_hash"]
    profile = body["profile"]
    # Proposed values are VISIBLE and labeled — usable, never failure-styled (no-blocked rule).
    assert profile["authority_role"]["display"]["value"] == "system_of_record"
    assert profile["authority_role"]["display"]["producer"] == "human"
    assert profile["authority_role"]["display"]["strength"] == "proposed"
    assert profile["authority_role"]["load_bearing"] is None
    assert profile["business_context"]["display"]["value"] == "Branch-booked retail orders."
    # The display projection LANDED on the table node (zero-row projection would have 500'd).
    row = conn.execute(
        "SELECT authority_role, temporal_storage_model FROM graph_node "
        "WHERE catalog_source = 'bank' AND object_ref = 'public.orders' AND kind = 'table'"
    ).fetchone()
    assert row == ("system_of_record", "scd2")

    # Promotion stays the EXISTING four-eyes confirm (distinct subject) on the decision route.
    confirm = client.post(
        "/catalog/assets/bank/public.orders/fields/authority_role/decisions", headers=CONFIRMER,
        json={"action": "confirm_override", "replacement_value": "system_of_record",
              "idempotency_key": "ar-c1", "expected_latest_decision_id": None,
              "expected_evidence_set_hash": "stale", "expected_policy_version": "stale"})
    assert confirm.status_code == 409   # CAS is enforced; fetch the real anchor
    anchor = _field_cas(conn, "bank::public.orders", "authority_role")
    confirm = client.post(
        "/catalog/assets/bank/public.orders/fields/authority_role/decisions", headers=CONFIRMER,
        json={"action": "confirm_override", "replacement_value": "system_of_record",
              "idempotency_key": "ar-c2", **anchor})
    assert confirm.status_code == 200, confirm.text
    promoted = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH).json()
    assert promoted["authority_role"]["state"] == "load_bearing"


def _field_cas(conn, logical_ref, field):
    from featuregen.overlay.field_decision import read_field_decisions
    from featuregen.overlay.field_evidence import read_active_field_evidence
    from featuregen.overlay.upload.field_resolution import (
        FIELD_POLICY_VERSION,
        _evidence_set_hash,
    )
    decisions = read_field_decisions(conn, logical_ref, field)
    return {
        "expected_latest_decision_id":
            decisions[-1].decision_event_id if decisions else None,
        "expected_evidence_set_hash":
            _evidence_set_hash(read_active_field_evidence(conn, logical_ref, field)),
        "expected_policy_version": FIELD_POLICY_VERSION,
    }


def test_asset_profile_put_cas_and_validation(flag_on, client):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    current = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH).json()
    stale = client.put("/catalog/asset-profiles/bank/public.orders", headers=OWNER,
                       json={"expected_dataset_profile_hash": "stale-hash",
                             "authority_role": "derived"})
    assert stale.status_code == 409
    bad_enum = client.put("/catalog/asset-profiles/bank/public.orders", headers=OWNER,
                          json={"expected_dataset_profile_hash":
                                current["dataset_profile_hash"],
                                "temporal_storage_model": "bitemporal"})
    assert bad_enum.status_code == 400
    empty = client.put("/catalog/asset-profiles/bank/public.orders", headers=OWNER,
                       json={"expected_dataset_profile_hash": current["dataset_profile_hash"]})
    assert empty.status_code == 400
    read_only = client.put("/catalog/asset-profiles/bank/public.orders", headers=VIEWER,
                           json={"expected_dataset_profile_hash":
                                 current["dataset_profile_hash"],
                                 "authority_role": "derived"})
    assert read_only.status_code == 403


OWNER2 = {"X-User": "dana", "X-Roles": "data_owner"}


def _evidence_rows(conn, field):
    return conn.execute(
        "SELECT proposed_value, lifecycle, producer_ref FROM field_evidence "
        "WHERE logical_ref = 'bank::public.orders' AND field_name = %s "
        "ORDER BY created_at, evidence_id", (field,)).fetchall()


def _graph_authority_role(conn):
    return conn.execute(
        "SELECT authority_role FROM graph_node WHERE catalog_source = 'bank' "
        "AND object_ref = 'public.orders' AND kind = 'table'").fetchone()[0]


def test_same_subject_re_put_supersedes_their_own_prior_proposal(flag_on, client, conn):
    """F1 (review blocker): a data_owner correcting their OWN pending proposal must not tie with
    it. The prior ACTIVE HUMAN/PROPOSED row is retired to `superseded` in the same transaction,
    the NEW value displays, and the graph projection is never cleared by the correction."""
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    current = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH).json()
    put1 = client.put("/catalog/asset-profiles/bank/public.orders", headers=OWNER,
                      json={"expected_dataset_profile_hash": current["dataset_profile_hash"],
                            "authority_role": "derived"})
    assert put1.status_code == 200, put1.text
    put2 = client.put("/catalog/asset-profiles/bank/public.orders", headers=OWNER,
                      json={"expected_dataset_profile_hash":
                            put1.json()["dataset_profile_hash"],
                            "authority_role": "system_of_record"})
    assert put2.status_code == 200, put2.text
    f = put2.json()["profile"]["authority_role"]
    assert f["display"] is not None and f["display"]["value"] == "system_of_record"
    assert f["state"] == "display_only"
    rows = _evidence_rows(conn, "authority_role")
    assert [r[0] for r in rows if r[1] == "active"] == ["system_of_record"]
    assert [r[1] for r in rows if r[0] == "derived"] == ["superseded"]
    # The graph projection shows the corrected value — never cleared by the self-correction.
    assert _graph_authority_role(conn) == "system_of_record"


def test_different_subject_proposal_competes_without_retiring_anyone(flag_on, client, conn):
    """A DIFFERENT subject proposing a different value is a legitimate competing proposal: both
    rows stay ACTIVE, and the unreviewed tie reports the honest `undecided:pending_review`
    (F2) — never a failure-shaped conflict, never anyone's row retired."""
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    current = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH).json()
    put1 = client.put("/catalog/asset-profiles/bank/public.orders", headers=OWNER,
                      json={"expected_dataset_profile_hash": current["dataset_profile_hash"],
                            "authority_role": "derived"})
    assert put1.status_code == 200, put1.text
    put2 = client.put("/catalog/asset-profiles/bank/public.orders", headers=OWNER2,
                      json={"expected_dataset_profile_hash":
                            put1.json()["dataset_profile_hash"],
                            "authority_role": "system_of_record"})
    assert put2.status_code == 200, put2.text
    rows = _evidence_rows(conn, "authority_role")
    assert sorted((r[0], r[2]) for r in rows if r[1] == "active") == [
        ("derived", "user:o"), ("system_of_record", "user:dana")]
    f = put2.json()["profile"]["authority_role"]
    assert f["display"] is None
    assert f["state"] == "undecided"
    assert f["unresolved_reason"] == "pending_review"
    assert f["unresolved_family"] == "undecided"


def _put(client, headers, expected_hash, **fields):
    return client.put("/catalog/asset-profiles/bank/public.orders", headers=headers,
                      json={"expected_dataset_profile_hash": expected_hash, **fields})


def test_a_user_idempotency_key_can_never_collide_with_a_profile_put(flag_on, client, conn):
    """F10: the PUT used to stamp HUMAN evidence with ``producer_item_ref="asset-profile-put:
    {field}"`` — inside the idempotency namespace ``field_correction``'s replay probe owns. A user
    who happened to send that exact ``idempotency_key`` got a 409 ("reused with different
    parameters") for a command they had never issued. The two key spaces are now disjoint, so the
    reviewer's collision probe is an ordinary successful proposal."""
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    current = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH).json()
    assert _put(client, OWNER, current["dataset_profile_hash"],
                authority_role="derived").status_code == 200
    command = {"action": "propose_override", "replacement_value": "mastered_view",
               "idempotency_key": "asset-profile-put:authority_role",
               **_field_cas(conn, "bank::public.orders", "authority_role")}
    collide = client.post(
        "/catalog/assets/bank/public.orders/fields/authority_role/decisions", headers=CONFIRMER,
        json=command)
    assert collide.status_code == 200, collide.text
    assert collide.json()["outcome"] == "proposed"
    # Narrowing the probe must not have DISABLED it: the command's own replay still replays.
    replay = client.post(
        "/catalog/assets/bank/public.orders/fields/authority_role/decisions", headers=CONFIRMER,
        json=command)
    assert replay.status_code == 200, replay.text
    assert replay.json()["outcome"] == "replayed" and replay.json()["replayed"] is True


def test_repeat_puts_never_stack_rows_under_one_key(flag_on, client, conn):
    """F10: every PUT appended under the SAME ``asset-profile-put:{field}`` key, so "one key, one
    write" — the discipline the replay probe depends on — did not hold on this surface. The key is
    now derived per (subject, field, VALUE): distinct values get distinct keys, and re-asserting a
    value the author themselves superseded revives that exact row instead of stacking a second."""
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    h = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH
                   ).json()["dataset_profile_hash"]
    for value in ("derived", "system_of_record", "derived"):
        res = _put(client, OWNER, h, authority_role=value)
        assert res.status_code == 200, res.text
        h = res.json()["dataset_profile_hash"]
    keys = conn.execute(
        "SELECT producer_item_ref FROM field_evidence WHERE logical_ref = 'bank::public.orders' "
        "AND field_name = 'authority_role' AND producer = 'human'").fetchall()
    assert len(keys) == len({k[0] for k in keys}), f"rows stacked under one key: {keys}"
    assert all(k[0] != "asset-profile-put:authority_role" for k in keys)
    # ...and the surface still behaves: exactly the last value is live and displayed.
    assert [r[0] for r in _evidence_rows(conn, "authority_role") if r[1] == "active"] == ["derived"]
    assert _graph_authority_role(conn) == "derived"


def test_set_advisory_flows_through_the_decision_route(flag_on, client):
    """A confirmer curates business_context in one step (advisory-only; RECOMMENDATION ceiling)."""
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    res = client.post(
        "/catalog/assets/bank/public.orders/fields/business_context/decisions", headers=CONFIRMER,
        json={"action": "set_advisory", "replacement_value": "Curated context.",
              "idempotency_key": "sa-1", "expected_latest_decision_id": None,
              "expected_evidence_set_hash": _empty_set_hash(),
              "expected_policy_version": "upload-field-policy-v1"})
    assert res.status_code == 200, res.text
    assert res.json()["outcome"] == "confirmed"
    profile = client.get("/catalog/asset-profiles/bank/public.orders", headers=AUTH).json()
    assert profile["business_context"]["display"]["value"] == "Curated context."
    assert profile["business_context"]["load_bearing"] is None


def _empty_set_hash():
    from featuregen.overlay.upload.field_resolution import _evidence_set_hash
    return _evidence_set_hash([])
