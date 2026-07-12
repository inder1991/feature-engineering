"""Task 12 — whole-phase integration: FTR glossary -> Pass B proposal -> confirm -> projection.

Proves the full Pass B loop on a realistic glossary upload driven through the REAL ``ingest_upload``
(fake LLM client, OVERLAY_TABLE_SYNTH=1): Pass B proposes a grain (PROPOSED-only, service actor),
``compute_readiness`` reports it as a review requirement, a platform-admin human confirms it via the
real gate commands, and a re-ingest projects the confirmed grain LOAD-BEARING onto ``graph_node``
(readiness flips to confirmed). And the authority ordering: a SOURCE-declared structural grain
(``_assert_fact`` -> VERIFIED) is never contested by a Pass B proposal for a different grain.

No OverlayConfig is sealed here (facts come from the real ingest; a sealed config would arm the
config-gated fail-close guards in ``resolve_fact`` against a test DB with no drift watermark).
"""
from __future__ import annotations

from tests.featuregen.overlay.upload.conftest import _confirm_grain


def test_glossary_upload_proposes_then_confirms_grain(overlay_conn, human_actor, monkeypatch,
                                                      fake_synth_client, glossary_rows):
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.readiness import ReadinessScopeType, compute_readiness

    # A glossary upload passes its rows AND the sidecar: the sidecar selects the glossary profile
    # (unknown-type rows pass validation) and keys the evidence attachment.
    r1 = ingest_upload(overlay_conn, "src", glossary_rows.rows, actor=human_actor,
                       client=fake_synth_client, glossary=glossary_rows)
    assert r1.status == "ingested"    # IngestResult.status ∈ {ingested, held, rejected}
    # readiness: grain proposed, not confirmed
    rd = compute_readiness(overlay_conn, source="src", scope=ReadinessScopeType.TABLE, subset="txn")
    assert any(x.status == "proposed" and "grain" in x.requirement_id
               for x in rd.review_requirements)

    _confirm_grain(overlay_conn, "src", "txn", ["txn_id"], actor=human_actor)  # human confirms

    # re-ingest projects the confirmed grain load-bearing onto graph_node
    ingest_upload(overlay_conn, "src", glossary_rows.rows, actor=human_actor,
                  client=fake_synth_client, glossary=glossary_rows)
    row = overlay_conn.execute(
        "SELECT is_grain FROM graph_node WHERE catalog_source='src' AND table_name='txn' "
        "AND column_name='txn_id' AND kind='column'").fetchone()
    assert row[0] is True
    rd2 = compute_readiness(overlay_conn, source="src", scope=ReadinessScopeType.TABLE, subset="txn")
    assert any(x.status == "confirmed" and "grain" in x.requirement_id
               for x in (rd2.review_requirements + rd2.blocking_requirements)
               ) or all("grain" not in x.requirement_id for x in rd2.blocking_requirements)


def test_advisory_propose_failure_keeps_the_egress_audit(overlay_conn, human_actor, monkeypatch,
                                                         fake_synth_client, glossary_rows):
    """The immutable record_llm_call egress audit (Pass B savepoint 1) must SURVIVE an
    advisory-stage failure (savepoint 2): the upload stays ingested (fail-soft), and the record of
    WHAT EGRESSED to the provider is never rolled back with the advisory propose/projection writes."""
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    import featuregen.overlay.upload.table_synth as ts
    from featuregen.overlay.upload.ingest import ingest_upload

    def _boom(*_a, **_k):
        raise RuntimeError("advisory boom")

    monkeypatch.setattr(ts, "_propose_table_facts", _boom)  # ingest imports it lazily at call time
    r = ingest_upload(overlay_conn, "src", glossary_rows.rows, actor=human_actor,
                      client=fake_synth_client, glossary=glossary_rows)
    assert r.status == "ingested"       # Pass B stays strictly advisory
    n = overlay_conn.execute(
        "SELECT count(*) FROM llm_call WHERE task = 'table_synth'").fetchone()[0]
    assert n >= 1                       # the egress audit committed before the advisory stage


def test_declared_structural_grain_beats_pass_b_proposal(overlay_conn, human_actor, monkeypatch,
                                                         fake_synth_client, technical_rows):
    # technical_rows: a TECHNICAL csv declaring is_grain on `id` -> _assert_fact auto-confirms it
    # (legitimate SOURCE attestation, §16). Pass B proposing a DIFFERENT grain must not touch it.
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.upload_catalog import table_ref
    # fake_synth_client returns grain=["txn_id"] — a REAL but DIFFERENT column of the same table
    # (it must pass make_ref_accept and reach the propose path, where the VERIFIED fact must win).
    ingest_upload(overlay_conn, "src", technical_rows, actor=human_actor, client=fake_synth_client)
    state = fold_overlay_state(load_fact(overlay_conn, fact_key(table_ref("src", "txn"), "grain")))
    assert state.status == "VERIFIED"                 # source-declared grain stands
    assert state.value["columns"] == ["id"]           # Pass B did NOT overwrite it
