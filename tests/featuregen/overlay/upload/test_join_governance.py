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

from dataclasses import asdict
from datetime import UTC, datetime

from tests.featuregen.overlay.upload.passc.conftest import (  # noqa: F401 — pytest fixtures
    SERVICE_ACTOR,
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
from featuregen.overlay.upload.join_governance import (
    list_open_approved_join_proposals,
    read_join_approvals,
)
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import build_join_ref
from featuregen.overlay.upload.passc.types import ALGORITHM_VERSION

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


def test_limit_is_clamped_and_bounds_proposals(passc_conn):
    _propose_join(passc_conn, _bare_ref("loans", "parties", "party_id"))
    _propose_join(passc_conn, _bare_ref("cards", "parties", "party_id"))
    assert len(list_open_approved_join_proposals(passc_conn, "src")) == 2
    assert len(list_open_approved_join_proposals(passc_conn, "src", limit=1)) == 1
    assert len(list_open_approved_join_proposals(passc_conn, "src", limit=0)) == 1   # clamped up
    assert len(list_open_approved_join_proposals(passc_conn, "src", limit=9999)) == 2
