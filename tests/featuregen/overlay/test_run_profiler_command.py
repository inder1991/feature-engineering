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

import pytest

from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import (
    current_authorizer,
    register_command_authorizer,
)
from featuregen.contracts import Command
from featuregen.identity.build import build_service_identity
from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
from featuregen.overlay.catalog import (
    CatalogObject,
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


class _Catalog:
    """Minimal in-test CatalogAdapter (Protocol impl); owners keyed on (schema, table)."""

    def __init__(self, objects, owners):
        self._objects = list(objects)
        self._owners = dict(owners)

    def list_objects(self):
        return list(self._objects)

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def owner_of(self, ref):
        return self._owners.get((ref.schema, ref.table))

    def fingerprint(self):
        return {o.object_ref: o for o in self._objects}


def _columns(ref, specs):
    return [
        CatalogObject(
            object_ref=f"{ref.schema}.{ref.table}.{name}",
            object_kind="column",
            schema=ref.schema,
            table=ref.table,
            column=name,
            data_type=dt,
            native_oid=None,
        )
        for name, dt in specs
    ]


def _service_actor():
    return build_service_identity(
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


def test_run_profiler_proposes_new_drafts(db):
    db.execute("CREATE TABLE prof_run_a (account_id integer, region text)")
    db.execute("INSERT INTO prof_run_a SELECT g, 'eu' FROM generate_series(1, 30) AS g")
    ref = CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema="public", table="prof_run_a"
    )
    adapter = _Catalog(
        _columns(ref, [("account_id", "integer"), ("region", "text")]),
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
    adapter = _Catalog(
        _columns(ref, [("account_id", "integer")]),
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
