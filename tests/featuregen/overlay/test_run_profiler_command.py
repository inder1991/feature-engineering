"""Task 6.3 — the `run_profiler` service command.

`run_profiler` runs `run_profiler_scan` and, for each candidate, performs a STREAM-BASED preflight
(round-5 finding 2): it reads the authoritative event stream (`load_fact` + `fold_overlay_state`),
NOT the asynchronous `overlay_proposal` projection, and skips BEFORE writing evidence when a
non-terminal fact exists or when the fact is REJECTED with the same `proposal_fingerprint`. That
ordering guarantees the profiler never leaves orphan evidence for a candidate `propose_fact` would
deny. Because the preflight is stream-based, the REJECTED-dedup test seeds the EVENT STREAM (the
authoritative source), not the projection.
"""
from dataclasses import asdict

import psycopg
import pytest
from tests.featuregen._helpers import mint_test_service_identity
from tests.featuregen.overlay._helpers import StubCatalog, catalog_columns

from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import (
    current_authorizer,
    register_command_authorizer,
)
from featuregen.contracts import Command
from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
from featuregen.overlay.catalog import (
    _clear_catalog_adapter,
    register_catalog_adapter,
)
from featuregen.overlay.identity import (
    CatalogObjectRef,
    display_object_ref,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.profiler import PROFILE_VERSION, ProfilerLimits, run_profiler_scan
from featuregen.overlay.store import append_overlay_event
from featuregen.runtime.handlers import HandlerRegistry


@pytest.fixture(autouse=True)
def _reset_process_globals():
    # _setup_overlay registers a process-wide catalog adapter and command authorizer; restore both
    # after each test so neither leaks into a later test that expects fail-closed defaults.
    saved_authorizer = current_authorizer()
    try:
        yield
    finally:
        _clear_catalog_adapter()
        register_command_authorizer(saved_authorizer)


def _service_actor():
    return mint_test_service_identity(
        subject="service:profiler",
        role_claims=("overlay",),
        attestation="att-profiler-1",
        groups=(),
    )


def _setup_overlay(conn, adapter):
    register_overlay(HandlerRegistry())
    seed_authz_policy(conn)
    seed_overlay_authz(conn)
    register_command_authorizer(PolicyAuthorizer())
    register_catalog_adapter(adapter)


def _run_profiler_cmd(ref):
    return Command(
        action="run_profiler",
        aggregate="overlay_fact",
        aggregate_id=f"{ref.schema}.{ref.table}",
        args={"ref": asdict(ref), "allowed_schemas": ["public"]},
        actor=_service_actor(),
        idempotency_key=f"runprof:{ref.schema}.{ref.table}:1",
    )


def _proposed_for(conn, fk):
    rows = conn.execute(
        "SELECT event_id FROM events WHERE overlay_fact_id = %s "
        "AND type = 'OVERLAY_FACT_PROPOSED' ORDER BY stream_version",
        (fk,),
    ).fetchall()
    return [r[0] for r in rows]


def _evidence_count(conn, fk):
    return conn.execute(
        "SELECT count(*) FROM overlay_evidence WHERE fact_key = %s", (fk,)
    ).fetchone()[0]


def test_run_profiler_off_allowlist_schema_denied_and_audited(db):
    """F6(b): an off-allowlist target must yield a CLEAN, audited CommandResult denial — NOT a
    SchemaNotAllowedError propagating out of execute_command. §6.5 maps the allowlist check to the
    security-audit stream, but authz_policy only checks capability+kind (not the schema), so the
    handler must record the denial itself. Pre-fix execute_command raises the exception."""
    ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="restricted", table="secrets"
    )
    adapter = StubCatalog(objects=catalog_columns(ref, [("id", "integer")]), owners={})
    _setup_overlay(db, adapter)

    result = execute_command(db, _run_profiler_cmd(ref))  # must NOT raise
    assert result.accepted is False
    assert "allowlist" in (result.denied_reason or "").lower()
    # §6.5: the denied attempt is recorded in the security-audit stream.
    n = db.execute(
        "SELECT count(*) FROM security_audit "
        "WHERE attempted_action = 'run_profiler' AND decision = 'denied'"
    ).fetchone()[0]
    assert n == 1


def test_profiler_scan_runs_read_only(db, monkeypatch):
    """F6(a): the scan phase runs under an in-code read-only guard (defense-in-depth for §5.2's
    read-only role), while the SUBSEQUENT propose_fact write phase still succeeds in the SAME
    transaction. A write attempted DURING the scan is rejected with ReadOnlySqlTransaction (probed
    inside its own savepoint so the abort is contained); the scan then completes and drafts are
    proposed. Pre-fix the probe write succeeds (no read-only mode set) -> pytest.raises fails."""
    import featuregen.overlay.profiler as prof

    real = prof._profile_single
    probed = {"checked": False}

    def probing(conn, ref, column, *, sample):
        if not probed["checked"]:
            probed["checked"] = True
            conn.execute("SAVEPOINT _probe")
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("CREATE TEMP TABLE _probe_write(i int)")  # write during scan must fail
            conn.execute("ROLLBACK TO SAVEPOINT _probe")  # contain the aborted subtransaction
        return real(conn, ref, column, sample=sample)

    monkeypatch.setattr(prof, "_profile_single", probing)

    db.execute("CREATE TABLE prof_run_ro (account_id integer)")
    db.execute("INSERT INTO prof_run_ro SELECT g FROM generate_series(1, 30) AS g")
    ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="public", table="prof_run_ro"
    )
    adapter = StubCatalog(
        objects=catalog_columns(ref, [("account_id", "integer")]),
        owners={("public", "prof_run_ro"): "user:owner-ro"},
    )
    _setup_overlay(db, adapter)

    result = execute_command(db, _run_profiler_cmd(ref))
    assert result.accepted is True          # write phase (propose_fact) still works after the scan
    assert result.produced_event_ids        # drafts were proposed
    assert probed["checked"] is True        # the read-only probe actually ran


def test_run_profiler_proposes_new_drafts(db):
    db.execute("CREATE TABLE prof_run_a (account_id integer, region text)")
    db.execute("INSERT INTO prof_run_a SELECT g, 'eu' FROM generate_series(1, 30) AS g")
    ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="public", table="prof_run_a"
    )
    adapter = StubCatalog(
        objects=catalog_columns(ref, [("account_id", "integer"), ("region", "text")]),
        owners={("public", "prof_run_a"): "user:owner-a"},
    )
    _setup_overlay(db, adapter)

    result = execute_command(db, _run_profiler_cmd(ref))
    assert result.accepted is True

    grain_key = fact_key(ref, "grain")
    assert len(_proposed_for(db, grain_key)) == 1
    assert result.produced_event_ids  # DRAFT event ids surfaced
    # Evidence WAS written for the proposed draft (and is referenced by the proposal).
    assert _evidence_count(db, grain_key) == 1


def test_run_profiler_skips_rejected_fingerprint_even_with_fresh_evidence(db):
    db.execute("CREATE TABLE prof_run_b (account_id integer)")
    db.execute("INSERT INTO prof_run_b SELECT g FROM generate_series(1, 25) AS g")
    ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="public", table="prof_run_b"
    )
    adapter = StubCatalog(
        objects=catalog_columns(ref, [("account_id", "integer")]),
        owners={("public", "prof_run_b"): "user:owner-b"},
    )
    _setup_overlay(db, adapter)

    # Discover the exact fingerprint the profiler will produce for this table.
    limits = ProfilerLimits(allowed_schemas=frozenset({"public"}))
    scan = run_profiler_scan(db, adapter, ref, limits=limits)
    grain = next(p for p in scan if p.fact_type == "grain")
    grain_key = fact_key(ref, "grain")
    rejected_fp = proposal_fingerprint(
        grain.proposed_value,
        profile_version=PROFILE_VERSION,
        thresholds=grain.evidence_metrics["thresholds"],
    )

    # Seed the authoritative EVENT STREAM (NOT the projection) with a PROPOSED->REJECTED carrying
    # that fingerprint — the same events propose_fact/reject_fact would emit. The stream-based
    # preflight reads exactly this.
    actor = _service_actor()
    draft = append_overlay_event(
        db,
        fact_key=grain_key,
        type="OVERLAY_FACT_PROPOSED",
        payload={
            "catalog_object_ref": asdict(ref),
            "object_ref": display_object_ref(ref),
            "fact_type": "grain",
            "use_case": None,
            "proposed_value": dict(grain.proposed_value),
            "proposal_fingerprint": rejected_fp,
            "evidence_ref": None,
            "proposed_by": actor.subject,
        },
        actor=actor,
        expected_version=0,
    )
    append_overlay_event(
        db,
        fact_key=grain_key,
        type="OVERLAY_FACT_REJECTED",
        payload={
            "rejected_by": "user:owner-b",
            "reason": "not the grain",
            "target_event_id": draft.event_id,
            "retired_fingerprint": rejected_fp,
        },
        actor=actor,
        caused_by=draft.event_id,
    )

    proposed_before = _proposed_for(db, grain_key)  # the single seeded draft

    result = execute_command(db, _run_profiler_cmd(ref))
    assert result.accepted is True
    # Fresh evidence does NOT revive the rejected candidate: no NEW DRAFT proposed.
    assert _proposed_for(db, grain_key) == proposed_before
    assert result.produced_event_ids == ()
    # round-5 finding 2: no ORPHAN evidence is written for the skipped candidate.
    assert _evidence_count(db, grain_key) == 0


def test_run_profiler_no_orphan_evidence_when_fact_created_concurrently(db, monkeypatch):
    """P2a: a concurrent tx commits a non-terminal fact for the same fact_key AFTER the profiler's
    preflight read but BEFORE propose_fact's load_fact. The preflight (modeled by the monkeypatch
    returning a stale empty snapshot) lets the candidate through the skip gates; propose_fact then
    sees the seeded DRAFT and denies. No evidence row may be left behind: propose_fact now owns the
    evidence INSERT and performs it only AFTER the replacement-semantics gates pass, so a denied
    proposal writes NOTHING. Pre-fix the profiler pre-wrote evidence before proposing -> orphan."""
    import featuregen.overlay.profiler_command as profiler_command

    db.execute("CREATE TABLE prof_run_c (account_id integer)")
    db.execute("INSERT INTO prof_run_c SELECT g FROM generate_series(1, 30) AS g")
    ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="public", table="prof_run_c"
    )
    adapter = StubCatalog(
        objects=catalog_columns(ref, [("account_id", "integer")]),
        owners={("public", "prof_run_c"): "user:owner-c"},
    )
    _setup_overlay(db, adapter)
    grain_key = fact_key(ref, "grain")

    # A concurrent transaction committed a non-terminal DRAFT for grain_key. Seed it on the
    # AUTHORITATIVE event stream with a DIFFERENT fingerprint than the profiler will produce.
    actor = _service_actor()
    append_overlay_event(
        db,
        fact_key=grain_key,
        type="OVERLAY_FACT_PROPOSED",
        payload={
            "catalog_object_ref": asdict(ref),
            "object_ref": display_object_ref(ref),
            "fact_type": "grain",
            "use_case": None,
            "proposed_value": {"columns": ["account_id"], "is_unique": True},
            "proposal_fingerprint": "other-fp",
            "evidence_ref": None,
            "proposed_by": actor.subject,
        },
        actor=actor,
        expected_version=0,
    )

    # Model the READ COMMITTED race: the profiler's preflight ran on an EARLIER snapshot that did not
    # yet see the concurrent commit, so it returns (None, None) for grain_key the first time and
    # lets the candidate past the skip gates into propose_fact.
    real_fp = profiler_command._existing_proposal_fingerprint
    seen = {"grain_first": True}

    def stale_preflight(conn, fk):
        if fk == grain_key and seen["grain_first"]:
            seen["grain_first"] = False
            return (None, None)
        return real_fp(conn, fk)

    monkeypatch.setattr(profiler_command, "_existing_proposal_fingerprint", stale_preflight)

    result = execute_command(db, _run_profiler_cmd(ref))
    assert result.accepted is True

    # propose_fact's real load_fact sees the seeded DRAFT (non-terminal) and denies BEFORE minting
    # evidence -> no orphan. Pre-fix the profiler had already written an evidence row by this point.
    assert _evidence_count(db, grain_key) == 0
    # No NEW DRAFT was proposed for the already-occupied fact_key (only the seeded one remains).
    assert len(_proposed_for(db, grain_key)) == 1
