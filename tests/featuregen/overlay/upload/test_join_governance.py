"""Confirmation-surface Task 3 — the join-governance read model + approval-stream reader.

`list_open_approved_join_proposals` lists a source's OPEN discovered-join proposals (folded
DRAFT -> displayed "PROPOSED", or PARTIALLY_CONFIRMED) as ONE view per `fact_key` — a dual join
opens TWO side-labelled `human_tasks`, which must collapse into one proposal carrying both task
rows. The view decodes from/to/cardinality from the DRAFT event's typed `ApprovedJoinRef`
(structural truth), shapes the pre-minted Pass C reviewer evidence TOLERANTLY (missing ->
"missing", defaulted fields -> "partial" + warnings, wrong types -> "invalid" — never a crash),
and reads approvals (subject/role/note/confirmed_at) off the PARTIALLY_CONFIRMED/CONFIRMED
events (Task 1 threaded `note`). One corrupt task must NEVER abort the whole queue.

Seeding mirrors `passc/propose.py::_propose_one`: pre-mint the evidence row
(`metric_values=asdict(JoinCandidateEvidenceV1)`) and dispatch `propose_fact` with its
`evidence_ref`; the bare no-evidence path reuses the passc conftest's `_propose_join`.
"""
# ruff: noqa: F811 — the passc conftest fixtures are IMPORTED by name (this module lives outside
# tests/.../passc/, so its conftest does not apply); pytest resolves them via the test parameters,
# which ruff sees as redefinitions of the imports.
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Json
from tests.featuregen.overlay.upload.passc.conftest import (  # noqa: F401 — pytest fixtures
    SERVICE_ACTOR,
    _confirm_join,
    _join_value,
    _propose_join,
    human_admin_1,
    human_admin_2,
    passc_conn,
)

from featuregen.contracts import GateTaskSpec
from featuregen.contracts.envelopes import Command
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.gates.tasks import open_task
from featuregen.overlay import facts
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer, write_evidence
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.upload import join_governance
from featuregen.overlay.upload.join_governance import (
    JoinGovernanceNotFound,
    list_open_approved_join_proposals,
    load_join_confirmation_context,
    project_verified_join,
    read_join_approvals,
)
from featuregen.overlay.upload.join_path import JoinStep, find_join_path
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import build_join_ref
from featuregen.overlay.upload.passc.types import ALGORITHM_VERSION
from featuregen.overlay.upload.upload_catalog import table_ref
from featuregen.runtime.observability import counters

_CIF_TERM = "Customer Information File Identifier"


# ── Seed helpers (the Task-1/Task-7 test shapes: one blocked pair, scored, proposed) ─────────────


def _col(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def _strong_evidence():
    """A strong, grain-inferred N:1 candidate: transactions.cif_id -> customers.cif_id."""
    pairs = block_candidates([_col("transactions", "cif_id", term_name=_CIF_TERM),
                              _col("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)])
    assert len(pairs) == 1, "test setup must yield exactly one blocked pair"
    ev = score(pairs[0], source_snapshot_id="snap-1")
    assert ev.bucket == "strong" and ev.proposed_cardinality == "N:1"
    return ev


def _propose_with_metric_values(conn, ref, metric_values):
    """Mirror `passc/propose.py::_propose_one`: pre-mint the reviewer evidence row, then
    dispatch `propose_fact` with its `evidence_ref` (rides the DRAFT payload + gate tasks)."""
    key = fact_key(ref, "approved_join")
    evidence_ref = write_evidence(
        conn, fact_key=key, table_snapshot_at=datetime.now(UTC), row_count=0, sample_size=0,
        profile_version=ALGORITHM_VERSION, thresholds_used={}, metric_values=metric_values,
        created_by=identity_to_jsonb(SERVICE_ACTOR),
        producer=EvidenceProducer.STRUCTURAL_CONNECTOR, strength=AssertionStrength.PROPOSED)
    value = _join_value(ref)
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "proposed_value": value,
         "evidence_ref": evidence_ref},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return key


def _seed_join_with_evidence(conn):
    """The canonical seeded proposal: full JoinCandidateEvidenceV1 metric_values."""
    ev = _strong_evidence()
    ref = build_join_ref(ev, "src")
    key = _propose_with_metric_values(conn, ref, asdict(ev))
    return ref, key


def _bare_ref(from_table, to_table, column):
    return ApprovedJoinRef(
        from_ref=CatalogObjectRef("src", "column", "public", from_table, column),
        to_ref=CatalogObjectRef("src", "column", "public", to_table, column),
        column_pairs=(ColumnPair(column, column),),
        cardinality="N:1")


def _confirm_once(conn, ref, key, actor, note):
    """Drive ONE confirm (the first reaches PARTIALLY_CONFIRMED) with a Task-1 approver note."""
    target = _cas_target(fold_overlay_state(load_fact(conn, key)))
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "use_case": None,
         "target_event_id": target, "note": note},
        actor, f"confirm-{actor.subject}-{target}"))
    assert res.accepted, res.denied_reason
    return res


# ── The read model lists an open proposal with its evidence ─────────────────────────────────────


def test_lists_a_proposed_join_with_evidence(passc_conn):
    ref, key = _seed_join_with_evidence(passc_conn)
    out = list_open_approved_join_proposals(passc_conn, ref.from_ref.catalog_source)
    assert len(out) == 1                                   # dedup: ONE proposal per fact_key
    p = out[0]
    assert p["fact_key"] == key == fact_key(ref, "approved_join", None)
    assert p["status"] == "PROPOSED"
    assert p["from"] == {"table": "transactions", "column": "cif_id"}
    assert p["to"] == {"table": "customers", "column": "cif_id"}
    assert p["cardinality"] in ("1:1", "1:N", "N:1")
    assert p["approvals"] == []
    # the dual join opened two side-labelled platform-admin tasks — both accumulate here
    assert len(p["tasks"]) == 2
    assert {t["side"] for t in p["tasks"]} == {"from", "to"}
    assert all(t["status"] == "open" and t["task_id"] for t in p["tasks"])
    # evidence: fully parsed from metric_values = asdict(JoinCandidateEvidenceV1)
    assert p["evidence_parse_status"] == "parsed"
    assert isinstance(p["evidence"]["score"], int)
    assert p["evidence"]["grain_status"] == "inferred_from_confirmed_grain"
    assert p["evidence"]["namespace_compatibility"]
    assert p["evidence"]["warnings"] == []
    assert p["evidence_version"] == ALGORITHM_VERSION
    assert "cif_id" in p["proposed_direction"]


def test_source_filter_is_normalized(passc_conn):
    _seed_join_with_evidence(passc_conn)
    assert len(list_open_approved_join_proposals(passc_conn, "  SRC  ")) == 1


def test_excludes_other_sources(passc_conn):
    _seed_join_with_evidence(passc_conn)
    assert list_open_approved_join_proposals(passc_conn, "some-other-source") == []


# ── Approval state ───────────────────────────────────────────────────────────────────────────────


def test_partial_confirm_shows_approver_and_note(passc_conn, human_admin_1):
    ref, key = _seed_join_with_evidence(passc_conn)
    _confirm_once(passc_conn, ref, key, human_admin_1, "looks right")
    out = list_open_approved_join_proposals(passc_conn, ref.from_ref.catalog_source)
    assert len(out) == 1
    assert out[0]["status"] == "PARTIALLY_CONFIRMED"
    (approval,) = out[0]["approvals"]
    assert approval["subject"] == human_admin_1.subject
    assert approval["note"] == "looks right"
    assert approval["display_name"] is None
    assert approval["role"].startswith("data_owner_")
    assert approval["confirmed_at"]                        # ISO timestamp string
    assert isinstance(approval["confirmed_at"], str)


def test_read_join_approvals_after_full_confirm(passc_conn, human_admin_1, human_admin_2):
    """Both confirm events yield approvals; the fully-VERIFIED join leaves the open list."""
    ref, key = _seed_join_with_evidence(passc_conn)
    _confirm_once(passc_conn, ref, key, human_admin_1, "looks right")
    _confirm_once(passc_conn, ref, key, human_admin_2, "ship it")
    assert fold_overlay_state(load_fact(passc_conn, key)).status == "VERIFIED"

    approvals = read_join_approvals(passc_conn, key)
    assert len(approvals) == 2                             # deduped: partial + confirmed
    by_subject = {a["subject"]: a for a in approvals}
    assert by_subject[human_admin_1.subject]["note"] == "looks right"
    assert by_subject[human_admin_2.subject]["note"] == "ship it"
    assert {a["role"] for a in approvals} == {"data_owner_from", "data_owner_to"}
    assert all(a["confirmed_at"] for a in approvals)
    # VERIFIED is not an open proposal (its tasks were closed by the second confirm)
    assert list_open_approved_join_proposals(passc_conn, "src") == []


# ── Evidence tolerance (never a crash) ───────────────────────────────────────────────────────────


def test_evidence_missing_does_not_crash(passc_conn):
    """A bare approved_join proposal with NO evidence row is still listed — status "missing"."""
    ref = _bare_ref("loans", "parties", "party_id")
    _propose_join(passc_conn, ref)
    out = list_open_approved_join_proposals(passc_conn, "src")
    assert len(out) == 1
    assert out[0]["evidence_parse_status"] == "missing"
    assert out[0]["evidence"] == {}
    assert out[0]["evidence_version"] is None
    assert out[0]["from"] == {"table": "loans", "column": "party_id"}   # structural truth intact


def test_partial_evidence_defaults_with_warnings(passc_conn):
    ref = _bare_ref("cards", "parties", "party_id")
    _propose_with_metric_values(passc_conn, ref, {"score": 80, "explanation": "just a score"})
    out = list_open_approved_join_proposals(passc_conn, "src")
    assert len(out) == 1
    assert out[0]["evidence_parse_status"] == "partial"
    assert out[0]["evidence"]["score"] == 80
    assert out[0]["evidence"]["positive_signals"] == []    # defaulted
    assert out[0]["evidence"]["grain_status"] is None      # defaulted
    assert out[0]["evidence"]["warnings"]                  # each defaulted field warned


def test_invalid_evidence_marked_invalid(passc_conn):
    ref = _bare_ref("deposits", "parties", "party_id")
    _propose_with_metric_values(passc_conn, ref, {"score": "eighty"})   # wrong TYPE
    out = list_open_approved_join_proposals(passc_conn, "src")
    assert len(out) == 1
    assert out[0]["evidence_parse_status"] == "invalid"
    assert out[0]["evidence"] == {}


# ── Failure isolation + bounds ───────────────────────────────────────────────────────────────────


def test_corrupt_ref_skipped_without_breaking_queue(passc_conn):
    """A task whose DRAFT ref decodes to a NON-join ref is skipped; the good join still lists."""
    bad_key = "corrupt-join-fact-key"
    draft = append_overlay_event(
        passc_conn, fact_key=bad_key, type="OVERLAY_FACT_PROPOSED",
        payload={
            # a TABLE ref (no column_pairs): _ref_from_payload yields a CatalogObjectRef,
            # NOT an ApprovedJoinRef — the reader must skip it, not raise
            "catalog_object_ref": asdict(CatalogObjectRef("src", "table", "public", "orphans")),
            "object_ref": "public.orphans", "fact_type": "approved_join", "use_case": None,
            "proposed_value": _join_value(_bare_ref("x1", "x2", "id")),
            "proposal_fingerprint": "fp-corrupt", "evidence_ref": None,
            "proposed_by": SERVICE_ACTOR.subject},
        actor=SERVICE_ACTOR, expected_version=0)
    open_task(passc_conn, GateTaskSpec(
        gate="OVERLAY_DATA_OWNER", required_inputs=("proposed_value",),
        eligible_assignees={"role": "platform-admin"}, allowed_responses=("confirm", "reject"),
        fact_key=bad_key, draft_event_id=draft.event_id, target_event_id=draft.event_id),
        SERVICE_ACTOR)

    ref, good_key = _seed_join_with_evidence(passc_conn)
    out = list_open_approved_join_proposals(passc_conn, "src")
    assert [p["fact_key"] for p in out] == [good_key]


def _open_synthetic_task(conn, key, eligible, fingerprint):
    """Append a minimal valid DRAFT for `key` and open ONE gate task on it with the given
    `eligible_assignees` — the seed for the isolation tests below."""
    draft = append_overlay_event(
        conn, fact_key=key, type="OVERLAY_FACT_PROPOSED",
        payload={
            "catalog_object_ref": asdict(CatalogObjectRef("src", "table", "public", "orphans")),
            "object_ref": "public.orphans", "fact_type": "approved_join", "use_case": None,
            "proposed_value": _join_value(_bare_ref("x1", "x2", "id")),
            "proposal_fingerprint": fingerprint, "evidence_ref": None,
            "proposed_by": SERVICE_ACTOR.subject},
        actor=SERVICE_ACTOR, expected_version=0)
    return open_task(conn, GateTaskSpec(
        gate="OVERLAY_DATA_OWNER", required_inputs=("proposed_value",),
        eligible_assignees=eligible, allowed_responses=("confirm", "reject"),
        fact_key=key, draft_event_id=draft.event_id, target_event_id=draft.event_id),
        SERVICE_ACTOR)


def test_non_dict_eligible_assignees_does_not_abort_queue(passc_conn):
    """A human_tasks row whose eligible_assignees JSONB is a LIST (not an object) is read when
    the {task_id, side, status} row is built — it must corrupt only its OWN task, never
    AttributeError the whole list (Task 3 review FIX 2)."""
    task_id = _open_synthetic_task(passc_conn, "corrupt-eligible-fact-key",
                                   {"role": "platform-admin"}, "fp-bad-eligible")
    passc_conn.execute("UPDATE human_tasks SET eligible_assignees = %s WHERE task_id = %s",
                       (Json(["not", "a", "mapping"]), task_id))

    _, good_key = _seed_join_with_evidence(passc_conn)
    out = list_open_approved_join_proposals(passc_conn, "src")
    assert [p["fact_key"] for p in out] == [good_key]


def test_authz_denied_task_is_a_benign_skip_not_corruption(passc_conn, caplog):
    """A subject-scoped (data-owner) task the subject-less governance reader is not bound to is
    a NORMAL "not my task" in a mixed-catalog DB — it must skip at DEBUG without the warning or
    the `task_unreadable` corruption counter (those stay reserved for genuinely unreadable
    tasks) and must not disturb the listing (Task 3 review FIX 1)."""
    _open_synthetic_task(passc_conn, "subject-scoped-fact-key",
                         {"role": "data_owner", "subject": "user:someone-else"}, "fp-subject")

    _, good_key = _seed_join_with_evidence(passc_conn)
    counter = "overlay.join_governance.task_unreadable"
    before = counters.snapshot()["counters"].get(counter, 0)
    with caplog.at_level(logging.DEBUG, logger="featuregen.overlay.upload.join_governance"):
        out = list_open_approved_join_proposals(passc_conn, "src")
    assert [p["fact_key"] for p in out] == [good_key]
    assert counters.snapshot()["counters"].get(counter, 0) == before
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(r.levelno == logging.DEBUG and "governance reader" in r.getMessage()
               for r in caplog.records)


def test_read_join_approvals_is_best_effort_on_malformed_event(monkeypatch):
    """Standalone (the Task-5 confirm response body), a malformed event on the stream must yield
    a best-effort [] rather than raising — inside the list it is already per-task guarded
    (Task 3 review FIX 3). The store's schema validation blocks APPENDING such an event, so the
    corrupt stream is stubbed at the exact load_fact boundary the guard protects."""
    class _MalformedEvent:
        type = facts.OVERLAY_FACT_PARTIALLY_CONFIRMED
        payload = ["not", "a", "mapping"]   # .get() on a list -> AttributeError

    monkeypatch.setattr(join_governance, "load_fact", lambda conn, key: [_MalformedEvent()])
    assert read_join_approvals(object(), "any-key") == []


def test_limit_is_clamped_and_bounds_proposals(passc_conn):
    _propose_join(passc_conn, _bare_ref("loans", "parties", "party_id"))
    _propose_join(passc_conn, _bare_ref("cards", "parties", "party_id"))
    assert len(list_open_approved_join_proposals(passc_conn, "src")) == 2
    assert len(list_open_approved_join_proposals(passc_conn, "src", limit=1)) == 1
    assert len(list_open_approved_join_proposals(passc_conn, "src", limit=0)) == 1   # clamped up
    assert len(list_open_approved_join_proposals(passc_conn, "src", limit=9999)) == 2


# ── Task 4: confirm/reject context bridge ────────────────────────────────────────────────────────


def _seed_grain_fact(passc_conn) -> str:
    """A NON-join fact seeded through the real table-fact propose path — the generic-approval-hole
    probe: its fact_key must NEVER load as a join confirmation context."""
    ref = table_ref("src", "txn")
    value = {"columns": ["id"], "is_unique": True}
    res = propose_fact(passc_conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return fact_key(ref, "grain")


def test_context_returns_typed_ref_and_a_target_a_real_confirm_accepts(
        passc_conn, human_admin_1, human_admin_2):
    """THE load-bearing wiring: the returned target_event_id must be the exact CAS target
    `confirm_fact` accepts — proven by driving BOTH stages of the dual confirm with a freshly
    loaded context each time (DRAFT head, then the PARTIALLY_CONFIRMED cycle-stable target)."""
    ref, key = _seed_join_with_evidence(passc_conn)
    ctx = load_join_confirmation_context(passc_conn, key)
    assert ctx["fact_type"] == "approved_join"
    assert ctx["use_case"] is None
    assert isinstance(ctx["ref"], ApprovedJoinRef)
    assert ctx["ref"].from_ref.column == ref.from_ref.column
    assert ctx["ref"].to_ref.table == ref.to_ref.table
    assert ctx["target_event_id"]                          # the current head

    first = confirm_fact(passc_conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
         "target_event_id": ctx["target_event_id"], "note": "via context"},
        human_admin_1, f"ctx-confirm-1-{ctx['target_event_id']}"))
    assert first.accepted, first.denied_reason

    ctx2 = load_join_confirmation_context(passc_conn, key)  # PARTIALLY_CONFIRMED re-read
    second = confirm_fact(passc_conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ctx2["ref"], "fact_type": ctx2["fact_type"], "use_case": ctx2["use_case"],
         "target_event_id": ctx2["target_event_id"], "note": "second side"},
        human_admin_2, f"ctx-confirm-2-{ctx2['target_event_id']}"))
    assert second.accepted, second.denied_reason
    assert fold_overlay_state(load_fact(passc_conn, key)).status == "VERIFIED"


def test_context_rejects_non_join_fact(passc_conn):
    """The fact_type gate: a grain fact_key must raise (Task 5 maps to 404) — the join confirm
    surface can never become a generic approval endpoint."""
    grain_key = _seed_grain_fact(passc_conn)
    with pytest.raises(JoinGovernanceNotFound):
        load_join_confirmation_context(passc_conn, grain_key)


def test_context_rejects_unknown_fact_key(passc_conn):
    with pytest.raises(JoinGovernanceNotFound):
        load_join_confirmation_context(passc_conn, "no-such-fact-key")


def test_context_rejects_ref_whose_decode_throws(passc_conn):
    """The decode-EXCEPTION branch (whole-branch review, Minor): a `catalog_object_ref` payload
    that is present and schema-valid (an object) but genuinely UNDECODABLE — `_ref_from_payload`
    raises building the dataclasses — must surface as JoinGovernanceNotFound (the routes' 404),
    never a 500. Distinct from the isinstance/non-join case below, which decodes fine."""
    bad_key = "ctx-undecodable-join-fact-key"
    append_overlay_event(
        passc_conn, fact_key=bad_key, type="OVERLAY_FACT_PROPOSED",
        payload={
            # "column_pairs" routes _ref_from_payload down the ApprovedJoinRef arm, where
            # CatalogObjectRef(**{"bogus": ...}) raises TypeError — a real decode exception.
            "catalog_object_ref": {"column_pairs": [], "cardinality": "N:1",
                                   "from_ref": {"bogus": 1}, "to_ref": {"bogus": 2}},
            "object_ref": "public.orphans", "fact_type": "approved_join", "use_case": None,
            "proposed_value": _join_value(_bare_ref("x1", "x2", "id")),
            "proposal_fingerprint": "fp-ctx-undecodable", "evidence_ref": None,
            "proposed_by": SERVICE_ACTOR.subject},
        actor=SERVICE_ACTOR, expected_version=0)
    with pytest.raises(JoinGovernanceNotFound):
        load_join_confirmation_context(passc_conn, bad_key)


def test_context_rejects_undecodable_ref(passc_conn):
    """A DRAFT that CLAIMS fact_type approved_join but whose ref decodes to a plain table
    CatalogObjectRef (no column_pairs) is not a typed join ref — raises, never returned."""
    bad_key = "ctx-corrupt-join-fact-key"
    append_overlay_event(
        passc_conn, fact_key=bad_key, type="OVERLAY_FACT_PROPOSED",
        payload={
            "catalog_object_ref": asdict(CatalogObjectRef("src", "table", "public", "orphans")),
            "object_ref": "public.orphans", "fact_type": "approved_join", "use_case": None,
            "proposed_value": _join_value(_bare_ref("x1", "x2", "id")),
            "proposal_fingerprint": "fp-ctx-corrupt", "evidence_ref": None,
            "proposed_by": SERVICE_ACTOR.subject},
        actor=SERVICE_ACTOR, expected_version=0)
    with pytest.raises(JoinGovernanceNotFound):
        load_join_confirmation_context(passc_conn, bad_key)


# ── Task 4: synchronous verified-join projection ─────────────────────────────────────────────────

# The seeded candidate's endpoints in PUBLIC graph scope (graph_node.object_ref form).
_FROM = "public.transactions.cif_id"
_TO = "public.customers.cif_id"


def test_project_verified_join_creates_operational_edge(passc_conn, human_admin_1, human_admin_2):
    """A just-VERIFIED join becomes operational SYNCHRONOUSLY — find_join_path traverses without
    waiting for a re-upload (`_confirm_join` drains the overlay READ-MODEL projection but never
    projects graph edges, so the edge here is created by `project_verified_join` alone)."""
    ref, _key = _seed_join_with_evidence(passc_conn)
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)
    assert find_join_path(passc_conn, "src", "transactions", "customers") is None

    status = project_verified_join(passc_conn, ref.from_ref.catalog_source, ref, now=None)
    assert status == "projected"
    assert find_join_path(passc_conn, "src", "transactions", "customers") \
        == [JoinStep(_FROM, _TO, "N:1")]


def test_project_verified_join_projects_within_the_confirming_request(
        passc_conn, human_admin_1, human_admin_2):
    """THE route-path shape (whole-branch review, FIX 2): `confirm_fact` has JUST appended
    OVERLAY_FACT_CONFIRMED on this same uncommitted connection and NOTHING drained the overlay
    projection (unlike `_confirm_join`), so `projection_lag(conn, "overlay")` >= 1 when
    `project_verified_join` runs. The helper must DRAIN on this conn (mirroring the ingest path's
    drain-before-project) and then project — the operational edge exists and `find_join_path`
    traverses it IMMEDIATELY, no re-ingest. Pre-fix this always returned "pending" (edge absent
    until a future re-upload)."""
    ref, key = _seed_join_with_evidence(passc_conn)
    _confirm_once(passc_conn, ref, key, human_admin_1, "side one")
    _confirm_once(passc_conn, ref, key, human_admin_2, "side two")   # VERIFIED — no drain ran
    assert fold_overlay_state(load_fact(passc_conn, key)).status == "VERIFIED"

    status = project_verified_join(passc_conn, ref.from_ref.catalog_source, ref, now=None)
    assert status == "projected"
    rows = passc_conn.execute(
        "SELECT authority, approved_join_fact_key, approved_join_status FROM graph_edge"
        " WHERE kind = 'joins' AND catalog_source = 'src'").fetchall()
    assert rows == [("operational", key, "VERIFIED")]
    assert find_join_path(passc_conn, "src", "transactions", "customers") \
        == [JoinStep(_FROM, _TO, "N:1")]


def test_project_verified_join_pending_when_drift_watermark_stale(
        passc_conn, human_admin_1, human_admin_2):
    """HONEST REPORTING under the drift-freshness guard (the common governance case: an admin
    approves a queued proposal hours/days after the upload). The join is VERIFIED, but the
    source's drift watermark is STALE (older than `drift_freshness_sla`), so `resolve_fact`
    refuses to serve the fact and `project_confirmed_joins` writes NO operational edge — the
    refusal is CORRECT and must stand. `project_verified_join` must then report "pending", not
    "projected": the planner cannot traverse an edge that was never written. Pre-fix this
    returned "projected" with zero edges (a mis-report the UI would display as live)."""
    from datetime import timedelta

    from featuregen.overlay.catalog_changes import _write_watermark
    from featuregen.overlay.config import OverlayConfig, register_overlay_config

    ref, _key = _seed_join_with_evidence(passc_conn)
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)   # VERIFIED, drained
    # Seal a config (the drift guard's opt-in — mirrors test_source_qualified's guard tests) and
    # stamp the source's watermark 2h in the past against a 60m SLA: resolve_fact fails closed.
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=False))
    now = datetime.now(UTC)
    _write_watermark(passc_conn, "src", now - timedelta(hours=2))

    assert project_verified_join(passc_conn, "src", ref, now=now) == "pending"
    assert passc_conn.execute(
        "SELECT count(*) FROM graph_edge WHERE kind = 'joins'"
        " AND authority = 'operational'").fetchone()[0] == 0


def test_project_verified_join_pending_when_projection_cannot_drain(passc_conn, monkeypatch):
    """The residual-lag fallback: when the drain cannot reach head (a poison-HALTED projection —
    `run_projection` stops advancing while events remain), `resolve_fact` could read a stale
    read model, so the helper defers ("pending") and writes NO edge."""
    ref, _key = _seed_join_with_evidence(passc_conn)   # the propose appended events; halted below
    monkeypatch.setattr(join_governance, "run_projection", lambda conn, projection: 0)
    assert project_verified_join(passc_conn, "src", ref, now=None) == "pending"
    assert passc_conn.execute(
        "SELECT count(*) FROM graph_edge WHERE kind = 'joins'").fetchone()[0] == 0


def test_project_verified_join_is_fail_soft(
        passc_conn, human_admin_1, human_admin_2, monkeypatch):
    """A projector fault must NEVER raise out of the confirm response — "pending" is returned and
    the fact stays VERIFIED (the next caught-up ingest re-projects it)."""
    ref, key = _seed_join_with_evidence(passc_conn)
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)   # lag drained to 0

    def _boom(*args, **kwargs):
        raise RuntimeError("projector exploded")

    monkeypatch.setattr(join_governance, "project_confirmed_joins", _boom)
    assert project_verified_join(passc_conn, "src", ref, now=None) == "pending"
    assert fold_overlay_state(load_fact(passc_conn, key)).status == "VERIFIED"
