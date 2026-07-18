"""Slice-2 Task 2: the STALE-VALUE LIFECYCLE for the Pass B advisory table fields.

When a re-upload's synthesis DROPS an advisory field (``table_role`` / ``primary_entity`` /
``event_or_snapshot``), producer-scope staling the LLM evidence is NOT enough:
``resolve_and_project`` iterates only fields WITH active evidence, so the previous ``graph_node``
display value would stay visible forever. ``_propose_table_facts`` now retires the LLM's prior
rows and — when NO active evidence remains for the field — calls ``stale_and_clear_field``: a
STALED decision (supersedes read from the DURABLE decision log, [F2] — the ``graph_node`` link is
NULL after ``build_graph`` rebuilds the node) and a CLEARED flat display column, with the
``*_decision_id`` link repointed at the STALED decision (non-NULL by design). The stored decision
enum is lowercase ``"staled"`` ([F14]). ``prior_value_staled`` on the run's disposition records is
driven by the staled COUNT in BOTH directions ([F9]) and is DECOUPLED from the clear-gate — a
human confirmation keeps the field alive while the LLM rows stale.
"""
import json

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import FieldDecisionEventType, read_field_decisions
from featuregen.overlay.field_evidence import read_active_field_evidence, record_field_evidence
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.table_synth import _propose_table_facts, make_ref_accept

_REF = normalize_ref("src", None, "txn")


def _seed_table_node(conn) -> None:
    """The public-flattened TABLE graph node build_graph would create for src/txn."""
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, is_grain, is_as_of)"
        " VALUES ('src', 'public.txn', 'table', 'txn', false, false)")


def _round(conn, raw: dict, *, actor, snapshot: str) -> list[dict]:
    """One Pass B round over the RAW synthesis: validate/normalize through the real ref-aware
    accept (which collects the run's dispositions), route through ``_propose_table_facts`` with
    that SAME collector, then ``resolve_and_project`` — the exact in-transaction order the ingest
    Pass B savepoint uses (propose THEN project), so the round-2 assertions prove the clear
    survives the projection rather than being re-projected away."""
    dispositions: list[dict] = []
    accept = make_ref_accept({"txn": {"id", "posted_at"}}, dispositions=dispositions)
    out, _verdict = accept(json.dumps(raw), "txn")
    assert out is not None
    _propose_table_facts(conn, "src", {"txn": json.loads(out)}, actor=actor,
                         source_snapshot_id=snapshot, dispositions=dispositions)
    resolve_and_project(conn, source="src", logical_refs=[_REF])
    return dispositions


def _graph_row(conn) -> tuple:
    return conn.execute(
        "SELECT table_role, table_role_decision_id FROM graph_node"
        " WHERE catalog_source = 'src' AND object_ref = 'public.txn'").fetchone()


def _active_llm(conn, field: str) -> list:
    return [e for e in read_active_field_evidence(conn, _REF, field)
            if e.producer == EvidenceProducer.LLM.value]


def _disp(dispositions: list[dict], field: str) -> dict:
    return next(r for r in dispositions if r["table"] == "txn" and r["field"] == field)


def test_dropped_advisory_field_is_staled_and_cleared(overlay_conn, service_actor):
    """Round 1 proposes table_role fact+event -> graph shows event_fact. Round 2 re-proposes with
    table_role ABSENT -> the display column clears, the LATEST decision is lowercase "staled" with
    a supersedes link to the round-1 RESOLVED decision, and no active LLM evidence remains."""
    _seed_table_node(overlay_conn)
    _round(overlay_conn, {"grain_columns": [], "table_role": "fact", "event_or_snapshot": "event"},
           actor=service_actor, snapshot="snap-r1")
    role, _link = _graph_row(overlay_conn)
    assert role == "event_fact"                    # round 1 projected the normalized display

    disp = _round(overlay_conn, {"grain_columns": []}, actor=service_actor, snapshot="snap-r2")

    role, link = _graph_row(overlay_conn)
    assert role is None                            # display column CLEARED, not re-projected away
    decisions = read_field_decisions(overlay_conn, _REF, "table_role")
    latest = decisions[-1]
    assert latest.event_type == "staled"           # [F14] the stored enum is LOWERCASE
    assert latest.event_type == FieldDecisionEventType.STALED.value
    # [F2] supersedes is read from the durable log: it points at the round-1 RESOLVED decision.
    assert latest.supersedes_event_id is not None
    resolved_ids = {d.decision_event_id for d in decisions if d.event_type == "resolved"}
    assert latest.supersedes_event_id in resolved_ids
    # The link is repointed AT the STALED decision — non-NULL by design (audit reaches the staling).
    assert link == latest.decision_event_id
    assert _active_llm(overlay_conn, "table_role") == []   # no active LLM evidence remains
    # [F9] dropped-direction: the disposition is marked from the staled COUNT.
    rec = _disp(disp, "table_role")
    assert rec["status"] == "abstained" and rec["prior_value_staled"] is True


def test_present_value_replacing_older_marks_prior_value_staled(overlay_conn, service_actor):
    """[F9] present-direction: a round-2 PRESENT value that supersedes the round-1 rows flips
    ``prior_value_staled`` on the ACCEPTED disposition; no STALED decision, no clearing."""
    _seed_table_node(overlay_conn)
    disp1 = _round(overlay_conn, {"grain_columns": [], "table_role": "dimension"},
                   actor=service_actor, snapshot="snap-r1")
    assert _disp(disp1, "table_role")["prior_value_staled"] is False   # nothing superseded yet

    disp2 = _round(overlay_conn, {"grain_columns": [], "table_role": "reference"},
                   actor=service_actor, snapshot="snap-r2")

    rec = _disp(disp2, "table_role")
    assert rec["status"] == "accepted" and rec["prior_value_staled"] is True
    role, _link = _graph_row(overlay_conn)
    assert role == "reference"                     # the NEW value projects; nothing cleared
    assert read_field_decisions(overlay_conn, _REF, "table_role")[-1].event_type == "resolved"


def test_staled_decision_uses_the_threaded_now(overlay_conn, service_actor):
    """Minor-1: the STALED decision carries the SAME threaded ``now`` as the round's sibling
    RESOLVED decisions (``read_field_decisions`` orders by ``created_at`` — a wall-clock STALED
    row under a future-dated round ``now`` would misorder the history). ``_propose_table_facts``
    threads ``now`` through to ``stale_and_clear_field``; unset keeps the wall-clock default."""
    from datetime import UTC, datetime, timedelta

    _seed_table_node(overlay_conn)
    _round(overlay_conn, {"grain_columns": [], "table_role": "dimension"},
           actor=service_actor, snapshot="snap-r1")

    future = datetime.now(UTC) + timedelta(days=7)
    dispositions: list[dict] = []
    accept = make_ref_accept({"txn": {"id", "posted_at"}}, dispositions=dispositions)
    out, _verdict = accept(json.dumps({"grain_columns": []}), "txn")   # table_role DROPPED
    _propose_table_facts(overlay_conn, "src", {"txn": json.loads(out)}, actor=service_actor,
                         source_snapshot_id="snap-r2", dispositions=dispositions, now=future)
    resolve_and_project(overlay_conn, source="src", logical_refs=[_REF], now=future)

    latest = read_field_decisions(overlay_conn, _REF, "table_role")[-1]
    assert latest.event_type == "staled"
    assert latest.created_at == future           # the round's now, not wall-clock


def test_human_confirmation_keeps_field_alive_while_llm_rows_stale(overlay_conn, service_actor):
    """[F9] the clear-gate decoupling: with a HUMAN confirmation active, a dropped advisory field
    still stales its LLM rows (disposition marked) but records NO staled decision — the field
    resolves from the surviving human evidence and its display stays projected."""
    _seed_table_node(overlay_conn)
    _round(overlay_conn, {"grain_columns": [], "table_role": "dimension"},
           actor=service_actor, snapshot="snap-r1")
    record_field_evidence(
        overlay_conn, logical_ref=_REF, field_name="table_role", proposed_value="reference",
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="user:sme", source_snapshot_id="snap-human", input_hash="h" * 64)

    disp = _round(overlay_conn, {"grain_columns": []}, actor=service_actor, snapshot="snap-r2")

    assert _disp(disp, "table_role")["prior_value_staled"] is True   # the LLM rows WERE staled...
    assert _active_llm(overlay_conn, "table_role") == []
    # ...but the human confirmation blocks the clear: latest decision resolved, display projected.
    assert read_field_decisions(overlay_conn, _REF, "table_role")[-1].event_type == "resolved"
    role, _link = _graph_row(overlay_conn)
    assert role == "reference"


# --- full-ingest two-round proof of [F2] + the in-transaction ordering ----------------------------


def _uploader():
    from featuregen.contracts.envelopes import IdentityEnvelope
    return IdentityEnvelope(subject="user:uploader", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _rows():
    from featuregen.overlay.upload.canonical import CanonicalRow
    return [CanonicalRow("src", "txn", "id", "integer"),
            CanonicalRow("src", "txn", "posted_at", "timestamp")]


def _client(synthesis: dict):
    from featuregen.intake.llm import FakeLLM, FakeResponse
    return FakeLLM(script={"table_synth": FakeResponse(
        output={"results": [{"ref": "txn", "synthesis": synthesis}]})})


def test_full_ingest_reupload_stales_and_clears_after_graph_rebuild(db, monkeypatch):
    """[F2] end-to-end: ``build_graph`` DELETEs+recreates ``graph_node`` (link columns NULL)
    BEFORE Pass B, so ``supersedes_event_id`` MUST come from the durable decision log — and the
    staling+clear runs in the same transaction BEFORE ``resolve_and_project``, which then skips
    the evidence-less field instead of re-projecting the dropped value into the fresh node."""
    from featuregen.overlay.upload.ingest import ingest_upload

    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    r1 = ingest_upload(db, "src", _rows(), actor=_uploader(), client=_client(
        {"grain_columns": ["id"], "as_of_column": "posted_at", "as_of_basis": "posted_at",
         "table_role": "fact", "event_or_snapshot": "event"}))
    assert r1.status == "ingested"
    role, _link = _graph_row(db)
    assert role == "event_fact"

    r2 = ingest_upload(db, "src", _rows(), actor=_uploader(), client=_client(
        {"grain_columns": ["id"], "as_of_column": "posted_at", "as_of_basis": "posted_at",
         "event_or_snapshot": "event"}))            # table_role DROPPED on the re-upload
    assert r2.status == "ingested"

    role, link = _graph_row(db)
    assert role is None                             # the dropped value did NOT survive the re-upload
    latest = read_field_decisions(db, _REF, "table_role")[-1]
    assert latest.event_type == "staled"            # [F14] lowercase
    assert latest.supersedes_event_id is not None   # [F2] read from the log AFTER the graph rebuild
    assert link == latest.decision_event_id
    assert _active_llm(db, "table_role") == []
    # The still-present advisory field kept its normal lifecycle: re-projected into the fresh node.
    eos = db.execute("SELECT event_or_snapshot FROM graph_node"
                     " WHERE catalog_source = 'src' AND object_ref = 'public.txn'").fetchone()[0]
    assert eos == "event"
