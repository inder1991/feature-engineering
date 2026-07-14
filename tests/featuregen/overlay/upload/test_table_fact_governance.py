"""Pass B confirm surface, Task 1 — the table-fact governance read model + confirmation bridge.

`list_open_table_fact_proposals_governance` lists a source's OPEN grain/availability_time
proposals (folded DRAFT -> displayed "PROPOSED") as ONE view per `fact_key`, decoding the table
from the DRAFT event's typed `CatalogObjectRef` (structural truth), stamping the origin honestly
("llm_proposed_not_profiled" — Pass B never profiles), and reading the table's advisory fields
(table_role/primary_entity/event_or_snapshot) BEST-EFFORT (display-only, null on absence/error).
One corrupt task must never abort the queue.

`load_table_fact_confirmation_context` turns a fact_key back into the typed command args a
confirm/reject route dispatches — fact_type-VALIDATED (a non-table fact_key raises
`TableFactGovernanceNotFound`, so the surface can never approve a join/policy fact), with
`target_event_id = _cas_target(state)` — proven below by driving a REAL `confirm_fact` with it.

`project_verified_table_fact` makes a just-VERIFIED grain/as-of operational SYNCHRONOUSLY
(drain-then-project onto graph_node, fail-soft) and reports HONESTLY: "projected" only when the
flag actually landed; a stale drift watermark's correct refusal reports "pending".
"""
# ruff: noqa: F811 — the passc conftest fixtures are IMPORTED by name (this module lives outside
# tests/.../passc/, so its conftest does not apply); pytest resolves them via the test parameters,
# which ruff sees as redefinitions of the imports.
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload.passc.conftest import (  # noqa: F401 — pytest fixtures
    SERVICE_ACTOR,
    _propose_join,
    human_admin_1,
    passc_conn,
)

from featuregen.contracts import GateTaskSpec
from featuregen.contracts.envelopes import Command
from featuregen.gates.tasks import open_task
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.upload import table_fact_governance
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.table_fact_governance import (
    TableFactGovernanceNotFound,
    list_open_table_fact_proposals_governance,
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)
from featuregen.overlay.upload.upload_catalog import table_ref

# ── Seed helpers ─────────────────────────────────────────────────────────────────────────────────

_GRAIN_VALUE = {"columns": ["cif_id"], "is_unique": True}


def _seed_grain(conn, source="src", table="t", value=None):
    """Seed a DRAFT grain proposal exactly the way Pass B does (`_propose_table_facts`): the real
    `propose_fact` command from the service enrichment actor — opens the platform-admin gate task."""
    value = dict(value or _GRAIN_VALUE)
    ref = table_ref(source, table)
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return ref, fact_key(ref, "grain")


def _seed_availability(conn, source="src", table="u"):
    value = {"column": "as_of_date", "basis": "posted_at"}
    ref = table_ref(source, table)
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "availability_time", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return ref, fact_key(ref, "availability_time")


def _join_ref():
    return ApprovedJoinRef(
        from_ref=CatalogObjectRef("src", "column", "public", "transactions", "cif_id"),
        to_ref=CatalogObjectRef("src", "column", "public", "customers", "cif_id"),
        column_pairs=(ColumnPair("cif_id", "cif_id"),),
        cardinality="N:1")


def _confirm_via_context(conn, key, actor, *, value=None):
    """Drive a REAL `confirm_fact` with the context bridge's own args — the load-bearing proof that
    `target_event_id` is the exact CAS target the command accepts (grain/as-of is single-confirm:
    one platform-admin -> VERIFIED; four-eyes holds since the proposer is the service actor)."""
    ctx = load_table_fact_confirmation_context(conn, key)
    args = {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
            "target_event_id": ctx["target_event_id"]}
    if value is not None:
        args["value"] = value
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None, args,
        actor, f"ctx-confirm-{ctx['target_event_id']}"))
    assert res.accepted, res.denied_reason
    return res


def _seed_graph_nodes(conn, source="src", table="t", cols=("cif_id", "amt")):
    """Physical column nodes for the projection tests (mirrors the upload conftest's seeded_graph)."""
    for col in cols:
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name,"
            " is_grain, is_as_of) VALUES (%s, %s, 'column', %s, %s, false, false)",
            (source, f"public.{table}.{col}", table, col))


def _seal_drift_config():
    """Seal an OverlayConfig (the drift-freshness guard's opt-in) with a 60m SLA — mirrors the
    join-governance stale-watermark test."""
    from featuregen.overlay.config import OverlayConfig, register_overlay_config
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=False))


def _grain_flags(conn, source="src", table="t"):
    return {c: (g, e) for c, g, e in conn.execute(
        "SELECT column_name, is_grain, grain_fact_event_id FROM graph_node "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
        (source, table)).fetchall()}


# ── The read model lists an open proposal ────────────────────────────────────────────────────────


def test_lists_a_grain_proposal(passc_conn):
    ref, key = _seed_grain(passc_conn)
    out = list_open_table_fact_proposals_governance(passc_conn, "src")
    assert len(out) == 1
    p = out[0]
    assert p["fact_key"] == key == fact_key(ref, "grain", None)
    assert p["task_id"]
    assert p["target_event_id"]
    assert p["fact_type"] == "grain"
    assert p["table"] == "t"
    assert p["status"] == "PROPOSED"
    assert p["proposed_value"]["columns"] == ["cif_id"]
    assert p["proposed_value"]["is_unique"] is True
    # the honest origin: Pass B proposals are LLM synthesis, never profiler-proven
    assert p["origin"] == "llm_proposed_not_profiled"
    # no advisory evidence seeded -> display-only nulls, never an error
    assert p["advisory"] == {"table_role": None, "primary_entity": None,
                             "event_or_snapshot": None}
    assert p["evidence_parse_status"] == "parsed"


def test_lists_an_availability_proposal(passc_conn):
    _seed_availability(passc_conn)
    out = list_open_table_fact_proposals_governance(passc_conn, "src")
    assert len(out) == 1
    assert out[0]["fact_type"] == "availability_time"
    assert out[0]["table"] == "u"
    assert out[0]["proposed_value"] == {"column": "as_of_date", "basis": "posted_at"}


def test_advisory_fields_read_from_field_evidence(passc_conn):
    """The advisory table fields Pass B records as LLM field evidence (table_synth
    `_ADVISORY_TABLE_FIELDS`) surface on the view — read from the table's logical_ref, latest
    active row wins."""
    _seed_grain(passc_conn)
    logical_ref = normalize_ref("src", None, "t")
    for field_name, v in (("table_role", "fact"), ("primary_entity", "customer")):
        record_field_evidence(
            conn=passc_conn, logical_ref=logical_ref, field_name=field_name, proposed_value=v,
            producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
            producer_ref="run-1", source_snapshot_id="snap-1", input_hash=f"h-{field_name}")
    out = list_open_table_fact_proposals_governance(passc_conn, "src")
    assert out[0]["advisory"] == {"table_role": "fact", "primary_entity": "customer",
                                  "event_or_snapshot": None}


def test_source_filter_is_normalized(passc_conn):
    _seed_grain(passc_conn)
    assert len(list_open_table_fact_proposals_governance(passc_conn, "  SRC  ")) == 1


def test_excludes_other_sources(passc_conn):
    _seed_grain(passc_conn)
    assert list_open_table_fact_proposals_governance(passc_conn, "other") == []


def test_excludes_non_table_fact_tasks(passc_conn):
    """An open approved_join governance task (Pass C's queue) must never surface here — the two
    confirm surfaces stay disjoint."""
    _propose_join(passc_conn, _join_ref())         # opens TWO platform-admin side tasks
    _, grain_key = _seed_grain(passc_conn)
    out = list_open_table_fact_proposals_governance(passc_conn, "src")
    assert [p["fact_key"] for p in out] == [grain_key]


def test_verified_fact_leaves_the_open_list(passc_conn, human_admin_1):
    _, key = _seed_grain(passc_conn)
    _confirm_via_context(passc_conn, key, human_admin_1)
    assert fold_overlay_state(load_fact(passc_conn, key)).status == "VERIFIED"
    assert list_open_table_fact_proposals_governance(passc_conn, "src") == []


# ── Failure isolation + bounds ───────────────────────────────────────────────────────────────────


def test_corrupt_ref_skipped_without_breaking_queue(passc_conn):
    """A task whose DRAFT claims fact_type 'grain' but whose ref decodes to an ApprovedJoinRef is
    structurally corrupt — skipped with a warning, never aborting the queue."""
    bad_key = "corrupt-grain-fact-key"
    draft = append_overlay_event(
        passc_conn, fact_key=bad_key, type="OVERLAY_FACT_PROPOSED",
        payload={
            # a JOIN ref (column_pairs present): _ref_from_payload yields an ApprovedJoinRef,
            # NOT a table CatalogObjectRef — the reader must skip it, not raise
            "catalog_object_ref": asdict(_join_ref()),
            "object_ref": "public.orphans", "fact_type": "grain", "use_case": None,
            "proposed_value": dict(_GRAIN_VALUE),
            "proposal_fingerprint": "fp-corrupt", "evidence_ref": None,
            "proposed_by": SERVICE_ACTOR.subject},
        actor=SERVICE_ACTOR, expected_version=0)
    open_task(passc_conn, GateTaskSpec(
        gate="OVERLAY_DATA_OWNER", required_inputs=("proposed_value",),
        eligible_assignees={"role": "platform-admin"}, allowed_responses=("confirm", "reject"),
        fact_key=bad_key, draft_event_id=draft.event_id, target_event_id=draft.event_id),
        SERVICE_ACTOR)

    _, good_key = _seed_grain(passc_conn)
    out = list_open_table_fact_proposals_governance(passc_conn, "src")
    assert [p["fact_key"] for p in out] == [good_key]


def test_limit_is_clamped_and_bounds_proposals(passc_conn):
    _seed_grain(passc_conn, table="t")
    _seed_grain(passc_conn, table="t2")
    assert len(list_open_table_fact_proposals_governance(passc_conn, "src")) == 2
    assert len(list_open_table_fact_proposals_governance(passc_conn, "src", limit=1)) == 1
    assert len(list_open_table_fact_proposals_governance(passc_conn, "src", limit=0)) == 1
    assert len(list_open_table_fact_proposals_governance(passc_conn, "src", limit=9999)) == 2


# ── Confirmation context bridge ──────────────────────────────────────────────────────────────────


def test_context_returns_ref_and_a_target_a_real_confirm_accepts(passc_conn, human_admin_1):
    """THE load-bearing wiring: the returned target_event_id must be the exact CAS target
    `confirm_fact` accepts — proven by driving a REAL confirm to VERIFIED with it (four-eyes
    holds: proposer is the service enrichment actor, confirmer a human platform-admin)."""
    ref, key = _seed_grain(passc_conn)
    ctx = load_table_fact_confirmation_context(passc_conn, key)
    assert ctx["fact_type"] == "grain"
    assert ctx["use_case"] is None
    assert isinstance(ctx["ref"], CatalogObjectRef)
    assert ctx["ref"].table == ref.table
    assert ctx["ref"].catalog_source == "src"
    assert ctx["target_event_id"]

    _confirm_via_context(passc_conn, key, human_admin_1)
    assert fold_overlay_state(load_fact(passc_conn, key)).status == "VERIFIED"


def test_context_rejects_non_table_fact(passc_conn):
    """The fact_type gate: an approved_join fact_key must raise (Task 2 maps to 404) — the
    table-fact confirm surface can never become a generic approval endpoint."""
    ref = _join_ref()
    _propose_join(passc_conn, ref)
    join_key = fact_key(ref, "approved_join")
    with pytest.raises(TableFactGovernanceNotFound):
        load_table_fact_confirmation_context(passc_conn, join_key)


def test_context_rejects_unknown_fact_key(passc_conn):
    with pytest.raises(TableFactGovernanceNotFound):
        load_table_fact_confirmation_context(passc_conn, "no-such-fact-key")


def test_context_rejects_undecodable_ref(passc_conn):
    """A DRAFT that CLAIMS fact_type grain but whose catalog_object_ref cannot decode must raise
    (the routes' 404), never 500."""
    bad_key = "ctx-undecodable-grain-fact-key"
    append_overlay_event(
        passc_conn, fact_key=bad_key, type="OVERLAY_FACT_PROPOSED",
        payload={
            # "column_pairs" routes _ref_from_payload down the ApprovedJoinRef arm, where
            # CatalogObjectRef(**{"bogus": ...}) raises TypeError — a real decode exception.
            "catalog_object_ref": {"column_pairs": [], "cardinality": "N:1",
                                   "from_ref": {"bogus": 1}, "to_ref": {"bogus": 2}},
            "object_ref": "public.orphans", "fact_type": "grain", "use_case": None,
            "proposed_value": dict(_GRAIN_VALUE),
            "proposal_fingerprint": "fp-ctx-undecodable", "evidence_ref": None,
            "proposed_by": SERVICE_ACTOR.subject},
        actor=SERVICE_ACTOR, expected_version=0)
    with pytest.raises(TableFactGovernanceNotFound):
        load_table_fact_confirmation_context(passc_conn, bad_key)


# ── Synchronous verified-fact projection ─────────────────────────────────────────────────────────


def test_project_sets_is_grain_on_fresh_watermark(passc_conn, human_admin_1):
    """A just-VERIFIED grain becomes operational SYNCHRONOUSLY: the helper drains the overlay
    projection on this conn (confirm_fact never drains) and projects onto graph_node — under an
    ACTIVE drift guard (sealed config) with a FRESH watermark, resolve_fact serves the fact and
    is_grain lands on exactly the confirmed columns, provenance event id set."""
    from featuregen.overlay.catalog_changes import _write_watermark

    ref, key = _seed_grain(passc_conn)
    _confirm_via_context(passc_conn, key, human_admin_1)     # VERIFIED — projection NOT drained
    _seed_graph_nodes(passc_conn)
    _seal_drift_config()
    now = datetime.now(UTC)
    _write_watermark(passc_conn, "src", now)                 # fresh: within the 60m SLA

    status = project_verified_table_fact(passc_conn, "src", ref, "grain", now=now)
    assert status == "projected"
    flags = _grain_flags(passc_conn)
    assert flags["cif_id"][0] is True and flags["cif_id"][1] is not None
    assert flags["amt"][0] is False


def test_project_pending_on_stale_watermark(passc_conn, human_admin_1):
    """HONEST REPORTING under the drift-freshness guard (the common governance case: an admin
    approves hours after the upload). The fact is VERIFIED but the source watermark is STALE, so
    resolve_fact correctly refuses to serve it — NO flag lands and the helper must say "pending",
    never launder the watermark or claim "projected"."""
    from featuregen.overlay.catalog_changes import _write_watermark

    ref, key = _seed_grain(passc_conn)
    _confirm_via_context(passc_conn, key, human_admin_1)
    _seed_graph_nodes(passc_conn)
    _seal_drift_config()
    now = datetime.now(UTC)
    _write_watermark(passc_conn, "src", now - timedelta(hours=2))   # stale vs the 60m SLA

    status = project_verified_table_fact(passc_conn, "src", ref, "grain", now=now)
    assert status == "pending"
    assert not any(g for g, _e in _grain_flags(passc_conn).values())


def test_project_is_fail_soft(passc_conn, human_admin_1, monkeypatch):
    """A projector fault must NEVER raise out of the confirm response — "pending" is returned and
    the fact stays VERIFIED (the next caught-up ingest re-projects it)."""
    ref, key = _seed_grain(passc_conn)
    _confirm_via_context(passc_conn, key, human_admin_1)
    _seed_graph_nodes(passc_conn)

    def _boom(*args, **kwargs):
        raise RuntimeError("projector exploded")

    monkeypatch.setattr(table_fact_governance, "project_table_facts_for_ref", _boom)
    assert project_verified_table_fact(passc_conn, "src", ref, "grain", now=None) == "pending"
    assert fold_overlay_state(load_fact(passc_conn, key)).status == "VERIFIED"


def test_project_pending_when_projection_cannot_drain(passc_conn, monkeypatch):
    """The residual-lag fallback: when the drain cannot reach head (a poison-HALTED projection),
    resolve_fact could read a stale read model — defer ("pending") rather than project a lie."""
    ref, _key = _seed_grain(passc_conn)                      # the propose appended events
    monkeypatch.setattr(table_fact_governance, "run_projection", lambda conn, projection: 0)
    assert project_verified_table_fact(passc_conn, "src", ref, "grain", now=None) == "pending"
