from featuregen.governance.replay import ArtifactReplayStatus, ReplayMode, replay_run


def _seed_event(db, run_id):
    db.execute(
        "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id, type, "
        "schema_version, table_version, actor, payload, provenance, occurred_at) "
        "VALUES (%s,'run',%s,1,%s,'RUN_OPENED',1,1,"
        '\'{"subject":"s","actor_kind":"service","authenticated":true,'
        '"auth_method":"workload-identity","role_claims":[]}\'::jsonb, \'{}\'::jsonb, '
        '\'{"artifact_type":"DRAFT_CONTRACT","schema_version":1,"producing_component":"featuregen@1"}\'::jsonb, now())',
        ("evt_" + run_id, run_id, run_id),
    )


def _blob(db, blob_id, classification, status):
    db.execute(
        "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, kms_key_id, status) "
        "VALUES (%s, %s, 'sha256:x', %s, 'k', %s)",
        (blob_id, "k/" + blob_id, classification, status),
    )


def _doc(db, doc_id, run_id, stage, body_ref):
    db.execute(
        "INSERT INTO documents (doc_id, run_id, stage, schema_version, branch_role, content_hash, "
        "body_classification, actor, provenance, body_ref) "
        "VALUES (%s, %s, %s, 1, 'primary', 'sha256:x', %s, '{}'::jsonb, '{}'::jsonb, %s)",
        (
            doc_id,
            run_id,
            stage,
            "pii-erasable" if "p" in doc_id else "governance-retained",
            body_ref,
        ),
    )


def test_full_replay_when_all_bodies_intact(db):
    _seed_event(db, "run_full")
    _blob(db, "blob_ok", "pii-erasable", "live")
    _doc(db, "doc_p_ok", "run_full", "DRAFT_CONTRACT", "blob_ok")
    _doc(db, "doc_meta", "run_full", "ASSUMPTION_LEDGER", None)  # metadata-only, no body

    result = replay_run(db, "run_full")
    assert result.mode is ReplayMode.FULL
    assert result.degraded_artifacts == ()
    assert len(result.events) == 1
    assert all(isinstance(a, ArtifactReplayStatus) and a.intact for a in result.artifacts)


def test_privacy_degraded_replay_labels_shredded_artifacts(db):
    _seed_event(db, "run_deg")
    _blob(db, "blob_shred", "pii-erasable", "shredded")
    _blob(db, "blob_gov", "governance-retained", "live")
    _doc(db, "doc_p_shred", "run_deg", "DRAFT_CONTRACT", "blob_shred")
    _doc(db, "doc_g_keep", "run_deg", "CONFIRMED_CONTRACT", "blob_gov")

    result = replay_run(db, "run_deg")
    assert result.mode is ReplayMode.PRIVACY_DEGRADED
    assert result.degraded_artifacts == ("doc_p_shred",)
    degraded = {a.doc_id: a for a in result.artifacts}
    assert degraded["doc_p_shred"].intact is False
    assert "shred" in (degraded["doc_p_shred"].degraded_reason or "")
    assert degraded["doc_g_keep"].intact is True
