"""Task 7: Pass B propose-only fact emission + fail-soft ingest wiring (default OFF).

Grain/availability route through the governed ``propose_fact`` gate — PROPOSED-only (the folded
status literal for a pending proposal is ``DRAFT``, state.py), never auto-confirmed. An existing
VERIFIED fact (a declared/structural grain from ``_assert_fact``) is never contested.
``table_role``/``primary_entity``/``event_or_snapshot`` are advisory field evidence written via the
SAME producer-scoped-staleness helper Pass A uses. The ingest wiring runs only behind
``OVERLAY_TABLE_SYNTH=1`` with a live client, proposes under the SERVICE actor (four-eyes), and is
strictly advisory (savepoint + except — Pass A facts hold).
"""
from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.table_synth import _propose_table_facts
from featuregen.overlay.upload.upload_catalog import table_ref


def test_grain_is_proposed_not_confirmed(overlay_conn, service_actor):
    syn = {"txn": {"grain": {"columns": ["id"], "is_unique": True},
                   "availability_time": None, "table_role": "fact", "primary_entity": "account"}}
    _propose_table_facts(overlay_conn, "src", syn, actor=service_actor,
                         source_snapshot_id="snap-test")
    key = fact_key(table_ref("src", "txn"), "grain")
    state = fold_overlay_state(load_fact(overlay_conn, key))
    # DRAFT is the folded literal for a pending proposal (state.py) — proposed, never auto-confirmed.
    assert state.status == "DRAFT"
    open_tasks = overlay_conn.execute(
        "SELECT count(*) FROM human_tasks WHERE fact_key=%s AND status='open'", (key,)
    ).fetchone()[0]
    assert open_tasks == 1   # human-gated: a governance-queue confirmation task is open


def test_existing_verified_grain_is_not_overwritten(overlay_conn, service_actor, human_actor):
    # simulate a declared/structural grain already VERIFIED (as _assert_fact would leave it)
    from featuregen.overlay.upload.ingest import _assert_fact
    _assert_fact(overlay_conn, "src", "txn", "grain",
                 {"columns": ["id"], "is_unique": True}, actor=human_actor)
    syn = {"txn": {"grain": {"columns": ["other"], "is_unique": True},
                   "availability_time": None, "table_role": None, "primary_entity": None}}
    _propose_table_facts(overlay_conn, "src", syn, actor=service_actor,
                         source_snapshot_id="snap-test")
    state = fold_overlay_state(load_fact(overlay_conn, fact_key(table_ref("src", "txn"), "grain")))
    assert state.status == "VERIFIED" and state.value["columns"] == ["id"]  # untouched


def test_advisory_table_role_recorded_as_evidence(overlay_conn, service_actor):
    from featuregen.overlay.field_evidence import read_active_field_evidence
    from featuregen.overlay.upload.object_ref import normalize_ref
    syn = {"txn": {"grain": None, "availability_time": None,
                   "table_role": "fact", "primary_entity": "account"}}
    _propose_table_facts(overlay_conn, "src", syn, actor=service_actor,
                         source_snapshot_id="snap-test")
    ref = normalize_ref("src", None, "txn")
    ev = read_active_field_evidence(overlay_conn, ref, "table_role")
    assert any(e.proposed_value == "fact" for e in ev)


# --- ingest wiring (Step 5): default-OFF flag, service-actor proposals, fail-soft ------------------


def _uploader():
    from featuregen.contracts.envelopes import IdentityEnvelope
    return IdentityEnvelope(subject="user:uploader", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _synth_client():
    from featuregen.intake.llm import FakeLLM, FakeResponse
    return FakeLLM(script={"table_synth": FakeResponse(output={"results": [
        {"ref": "txn", "synthesis": {"grain_columns": ["id"], "as_of_column": "posted_at",
                                     "as_of_basis": "posted_at", "table_role": "fact",
                                     "primary_entity": "transaction",
                                     "event_or_snapshot": "event"}}]})})


def _rows():
    from featuregen.overlay.upload.canonical import CanonicalRow
    # No declared grain/as-of: only Pass B can propose them (nothing for _assert_fact to VERIFY).
    return [CanonicalRow("src", "txn", "id", "integer"),
            CanonicalRow("src", "txn", "posted_at", "timestamp")]


def test_flag_off_ingest_never_touches_pass_b(db, monkeypatch):
    """OVERLAY_TABLE_SYNTH unset -> the Pass B block is never entered: the assembler is never
    called and no table-fact stream appears (ingest byte-for-byte unchanged)."""
    import featuregen.overlay.upload.table_synth as ts
    from featuregen.overlay.upload.ingest import ingest_upload

    monkeypatch.delenv("OVERLAY_TABLE_SYNTH", raising=False)
    called: list[bool] = []
    monkeypatch.setattr(ts, "assemble_table_items", lambda *a, **k: called.append(True))
    res = ingest_upload(db, "src", _rows(), actor=_uploader(), client=_synth_client())
    assert res.status == "ingested"
    assert called == []                                               # Pass B never ran
    assert load_fact(db, fact_key(table_ref("src", "txn"), "grain")) == []   # no proposal appeared


def test_ingest_wires_pass_b_behind_flag(db, monkeypatch):
    """Flag ON + live client: ingest runs Pass B — grain/availability land as DRAFT proposals
    under the SERVICE actor (four-eyes vs the human uploader) and the advisory table fields land
    as LLM field evidence."""
    from featuregen.overlay.field_evidence import read_active_field_evidence
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.object_ref import normalize_ref

    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    res = ingest_upload(db, "src", _rows(), actor=_uploader(), client=_synth_client())
    assert res.status == "ingested"

    stream = load_fact(db, fact_key(table_ref("src", "txn"), "grain"))
    state = fold_overlay_state(stream)
    assert state.status == "DRAFT"                                    # proposed, never confirmed
    proposed = [e for e in stream if e.type == "OVERLAY_FACT_PROPOSED"]
    # four-eyes material: the proposer is the SERVICE enrichment actor, not the human uploader.
    assert proposed[0].payload["proposed_by"] == "featuregen-overlay-enrichment"

    avail = fold_overlay_state(load_fact(db, fact_key(table_ref("src", "txn"), "availability_time")))
    assert avail.status == "DRAFT"

    ev = read_active_field_evidence(db, normalize_ref("src", None, "txn"), "table_role")
    # Slice 2: the accept vocab-normalizes the advisory role — "fact" + event -> "event_fact".
    assert any(e.proposed_value == "event_fact" for e in ev)
