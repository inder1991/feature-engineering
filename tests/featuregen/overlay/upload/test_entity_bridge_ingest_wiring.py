"""Wiring the cross-catalog entity bridge into ingest.

Everything needed to link a customer in one catalog to the same customer in another already existed
— `derive_bridge_candidates` finds the pairs, `propose_bridge` puts them on the governed fact spine,
`entity_bridge_candidate_evidence` is the durable ledger, and `entity_bridge_edge` is the VERIFIED
projection the planner reads. Nothing called any of it. Against two real catalogs the derivation
returns 9 candidates, and all 9 evaporated when the process exited.

This stage closes that gap: after evidence resolution has populated `graph_node.concept` (the
derivation's only input), derive and propose. It is a PROPOSAL stage — it never confirms, so the
invariant that only a VERIFIED fact is operational is untouched.

The stage is ADVISORY: a failure here must never fail an upload that has already stored its facts
and graph.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.identity import EntityBridgeRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.bridge_store import withdraw_missing_candidate_assessments
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.cross_catalog_links import cross_catalog_links
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.ingest import entity_bridges_enabled, ingest_upload
from featuregen.overlay.upload.stage_report import StageRecorder
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter
from featuregen.projections.runner import projection_lag

_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="owner", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _rows(source: str, table: str, id_col: str) -> list[CanonicalRow]:
    return [CanonicalRow(source=source, table=table, column=id_col, type="text"),
            CanonicalRow(source=source, table=table, column="amount", type="numeric")]


def _seed_two_catalogs(db, *, actor: IdentityEnvelope | None = None) -> None:
    """Two catalogs whose identifier columns carry the SAME entity concept — the shape that must
    bridge. Concepts are set directly: this test is about the WIRING, not about enrichment."""
    who = actor or _actor()
    for source, table, id_col in (("cat_a", "customer", "cust_num"),
                                  ("cat_b", "txn", "cif_id")):
        ingest_upload(db, source, _rows(source, table, id_col), actor=who, now=_NOW)
        db.execute(
            "UPDATE graph_node SET concept = 'customer_id', declared_type = 'varchar' "
            "WHERE catalog_source = %s AND kind = 'column' AND column_name = %s",
            (source, id_col))


def _trigger(db, *, actor: IdentityEnvelope | None = None,
             ingestion_run_id: str | None = None):
    """Upload an UNRELATED third catalog to drive the stage.

    `ingest_upload` wipes and rebuilds the uploading catalog's graph nodes, so re-uploading `cat_b`
    here would erase the concept this test just set and there would be no pair left to find. In
    production the concept survives that rebuild because `resolve_and_project` re-projects it from
    `field_evidence` after `build_graph` — machinery this test deliberately does not stand up.

    Triggering from a third catalog keeps the two seeded endpoints intact and exercises exactly what
    the wiring claims: the stage runs on EVERY upload and derives GLOBALLY, not scoped to the catalog
    being uploaded.
    """
    return ingest_upload(db, "cat_c", _rows("cat_c", "other", "some_id"),
                         actor=actor or _actor(), now=_NOW, ingestion_run_id=ingestion_run_id)


def _ledger(db) -> list[tuple]:
    return db.execute(
        "SELECT entity_id, left_catalog_source, left_object_ref, right_catalog_source, "
        "       right_object_ref FROM entity_bridge_candidate_evidence ORDER BY 1,2,3").fetchall()


# ── the flag ─────────────────────────────────────────────────────────────────────────────────────

def test_the_stage_is_off_by_default(monkeypatch):
    monkeypatch.delenv("OVERLAY_ENTITY_BRIDGES", raising=False)
    assert entity_bridges_enabled() is False


def test_flag_off_writes_no_candidate(db, monkeypatch):
    """Flag-off must be byte-identical to today: no ledger row, no fact."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "0")
    _seed_two_catalogs(db)
    assert _ledger(db) == []


# ── the wiring ───────────────────────────────────────────────────────────────────────────────────

def test_the_second_catalog_s_upload_persists_the_bridge(db, monkeypatch):
    """THE point of the change. Before this, the derivation produced 9 candidates that were never
    written anywhere; now uploading the second catalog leaves a durable ledger row."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    _trigger(db)
    ledger = _ledger(db)
    assert len(ledger) == 1, ledger
    entity, left_src, left_ref, right_src, right_ref = ledger[0]
    assert entity == "customer"
    assert {left_src, right_src} == {"cat_a", "cat_b"}
    assert "cust_num" in left_ref + right_ref and "cif_id" in left_ref + right_ref


def test_a_bridge_is_PROPOSED_never_verified(db, monkeypatch):
    """The governing invariant: this stage proposes. Only a human confirm makes a bridge
    operational, so nothing may land in the VERIFIED projection."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    _trigger(db)
    assert _ledger(db), "precondition: a candidate was proposed"
    assert db.execute("SELECT count(*) FROM entity_bridge_edge").fetchone()[0] == 0


def test_a_single_catalog_proposes_nothing(db, monkeypatch):
    """A bridge is cross-catalog by definition; one catalog alone must produce none."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    ingest_upload(db, "cat_a", _rows("cat_a", "customer", "cust_num"), actor=_actor(), now=_NOW)
    db.execute("UPDATE graph_node SET concept = 'customer_id' WHERE kind='column' "
               "AND column_name = 'cust_num'")
    ingest_upload(db, "cat_a", _rows("cat_a", "customer", "cust_num"), actor=_actor(), now=_NOW)
    assert _ledger(db) == []


def test_re_uploading_does_not_duplicate_the_candidate(db, monkeypatch):
    """The ledger is keyed on the pair, and the fact spine dedups on the deterministic fact_key, so
    a re-upload is a no-op rather than a second proposal."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    for _ in range(3):
        _trigger(db)
    assert len(_ledger(db)) == 1


# ── advisory: never fail the upload ──────────────────────────────────────────────────────────────

def test_a_failure_in_the_stage_does_not_fail_the_upload(db, monkeypatch):
    """The facts and graph are already stored by this point. A bridge fault must degrade to a
    warning, exactly as the sibling Pass C / governed-join seams do."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)

    def boom(*a, **k):
        raise RuntimeError("derivation exploded")

    monkeypatch.setattr(
        "featuregen.overlay.upload.ingest.derive_bridge_candidates_with_report",
        boom,
    )
    result = _trigger(db)
    assert result.status == "ingested"
    assert _ledger(db) == []


def test_the_count_is_reported_on_the_result(db, monkeypatch):
    """Observability: the count rides the IngestResult like every sibling stage's, so the run
    report can show it instead of it being discoverable only by querying the table."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    result = _trigger(db)
    assert result.entity_bridges_proposed == 1
    assert result.entity_bridge_candidates_considered == 1
    assert result.entity_bridge_candidates_retained == 1
    assert result.entity_bridge_candidates_suppressed == 0
    assert result.entity_bridge_candidates_truncated == 0


def test_bridge_proposals_do_not_leave_the_governed_projection_lagged(db, monkeypatch):
    """The bridge stage runs after the ordinary ingest drain because it needs the freshly
    re-projected grain facts.  Its DRAFT events must still be drained before the upload returns:
    catalog readiness fails closed on *global* overlay lag, so leaving even one bridge event behind
    makes every role on every uploaded column appear unavailable."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)

    _trigger(db)

    assert _ledger(db), "precondition: the trigger proposed a bridge"
    assert projection_lag(db, "overlay") == 0


def test_reingest_withdraws_suppressed_candidates_and_reappearance_reactivates_them(
    db, monkeypatch
):
    """The current shortlist must follow the latest graph, not accumulate every historical
    proposal forever.  Withdrawal changes only the current pointer: immutable assessment/event
    history remains and the same candidate can become current again."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    _trigger(db)
    candidate_id = derive_bridge_candidates(db)[0].candidate_id
    assert len(cross_catalog_links(db)) == 1
    original_task = db.execute(
        "SELECT task_id FROM human_tasks WHERE status='open'"
    ).fetchone()
    assert original_task is not None

    db.execute(
        "UPDATE graph_node SET concept='unrelated_identifier' "
        "WHERE catalog_source='cat_b' AND column_name='cif_id'"
    )
    run_id = _open_run(db, "ingrun_withdraw_bridge", _actor())
    recorder = StageRecorder()
    ingest_upload(
        db,
        "cat_c",
        _rows("cat_c", "other", "some_id"),
        actor=_actor(),
        now=_NOW,
        ingestion_run_id=run_id,
        stage_recorder=recorder,
    )

    assert cross_catalog_links(db) == ()
    lifecycle = db.execute(
        "SELECT lifecycle FROM governed_candidate_current WHERE candidate_id=%s",
        (candidate_id,),
    ).fetchone()
    assert lifecycle == ("withdrawn",)
    stage = next(item for item in recorder.reports if item.stage == "entity_bridges")
    assert stage.detail is not None
    assert stage.detail["withdrawn_count"] == 1
    assert stage.detail["withdrawn_realization_count"] == 0
    assert stage.detail["superseded_review_task_count"] == 1
    assert db.execute(
        "SELECT status FROM human_tasks WHERE task_id=%s",
        (original_task[0],),
    ).fetchone() == ("superseded",)
    assert db.execute(
        "SELECT count(*) FROM human_tasks WHERE status='open'"
    ).fetchone()[0] == 0

    db.execute(
        "UPDATE graph_node SET concept='customer_id' "
        "WHERE catalog_source='cat_b' AND column_name='cif_id'"
    )
    _trigger(db)

    assert len(cross_catalog_links(db)) == 1
    lifecycle = db.execute(
        "SELECT lifecycle FROM governed_candidate_current WHERE candidate_id=%s",
        (candidate_id,),
    ).fetchone()
    assert lifecycle == ("active",)
    new_task = db.execute(
        "SELECT task_id FROM human_tasks WHERE status='open'"
    ).fetchone()
    assert new_task is not None
    assert new_task[0] != original_task[0]


def test_reconciliation_supersedes_tasks_left_by_an_already_withdrawn_pointer(
    db, monkeypatch
):
    """Deploy-time repair: BUG 3 shipped before BUG 4, so a pointer can already be withdrawn while
    its old task is still open. Reconciliation must repair that state without a catalog re-upload."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    _trigger(db)
    candidate_id = derive_bridge_candidates(db)[0].candidate_id
    task_id = db.execute(
        "SELECT task_id FROM human_tasks WHERE status='open'"
    ).fetchone()[0]
    db.execute(
        "UPDATE governed_candidate_current SET lifecycle='withdrawn' "
        "WHERE candidate_id=%s",
        (candidate_id,),
    )

    result = withdraw_missing_candidate_assessments(db, frozenset())

    assert result == (0, 0, 1)
    assert db.execute(
        "SELECT status FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone() == ("superseded",)


def test_withdrawn_candidate_revision_cannot_cancel_its_active_semantic_replacement(
    db, monkeypatch
):
    """Candidate identity is revision-local, but review belongs to the stable semantic fact.

    A derivation-version change can replace a candidate id while retaining the same bridge
    ``fact_key``.  The withdrawn historical id must not cancel the active replacement's review
    task merely because both revisions name the same semantic link.
    """
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    _seed_two_catalogs(db)
    _trigger(db)
    active_candidate_id = derive_bridge_candidates(db)[0].candidate_id
    active_task_id = db.execute(
        "SELECT task_id FROM human_tasks WHERE status='open'"
    ).fetchone()[0]
    db.execute(
        "INSERT INTO governed_candidate_identity "
        "(candidate_id, candidate_family, logical_identity_json) "
        "SELECT 'historical-candidate', candidate_family, logical_identity_json "
        "FROM governed_candidate_identity WHERE candidate_id=%s",
        (active_candidate_id,),
    )
    db.execute(
        "INSERT INTO governed_candidate_revision "
        "(candidate_revision_id, candidate_id, assessment_json, assessment_version) "
        "SELECT 'historical-revision', 'historical-candidate', assessment_json, "
        "       assessment_version "
        "FROM governed_candidate_revision "
        "WHERE candidate_revision_id=("
        "  SELECT candidate_revision_id FROM governed_candidate_current WHERE candidate_id=%s"
        ")",
        (active_candidate_id,),
    )
    db.execute(
        "INSERT INTO governed_candidate_current "
        "(candidate_id, candidate_revision_id, pointer_version, lifecycle) "
        "VALUES ('historical-candidate', 'historical-revision', 1, 'withdrawn')"
    )

    result = withdraw_missing_candidate_assessments(
        db, frozenset({active_candidate_id})
    )

    assert result == (0, 0, 0)
    assert db.execute(
        "SELECT status FROM human_tasks WHERE task_id=%s",
        (active_task_id,),
    ).fetchone() == ("open",)


# ── who proposes: the SYSTEM, never the uploading human ──────────────────────────────────────────
#
# A derived bridge proposed under the UPLOADER's own identity is permanently unconfirmable BY that
# uploader: `proposer_ne_confirmer` denies "a proposer may not confirm the same fact". On a small
# team the uploader IS the reviewer, so every bridge an upload discovers sits in DRAFT forever and
# nothing explains why. Measured on the deployed cluster: 9 bridges, all `proposed_by = user:inder`,
# and `entity_bridge_edge` empty.
#
# The derivation is the SYSTEM speaking. Its only input is the resolved GLOBAL graph across every
# catalog — never the file just uploaded — so the uploading human authored neither endpoint. The
# service actor proposes and a human disposes, exactly as Pass C's `propose_join_candidates` has
# always done one seam below.


def _admin_uploader() -> IdentityEnvelope:
    """An uploader who is ALSO the reviewer — the small-team shape the lockout hit. An owner-less
    `entity_bridge` resolves to the platform-admin governance queue (authority.py), so this is the
    identity that is genuinely entitled to confirm."""
    return IdentityEnvelope(subject="user:inder", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner", "platform-admin"))


def _bridge_ref(db) -> EntityBridgeRef:
    cand = derive_bridge_candidates(db)[0]
    return EntityBridgeRef(cand.entity_id, cand.left_ref, cand.right_ref)


def _confirm_as(db, ref: EntityBridgeRef, actor: IdentityEnvelope):
    key = fact_key(ref, "entity_bridge")
    target = _cas_target(fold_overlay_state(load_fact(db, key)))
    return confirm_fact(db, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "use_case": None, "target_event_id": target},
        actor, f"confirm-{target}"))


def _open_run(db, run_id: str, actor: IdentityEnvelope) -> str:
    db.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, heartbeat_at) VALUES (%s, 'upload', 'cat_c', %s, 'in_progress', %s, %s)",
        (run_id, actor.subject, _NOW, _NOW))
    return run_id


def test_the_uploader_can_confirm_a_bridge_their_own_upload_derived(db, monkeypatch):
    """THE property, stated behaviourally: one human uploads, the upload derives a bridge, and that
    same human confirms it. Before the fix this confirm was DENIED — the uploader was recorded as
    the proposer, so four-eyes barred them from ever disposing of what their upload discovered."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    uploader = _admin_uploader()
    _seed_two_catalogs(db, actor=uploader)
    _trigger(db, actor=uploader)
    ref = _bridge_ref(db)
    key = fact_key(ref, "entity_bridge")
    assert fold_overlay_state(load_fact(db, key)).status == "DRAFT", "precondition: proposed"

    res = _confirm_as(db, ref, uploader)

    assert res.accepted, res.denied_reason
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"


def test_the_recorded_proposer_is_a_service_subject_not_the_uploader(db, monkeypatch):
    """The mechanism behind the test above, pinned so a future edit cannot quietly re-thread the
    human. Also pins the ABSENCE of `source_uploader`: that field arms the SECOND four-eyes gate
    (`uploader_ne_confirmer`) and would restore the identical lockout. It exists for values
    DECLARED in an uploaded file (semantic bindings, Pass B grain) — a bridge is DERIVED."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    uploader = _admin_uploader()
    _seed_two_catalogs(db, actor=uploader)
    _trigger(db, actor=uploader)

    events = load_fact(db, fact_key(_bridge_ref(db), "entity_bridge"))
    proposed = [e for e in events if e.type == "OVERLAY_FACT_PROPOSED"]
    assert len(proposed) == 1
    assert proposed[0].payload["proposed_by"] != uploader.subject
    assert proposed[0].payload["proposed_by"] == _ENRICH_ACTOR.subject
    assert "source_uploader" not in proposed[0].payload


def test_the_originating_upload_stays_recoverable_from_the_audit_trail(db, monkeypatch):
    """Handing the proposal to a service actor must not lose WHICH upload produced the bridge. No
    new field: the pre-minted evidence row's existing `producer_item_ref` column carries the
    durable ingestion run id, and `ingestion_run.actor_subject` resolves that back to the human.
    That is strictly MORE than `proposed_by` ever said — which upload, not just which person."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    uploader = _admin_uploader()
    _seed_two_catalogs(db, actor=uploader)
    run_id = _open_run(db, "ingrun_R1BRIDGE", uploader)

    _trigger(db, actor=uploader, ingestion_run_id=run_id)

    key = fact_key(_bridge_ref(db), "entity_bridge")
    row = db.execute("SELECT producer_item_ref FROM overlay_evidence WHERE fact_key = %s",
                     (key,)).fetchone()
    assert row is not None and row[0] == run_id
    assert db.execute("SELECT actor_subject FROM ingestion_run WHERE id = %s",
                      (run_id,)).fetchone()[0] == uploader.subject


def test_a_bridge_already_proposed_by_a_human_is_left_exactly_as_it_was(db, monkeypatch):
    """The nine bridges already on the cluster were proposed by a human and STAY that way: this
    changes who proposes next, it does not rewrite history. `propose_fact` dedups on the
    deterministic fact_key and `fold_overlay_state` ignores a stray PROPOSED once a fact is past
    DRAFT, so re-derivation is a no-op — the human proposer, and their lockout, persist until the
    fact is rejected and re-proposed. That is the designed outcome, not a regression."""
    monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", "1")
    uploader = _admin_uploader()
    _seed_two_catalogs(db, actor=uploader)
    ensure_upload_catalog_adapter()
    legacy_key = propose_bridge(db, derive_bridge_candidates(db)[0], actor=uploader, now=_NOW)

    _trigger(db, actor=uploader)          # a later upload re-derives the very same pair

    events = load_fact(db, legacy_key)
    proposed = [e for e in events if e.type == "OVERLAY_FACT_PROPOSED"]
    assert len(proposed) == 1, "re-derivation must not append a second proposal"
    assert proposed[0].payload["proposed_by"] == uploader.subject
    assert fold_overlay_state(events).status == "DRAFT"
    # …and it is still locked for that human: only a FRESH proposal carries the service proposer.
    assert not _confirm_as(db, _bridge_ref(db), uploader).accepted


@pytest.mark.parametrize("flag", ["0", None])
def test_flag_off_reports_zero(db, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("OVERLAY_ENTITY_BRIDGES", raising=False)
    else:
        monkeypatch.setenv("OVERLAY_ENTITY_BRIDGES", flag)
    _seed_two_catalogs(db)
    result = _trigger(db)
    assert result.entity_bridges_proposed == 0
    assert result.entity_bridge_candidates_considered == 0
    assert result.entity_bridge_candidates_retained == 0
    assert result.entity_bridge_candidates_suppressed == 0
    assert result.entity_bridge_candidates_truncated == 0
