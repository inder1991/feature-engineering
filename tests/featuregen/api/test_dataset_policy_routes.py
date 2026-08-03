"""Release-B Task 7 — the serving/temporal policy routes + the flag matrix.

The flag law (D8) is the spine: with `FEATUREGEN_SOURCE_TEMPORAL_SELECTION` off every route 404s,
AND — the Release-B addition — the flag is FAIL-CLOSED on its `FEATUREGEN_DATASET_PROFILES`
dependency, so requesting it alone is an invalid configuration that keeps the surface hidden rather
than half-enabled. On, the PUTs take the existing platform-admin confirmer gate, carry a REQUIRED
body `expected_pointer_version` (409 on any miss), refuse a policy that would overrule a
load-bearing profile classification, and never reveal a dataset the caller cannot see.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api._helpers import AUTH, VIEWER, upload_csv

_HEADER = ("source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,"
           "additivity,unit,currency,entity\n")
BANK_CSV = _HEADER + (
    "bank,customer_master,cif_id,text,y,,customer key,,,,,,,Customer\n"
    "bank,customer_ods,cif_id,text,y,,customer key,,,,,,,Customer\n"
    "bank,customer_segment,cif_id,text,y,,customer key,,,,,,,Customer\n"
    "bank,customer_segment,effective_from,timestamp,,,valid from,,,,,,,\n"
    "bank,customer_segment,effective_to,timestamp,,,valid to,,,,,,,\n"
    "bank,customer_segment,is_current,text,,,current row flag,,,,,,,\n"
)
VAULT_CSV = _HEADER + (
    "vault,identities,eida_num,text,,,national id number,pii,,,,,,Customer\n"
)
# A MIXED-VISIBILITY table: one public column (so the table ANCHOR is visible to every catalog
# reader under the D11 derived scope) and two `pii` columns a plain caller may not see. This is the
# probe shape for the temporal GET — the caller can reach the route, and the question is what the
# POLICY BODY is allowed to tell them.
MIXED_CSV = _HEADER + (
    "mixed,segment_history,cif_id,text,y,,customer key,,,,,,,Customer\n"
    "mixed,segment_history,valid_from,timestamp,,,valid from,pii,,,,,,\n"
    "mixed,segment_history,valid_to,timestamp,,,valid to,pii,,,,,,\n"
    "mixed,segment_history,is_current,text,,,current row flag,,,,,,,\n"
)

ADMIN = {"X-User": "priya", "X-Roles": "platform-admin,platform_admin"}
ADMIN2 = {"X-User": "sam", "X-Roles": "platform-admin,platform_admin"}
PII_ADMIN = {"X-User": "priya", "X-Roles": "platform-admin,platform_admin,pii_reader"}

_CUST = "bank::public.customer_master"
_ODS = "bank::public.customer_ods"
_SEG = "bank::public.customer_segment"

_MIXED = "mixed::public.segment_history"

_SERVING = "/catalog/dataset-policies/serving/customer/population/analytical"
_TEMPORAL = "/catalog/dataset-policies/temporal/bank/public.customer_segment"
_MIXED_TEMPORAL = "/catalog/dataset-policies/temporal/mixed/public.segment_history"


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")


@pytest.fixture
def seeded(client):
    assert upload_csv(client, "bank", BANK_CSV).status_code == 200
    return client


def _temporal_body(**over) -> dict:
    body = {
        "expected_pointer_version": 0,
        "temporal_storage_model": "scd2",
        "current_selection": "current_record",
        "historical_selection": "valid_at_report_cutoff",
        "effective_from_ref": f"{_SEG}.effective_from",
        "effective_to_ref": f"{_SEG}.effective_to",
        "current_flag_ref": f"{_SEG}.is_current",
    }
    body.update(over)
    return body


# ── the flag matrix, including the fail-closed dependency ───────────────────────────────────────

@pytest.mark.parametrize("profiles,selection", [
    (None, None),      # both off — the default
    ("1", None),       # Release-A's shipped state: profiles only
    (None, "1"),       # INVALID: the dependency is unmet — fail closed, still 404
    (None, "on"),
])
def test_every_route_404s_unless_both_flags_are_set(seeded, monkeypatch, profiles, selection):
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    if profiles is not None:
        monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", profiles)
    if selection is not None:
        monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", selection)
    assert seeded.get(_SERVING, headers=AUTH).status_code == 404
    assert seeded.get(_TEMPORAL, headers=AUTH).status_code == 404
    assert seeded.put(_SERVING, headers=ADMIN, json={
        "expected_pointer_version": 0, "eligible_dataset_refs": [_CUST]}).status_code == 404
    assert seeded.put(_TEMPORAL, headers=ADMIN, json=_temporal_body()).status_code == 404


def test_the_startup_check_reports_the_invalid_combination_without_blocking_boot(monkeypatch):
    """D8 / §5.16: an invalid combination FAILS CONFIGURATION VALIDATION. It must be loud at boot
    and it must not take the service down — the running configuration is valid (the feature is off),
    so refusing to boot would turn a misconfiguration into an outage."""
    from fastapi import FastAPI

    from featuregen.api.app import _startup_flag_dependency_check

    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")
    app = FastAPI()
    _startup_flag_dependency_check(app)
    assert app.state.source_temporal_selection_valid is False
    assert app.state.source_temporal_selection_enabled is False

    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    ok = FastAPI()
    _startup_flag_dependency_check(ok)
    assert ok.state.source_temporal_selection_valid is True
    assert ok.state.source_temporal_selection_enabled is True


# ── serving policy ──────────────────────────────────────────────────────────────────────────────

def test_no_policy_is_a_normal_state_not_an_error(seeded, flag_on):
    res = seeded.get(_SERVING, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["policy"] is None
    assert body["pointer_version"] == 0        # the version a first PUT must carry
    assert body["ambiguous"] is False


def test_declare_then_read_back_a_serving_policy(seeded, flag_on):
    put = seeded.put(_SERVING, headers=ADMIN, json={
        "expected_pointer_version": 0,
        "eligible_dataset_refs": [_ODS, _CUST],
        "preferred_dataset_refs": [_CUST]})
    assert put.status_code == 200, put.text
    assert put.json()["revision_id"].startswith("dsp_")
    assert put.json()["pointer_version"] == 1

    got = seeded.get(_SERVING, headers=AUTH).json()
    assert got["policy"]["eligible_dataset_refs"] == [_CUST, _ODS]    # sorted, not as sent
    assert got["policy"]["preferred_dataset_refs"] == [_CUST]
    assert got["ambiguous"] is False
    assert got["declared_by"] == "user:priya"      # the authenticated subject, never a body field
    (evidence,) = got["policy"]["provenance"]["evidence"]
    assert (evidence["producer"], evidence["strength"]) == ("human", "confirmed")


def test_ambiguity_is_reported_rather_than_resolved_by_order(seeded, flag_on):
    """Two equally preferred candidates stay an EXPLICIT ambiguity the selector refuses on."""
    put = seeded.put(_SERVING, headers=ADMIN, json={
        "expected_pointer_version": 0,
        "eligible_dataset_refs": [_CUST, _ODS],
        "preferred_dataset_refs": [_CUST, _ODS]})
    assert put.status_code == 200, put.text
    assert put.json()["ambiguous"] is True
    assert seeded.get(_SERVING, headers=AUTH).json()["ambiguous"] is True


def test_the_cas_version_is_required_and_fails_closed(seeded, flag_on):
    missing = seeded.put(_SERVING, headers=ADMIN,
                         json={"eligible_dataset_refs": [_CUST]})
    assert missing.status_code == 422, "expected_pointer_version must be REQUIRED, never optional"

    assert seeded.put(_SERVING, headers=ADMIN, json={
        "expected_pointer_version": 0, "eligible_dataset_refs": [_CUST]}).status_code == 200
    stale = seeded.put(_SERVING, headers=ADMIN2, json={
        "expected_pointer_version": 0, "eligible_dataset_refs": [_ODS]})
    assert stale.status_code == 409, stale.text
    fresh = seeded.put(_SERVING, headers=ADMIN2, json={
        "expected_pointer_version": 1, "eligible_dataset_refs": [_ODS]})
    assert fresh.status_code == 200 and fresh.json()["pointer_version"] == 2


def test_only_a_platform_admin_may_declare_a_policy(seeded, flag_on):
    """A serving policy is an OPERATIONAL declaration — it can decide which copy answers a
    production question — so the write bar is the existing confirmer claim, not catalog:write."""
    res = seeded.put(_SERVING, headers=AUTH, json={
        "expected_pointer_version": 0, "eligible_dataset_refs": [_CUST]})
    assert res.status_code == 403, res.text
    assert seeded.get(_SERVING, headers=VIEWER).status_code == 200   # reading is catalog:read


def test_a_policy_may_not_name_a_dataset_the_author_cannot_see(seeded, flag_on):
    assert upload_csv(seeded, "vault", VAULT_CSV).status_code == 200
    hidden = "vault::public.identities"
    res = seeded.put(_SERVING, headers=ADMIN, json={
        "expected_pointer_version": 0, "eligible_dataset_refs": [_CUST, hidden]})
    assert res.status_code == 400
    assert "not a readable dataset" in res.json()["detail"]
    # Identical wording for one that never existed — hidden and missing are the same answer.
    ghost = seeded.put(_SERVING, headers=ADMIN, json={
        "expected_pointer_version": 0, "eligible_dataset_refs": ["bank::public.ghost"]})
    assert ghost.status_code == 400
    assert "not a readable dataset" in ghost.json()["detail"]


def test_a_policy_naming_a_hidden_dataset_is_not_shown_to_a_caller_who_cannot_see_it(seeded,
                                                                                     flag_on):
    """No existence oracle: a partial policy would understate what governs, and naming the hidden
    ref would leak it. The same fixture under a granting role DOES return it."""
    assert upload_csv(seeded, "vault", VAULT_CSV).status_code == 200
    hidden = "vault::public.identities"
    assert seeded.put(_SERVING, headers=PII_ADMIN, json={
        "expected_pointer_version": 0,
        "eligible_dataset_refs": [_CUST, hidden]}).status_code == 200

    unprivileged = seeded.get(_SERVING, headers=AUTH)
    assert unprivileged.status_code == 404
    assert "identities" not in unprivileged.text
    assert seeded.get(_SERVING, headers=PII_ADMIN).status_code == 200


def test_preferred_must_be_a_subset_of_eligible(seeded, flag_on):
    res = seeded.put(_SERVING, headers=ADMIN, json={
        "expected_pointer_version": 0,
        "eligible_dataset_refs": [_CUST], "preferred_dataset_refs": [_ODS]})
    assert res.status_code == 400
    assert "subset of eligible" in res.json()["detail"]


def test_an_unknown_need_role_or_purpose_is_a_named_400(seeded, flag_on):
    res = seeded.get("/catalog/dataset-policies/serving/customer/whatever/analytical",
                     headers=AUTH)
    assert res.status_code == 400
    assert "need_role must be one of" in res.json()["detail"]


# ── temporal policy ─────────────────────────────────────────────────────────────────────────────

def test_declare_then_read_back_a_temporal_policy(seeded, flag_on):
    put = seeded.put(_TEMPORAL, headers=ADMIN, json=_temporal_body())
    assert put.status_code == 200, put.text
    assert put.json()["revision_id"].startswith("dtp_")
    assert put.json()["selection_kinds"] == {"current": "current_record",
                                             "historical": "valid_at_report_cutoff"}
    got = seeded.get(_TEMPORAL, headers=AUTH).json()
    assert got["policy"]["temporal_storage_model"] == "scd2"
    assert got["policy"]["effective_from_ref"] == f"{_SEG}.effective_from"
    # The profile's own answer rides along so the editor can say WHY the model choice is free.
    assert got["load_bearing_temporal_storage_model"] is None


def test_a_historical_promise_a_dataset_cannot_keep_is_refused(seeded, flag_on):
    res = seeded.put(_TEMPORAL, headers=ADMIN, json=_temporal_body(
        temporal_storage_model="current_only", effective_from_ref=None, effective_to_ref=None,
        current_flag_ref=None))
    assert res.status_code == 400
    assert "keeps no history" in res.json()["detail"]


def test_a_policy_may_not_name_a_column_that_does_not_exist(seeded, flag_on):
    res = seeded.put(_TEMPORAL, headers=ADMIN,
                     json=_temporal_body(availability_ref=f"{_SEG}.ghost"))
    assert res.status_code == 400
    assert "not a readable column" in res.json()["detail"]


def test_a_literal_cutoff_can_never_be_authored_into_a_policy(seeded, flag_on):
    """There is no slot for a runtime value at all — the request model has no cutoff field, and a
    date smuggled into a ref field is refused as a ref."""
    from featuregen.api.routes.dataset_policies import TemporalPolicyPutRequest

    assert not (set(TemporalPolicyPutRequest.model_fields)
                & {"cutoff", "report_cutoff", "cutoff_value", "predicate_sql", "sql"})
    res = seeded.put(_TEMPORAL, headers=ADMIN,
                     json=_temporal_body(availability_ref="2026-06-30"))
    assert res.status_code == 400


def test_the_temporal_cas_version_fails_closed(seeded, flag_on):
    assert seeded.put(_TEMPORAL, headers=ADMIN, json=_temporal_body()).status_code == 200
    stale = seeded.put(_TEMPORAL, headers=ADMIN2,
                       json=_temporal_body(availability_ref=f"{_SEG}.effective_from"))
    assert stale.status_code == 409, stale.text
    fresh = seeded.put(_TEMPORAL, headers=ADMIN2, json=_temporal_body(
        expected_pointer_version=1, availability_ref=f"{_SEG}.effective_from"))
    assert fresh.status_code == 200 and fresh.json()["pointer_version"] == 2


def test_only_a_platform_admin_may_declare_a_temporal_policy(seeded, flag_on):
    assert seeded.put(_TEMPORAL, headers=AUTH, json=_temporal_body()).status_code == 403


def test_a_hidden_or_absent_dataset_is_a_404_not_an_oracle(seeded, flag_on):
    assert upload_csv(seeded, "vault", VAULT_CSV).status_code == 200
    assert seeded.get("/catalog/dataset-policies/temporal/vault/public.identities",
                      headers=AUTH).status_code == 404
    assert seeded.get("/catalog/dataset-policies/temporal/bank/public.ghost",
                      headers=AUTH).status_code == 404
    assert seeded.get("/catalog/dataset-policies/temporal/vault/public.identities",
                      headers=PII_ADMIN).status_code == 200


def test_a_temporal_policy_naming_a_hidden_column_is_withheld_WHOLE(seeded, flag_on):
    """The probe: an unprivileged caller who can see the table anchor must not read the NAMES of
    columns they may not see. A policy body is a list of column refs, so a partial redaction would
    still confirm "a column exists here that you may not see" — an existence oracle over the exact
    thing the sensitivity tag hides. The whole policy is withheld, in the serving GET's words."""
    assert upload_csv(seeded, "mixed", MIXED_CSV).status_code == 200
    # The anchor IS visible to a plain caller (cif_id is public) — the route is reachable, and with
    # no policy declared it answers normally. That is what makes the leak below reachable at all.
    before = seeded.get(_MIXED_TEMPORAL, headers=AUTH)
    assert before.status_code == 200, before.text
    assert before.json()["policy"] is None

    declared = seeded.put(_MIXED_TEMPORAL, headers=PII_ADMIN, json={
        "expected_pointer_version": 0,
        "temporal_storage_model": "scd2",
        "current_selection": "current_record",
        "historical_selection": "valid_at_report_cutoff",
        "effective_from_ref": f"{_MIXED}.valid_from",
        "effective_to_ref": f"{_MIXED}.valid_to",
        "current_flag_ref": f"{_MIXED}.is_current"})
    assert declared.status_code == 200, declared.text

    withheld = seeded.get(_MIXED_TEMPORAL, headers=AUTH)
    assert withheld.status_code == 404, withheld.text
    # The SAME words the serving GET uses for a policy it may not show, and the same words a
    # caller gets for a policy that was never declared: hidden and missing read alike.
    assert withheld.json() == {"detail": "policy not found"}
    assert "valid_from" not in withheld.text and "valid_to" not in withheld.text

    granted = seeded.get(_MIXED_TEMPORAL, headers=PII_ADMIN)
    assert granted.status_code == 200, granted.text
    assert granted.json()["policy"]["effective_from_ref"] == f"{_MIXED}.valid_from"


def test_a_temporal_policy_naming_only_visible_columns_is_shown_to_everyone(seeded, flag_on):
    """The control. Withholding is driven by what the policy NAMES, not by the table carrying a
    sensitive column somewhere: a policy over public columns of a mixed table stays readable."""
    assert upload_csv(seeded, "mixed", MIXED_CSV).status_code == 200
    assert seeded.put(_MIXED_TEMPORAL, headers=PII_ADMIN, json={
        "expected_pointer_version": 0,
        "temporal_storage_model": "scd2",
        "current_selection": "current_record",
        "historical_selection": "explicit_only",
        "current_flag_ref": f"{_MIXED}.is_current"}).status_code == 200

    for headers in (AUTH, PII_ADMIN):
        got = seeded.get(_MIXED_TEMPORAL, headers=headers)
        assert got.status_code == 200, got.text
        assert got.json()["policy"]["current_flag_ref"] == f"{_MIXED}.is_current"


def test_a_column_ref_is_not_a_dataset(seeded, flag_on):
    res = seeded.get("/catalog/dataset-policies/temporal/bank/public.customer_segment.cif_id",
                     headers=AUTH)
    assert res.status_code == 400
    assert "table-scoped" in res.json()["detail"]
