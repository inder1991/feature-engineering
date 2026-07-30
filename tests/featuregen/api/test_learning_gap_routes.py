"""`GET /learning/gaps` — the reader the learning loop was missing.

Release 3 required recording learning evidence rather than deferring it to Release 5, "otherwise the
first working question's evidence is discarded". The recorder was built and wired, and then nothing
read it: `open_gaps` had callers only in tests, so a refused analysis stored a perfectly good
actionable gap where no human would ever encounter it.

That is the same shape as the four inert mechanisms this programme has already found — `record_gap`
with no producer, `derive_bridge_candidates` with no caller, the candidate ledger with no reader,
`_entity_candidates` gated on a column nothing populated. This closes the one that was created most
recently, and by the same hand that criticised the others.

**Gated on `catalog:read`, not `governance:confirm`.** The gap queue is a work backlog, not a
privileged secret: it says which business decision is blocking which question. `governance:confirm`
is held by `platform_admin` alone (`identity/permissions.py:57`), so gating on it would hide the
queue from the feature engineers and data owners who would act on it — the same defect P4's
suggestions route shipped with, where the default `data_owner` session simply got a 403.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.data_agent.learning import (
    AnalysisLearningEventV1,
    LearningStage,
    RequiredAction,
    record_gap,
    resolve_gap,
)

_NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _h(roles: str = "feature_engineer", user: str = "u") -> dict:
    return {"X-User": user, "X-Roles": roles}


def _gap(conn, *, code: str, subjects: tuple[str, ...], request_id: str,
         action: RequiredAction = RequiredAction.CONFIRM_RELATIONSHIP) -> str:
    return record_gap(conn, AnalysisLearningEventV1(
        analysis_request_id=request_id, stage=LearningStage.PLANNING, code=code,
        subject_refs=subjects, required_action=action,
        dependency_snapshot_id="snap-1"), now=_NOW)


_JOIN_SUBJECTS = ("ftr::dpl_eib.customer_master.cif_id", "ftr::dpl_eib.tran_repos.cif_id")


@pytest.fixture
def seeded(conn):
    """Two gaps whose demand order DISAGREES with their alphabetical order.

    This matters: `RELATIONSHIP_UNVERIFIED` sorts before `REVERSAL_AS_OF_UNRESOLVED` (L before V), so
    seeding the relationship gap as the most-blocking one made the ordering assertion pass even with
    the demand ordering deleted — the test agreed with the accident. Giving the alphabetically-FIRST
    code the LOWER demand is what makes it discriminate.
    """
    _gap(conn, code="REVERSAL_AS_OF_UNRESOLVED", request_id="req-1",
         subjects=("ftr::dpl_eib.tran_repos.reversal_flag",),
         action=RequiredAction.CONFIRM_BUSINESS_POLICY)
    _gap(conn, code="REVERSAL_AS_OF_UNRESOLVED", request_id="req-2",
         subjects=("ftr::dpl_eib.tran_repos.reversal_flag",),
         action=RequiredAction.CONFIRM_BUSINESS_POLICY)
    _gap(conn, code="RELATIONSHIP_UNVERIFIED", request_id="req-1", subjects=_JOIN_SUBJECTS)
    return conn


# ── the read ─────────────────────────────────────────────────────────────────────────────────────

def test_the_route_lists_the_open_gaps(client, seeded):
    body = client.get("/learning/gaps", headers=_h()).json()
    assert {g["code"] for g in body["gaps"]} == {
        "RELATIONSHIP_UNVERIFIED", "REVERSAL_AS_OF_UNRESOLVED"}


def test_the_most_blocking_gap_comes_first(client, seeded):
    """The prioritisation signal the release asked for, and the reason gap identity excludes the
    request id: two questions waiting on one undecided relationship is a stronger case for deciding
    it than one question waiting on something else."""
    gaps = client.get("/learning/gaps", headers=_h()).json()["gaps"]
    # REVERSAL sorts AFTER RELATIONSHIP alphabetically, so leading with it can only be the demand
    # ordering — sorting by code alone would put the relationship gap first.
    assert [g["code"] for g in gaps] == ["REVERSAL_AS_OF_UNRESOLVED", "RELATIONSHIP_UNVERIFIED"]
    assert [g["blocked_requests"] for g in gaps] == [2, 1]


def test_a_gap_whose_answer_is_a_closed_vocabulary_carries_its_CHOICES(client, seeded):
    """A reviewer told only "reversal semantics unresolved" has to go and rediscover what they are
    choosing between. The two options ride on the gap."""
    gaps = client.get("/learning/gaps", headers=_h()).json()["gaps"]
    reversal = next(g for g in gaps if g["code"] == "REVERSAL_AS_OF_UNRESOLVED")
    assert reversal["choices"] == ["reversed_by_cutoff", "reversed_at_any_time"]
    assert reversal["required_action"] == "confirm_business_policy"


def test_the_gap_names_the_COLUMNS_it_is_about(client, seeded):
    """Subjects are what make a gap actionable, and they must arrive intact — `open_gaps` used to
    rebuild them by splitting the array's text form, which mangled any value PostgreSQL quotes."""
    gaps = client.get("/learning/gaps", headers=_h()).json()["gaps"]
    join_gap = next(g for g in gaps if g["code"] == "RELATIONSHIP_UNVERIFIED")
    assert join_gap["subject_refs"] == list(_JOIN_SUBJECTS)


def test_a_RESOLVED_gap_leaves_the_queue(client, conn):
    """The queue is derived, never stored — and resolution is an append, so the original event is
    still there. A gap that stayed visible after a decision would make the backlog unusable."""
    event_id = _gap(conn, code="REVERSAL_AS_OF_UNRESOLVED", request_id="req-9",
                    subjects=("ftr::dpl_eib.tran_repos.reversal_flag",),
                    action=RequiredAction.CONFIRM_BUSINESS_POLICY)
    assert client.get("/learning/gaps", headers=_h()).json()["gaps"]
    resolve_gap(conn, event_id, decision="reversed_by_cutoff", actor="priya", now=_NOW)
    assert client.get("/learning/gaps", headers=_h()).json()["gaps"] == []


def test_an_empty_queue_is_an_empty_list_not_an_error(client):
    r = client.get("/learning/gaps", headers=_h())
    assert r.status_code == 200
    assert r.json()["gaps"] == []


# ── access ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["catalog_viewer", "data_owner", "feature_engineer",
                                  "platform_admin"])
def test_every_role_that_could_act_on_a_gap_can_read_it(client, seeded, role):
    """The P4 lesson, asserted rather than hoped for: a queue only `platform_admin` can open is a
    queue nobody opens."""
    assert client.get("/learning/gaps", headers=_h(role)).status_code == 200


def test_a_caller_with_no_catalog_read_is_refused(client, seeded):
    """`access_admin` holds `iam:manage` only — separation of duties from data work."""
    assert client.get("/learning/gaps", headers=_h("access_admin")).status_code == 403


def test_the_route_is_read_only(client, seeded):
    for method in ("post", "put", "delete", "patch"):
        r = getattr(client, method)("/learning/gaps", headers=_h())
        assert r.status_code == 405, method
