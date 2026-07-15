"""Phase 4 Task 1 — the governance analytics read model (dashboard rollups + calibration seed).

`compute_governance_dashboard` folds the governed pipeline's three stores into one read-only
dashboard shape: the `overlay_proposal` enumeration with each fact's LIVE folded status (never
the read-model status column), `human_tasks` queue health (open depth + age buckets), the
`pass_c_candidate_evidence` ledger joined with folded outcomes as the calibration seed, and a
7-day recent-activity window off the CONFIRMED/REJECTED events.

Seeding drives the REAL governance commands (the passc conftest helpers): a VERIFIED join is
propose + dual platform-admin confirm; a REJECTED join is propose + `reject_fact` with a
structured `category`; a DRAFT is propose only; ledger rows are inserted with
`evidence_json = asdict(JoinCandidateEvidenceV1(...))` exactly as Pass C writes them.

THE load-bearing invariant: one corrupt row (a stream-less proposal, an exploding load_fact, a
garbage evidence_json) is skipped + counted — it must NEVER blank the dashboard.
"""
# ruff: noqa: F811 — the passc conftest fixtures are IMPORTED by name (this module lives outside
# tests/.../passc/, so its conftest does not apply); pytest resolves them via the test parameters,
# which ruff sees as redefinitions of the imports.
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Json
from tests.featuregen.overlay.upload.passc.conftest import (  # noqa: F401 — pytest fixtures
    SERVICE_ACTOR,
    _confirm_join,
    _drain,
    _expire_join,
    _propose_join,
    human_admin_1,
    human_admin_2,
    passc_conn,
)

from featuregen.contracts.envelopes import Command
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.commands import confirm_fact, propose_fact, reject_fact
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    ColumnPair,
    EntityBridgeRef,
    fact_key,
    proposal_fingerprint,
)
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload import governance_analytics
from featuregen.overlay.upload.governance_analytics import (
    FactTypeRollup,
    compute_governance_dashboard,
    list_source_governance_summaries,
)
from featuregen.overlay.upload.passc.types import (
    ALGORITHM_VERSION,
    CONFIG_VERSION,
    CardinalityInferenceStatus,
    JoinCandidateEvidenceV1,
    NamespaceCompatibility,
    SignalEvidence,
)
from featuregen.overlay.upload.upload_catalog import table_ref
from featuregen.runtime.observability import counters

# ── Seed helpers ─────────────────────────────────────────────────────────────────────────────────


def _join_ref(from_table, to_table, column, source="src"):
    return ApprovedJoinRef(
        from_ref=CatalogObjectRef(source, "column", "public", from_table, column),
        to_ref=CatalogObjectRef(source, "column", "public", to_table, column),
        column_pairs=(ColumnPair(column, column),),
        cardinality="N:1")


def _col_ref(ref: CatalogObjectRef) -> str:
    return f"{ref.catalog_source}::{ref.schema}.{ref.table}.{ref.column}"


def _evidence(ref, key, *, bucket, signals):
    """A schema-complete JoinCandidateEvidenceV1 whose positive_signals are the given
    (signal_name, score_delta) pairs — the calibration seed's top-signal input."""
    lo, hi = sorted([_col_ref(ref.from_ref), _col_ref(ref.to_ref)])
    return JoinCandidateEvidenceV1(
        candidate_id=f"cand-{key[:12]}", from_ref=lo, to_ref=hi,
        column_pairs=((ref.column_pairs[0].from_col, ref.column_pairs[0].to_col),),
        proposed_direction=f"{lo} -> {hi}", proposed_cardinality=ref.cardinality,
        cardinality_status=CardinalityInferenceStatus.INFERRED_FROM_CONFIRMED_GRAIN,
        bucket=bucket, score=sum(delta for _name, delta in signals),
        positive_signals=tuple(SignalEvidence(n, d, (), "seeded") for n, d in signals),
        negative_signals=(),
        namespace_compatibility=NamespaceCompatibility.COMPATIBLE,
        namespace_reason_codes=(), grain_evidence=(), missing_requirements=(),
        llm_annotations=(), explanation="seeded", producer="passc",
        config_version=CONFIG_VERSION, candidate_algorithm_version=ALGORITHM_VERSION,
        source_snapshot_id="snap-test")


def _insert_ledger_row(conn, ref, key, *, bucket="strong",
                       signals=(("same_identifier_concept", 40), ("same_column_name", 30)),
                       evidence_json=None):
    """One pass_c_candidate_evidence row exactly as Pass C persists it (migration 0988):
    from_ref/to_ref stored SORTED, evidence_json = asdict(JoinCandidateEvidenceV1)."""
    lo, hi = sorted([_col_ref(ref.from_ref), _col_ref(ref.to_ref)])
    payload = (evidence_json if evidence_json is not None
               else asdict(_evidence(ref, key, bucket=bucket, signals=tuple(signals))))
    conn.execute(
        "INSERT INTO pass_c_candidate_evidence"
        " (catalog_source, candidate_id, candidate_fingerprint, from_ref, to_ref, fact_key,"
        "  proposed_event_id, bucket, namespace_compatibility, lifecycle, evidence_json,"
        "  source_snapshot_id, config_version, candidate_algorithm_version)"
        " VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, 'compatible', 'proposed', %s, 'snap-test',"
        " %s, %s)",
        (ref.from_ref.catalog_source, f"cand-{key[:12]}", f"fp-{key[:12]}", lo, hi, key,
         bucket, Json(payload), CONFIG_VERSION, ALGORITHM_VERSION))


def _reject_join_with_category(conn, ref, *, admin, category):
    """Reject the pending join with a structured category — the reject route's Command shape
    (api/routes/governance.py): `category` rides the OVERLAY_FACT_REJECTED payload."""
    key = fact_key(ref, "approved_join")
    target = _cas_target(fold_overlay_state(load_fact(conn, key)))
    res = reject_fact(conn, Command(
        "reject_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "target_event_id": target,
         "reason": "seeded reject", "category": category},
        admin, f"reject-{target}"))
    assert res.accepted, res.denied_reason
    return res


def _seed_draft_grain(conn, *, source="src", table="txn_grain"):
    """A DRAFT grain proposal via the REAL propose path (opens the platform-admin gate task)."""
    value = {"columns": ["id"], "is_unique": True}
    ref = table_ref(source, table)
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return ref, fact_key(ref, "grain")


def _seed_verified_grain(conn, admin, *, source="src", table="txn_grain"):
    """Propose + single platform-admin confirm (grain is single-confirm) -> VERIFIED."""
    ref, key = _seed_draft_grain(conn, source=source, table=table)
    target = _cas_target(fold_overlay_state(load_fact(conn, key)))
    res = confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target},
        admin, f"confirm-grain-{target}"))
    assert res.accepted, res.denied_reason
    assert fold_overlay_state(load_fact(conn, key)).status == "VERIFIED"
    return key


def _seed_open_bridge_task(conn):
    """An OPEN entity_bridge gate task via the REAL 3B.2B propose path (propose_fact -> open_task).
    entity_bridge is NOT a dashboard-governed fact type and never lands in ``overlay_proposal``
    (projection.py early-returns it), so its open task sits OUTSIDE the dashboard's enumeration."""
    ref = EntityBridgeRef(
        entity_id="party",
        left_ref=CatalogObjectRef("src", "column", "public", "customers", "party_id"),
        right_ref=CatalogObjectRef("src2", "column", "public", "parties", "party_id"))
    value = {"entity_id": "party",
             "left_ref": asdict(ref.left_ref), "right_ref": asdict(ref.right_ref)}
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "entity_bridge", "proposed_value": value},
        SERVICE_ACTOR, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return fact_key(ref, "entity_bridge")


# ── Fixtures ─────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def seed_governed_facts(passc_conn, human_admin_1, human_admin_2):
    """Source "src": 1 VERIFIED join (+ strong ledger row), 1 REJECTED join (category
    "different_entity", + strong ledger row whose top signal is same_identifier_concept),
    1 DRAFT join (its 2 side-labelled gate tasks stay open), 1 VERIFIED grain. Drained."""
    verified_ref = _join_ref("transactions", "customers", "cif_id")
    _propose_join(passc_conn, verified_ref)
    verified_key = fact_key(verified_ref, "approved_join")
    _insert_ledger_row(passc_conn, verified_ref, verified_key, bucket="strong",
                       signals=(("related_terms_key_link", 50), ("same_column_name", 30)))
    _confirm_join(passc_conn, verified_ref, admin1=human_admin_1, admin2=human_admin_2)

    rejected_ref = _join_ref("loans", "parties", "party_id")
    _propose_join(passc_conn, rejected_ref)
    rejected_key = fact_key(rejected_ref, "approved_join")
    _insert_ledger_row(passc_conn, rejected_ref, rejected_key, bucket="strong",
                       signals=(("same_identifier_concept", 40), ("same_column_name", 30)))
    _reject_join_with_category(passc_conn, rejected_ref, admin=human_admin_1,
                               category="different_entity")

    draft_ref = _join_ref("cards", "parties", "party_id")
    _propose_join(passc_conn, draft_ref)
    draft_key = fact_key(draft_ref, "approved_join")

    grain_key = _seed_verified_grain(passc_conn, human_admin_1)

    _drain(passc_conn)  # surface every proposal (incl. the DRAFTs) to overlay_proposal
    return {"verified_join": verified_key, "rejected_join": rejected_key,
            "draft_join": draft_key, "grain": grain_key}


@pytest.fixture
def seed_corrupt_proposal(passc_conn):
    """An overlay_proposal row whose fact_key has NO event stream — load_fact yields [] and the
    fold has no status. The reader must skip it (+ counter), never blank the dashboard."""
    passc_conn.execute(
        "INSERT INTO overlay_proposal (fact_key, status, proposed_value, proposal_fingerprint,"
        " draft_event_id, object_ref, catalog_source, fact_type, updated_seq)"
        " VALUES ('corrupt-governance-fact', 'DRAFT', %s, 'fp-corrupt', 'evt-corrupt',"
        " 'public.corrupt', 'src', 'approved_join', 999999999)",
        (Json({}),))
    return "corrupt-governance-fact"


@pytest.fixture
def seed_two_sources(passc_conn):
    """A DRAFT join for source "src" and a DRAFT grain for source "src2" — the cross-source and
    per-source-summary shapes."""
    _propose_join(passc_conn, _join_ref("transactions", "customers", "cif_id", source="src"))
    _seed_draft_grain(passc_conn, source="src2", table="t2")
    _drain(passc_conn)


# ── Counts by type × status ──────────────────────────────────────────────────────────────────────


def test_counts_by_type_and_status(passc_conn, seed_governed_facts):
    dash = compute_governance_dashboard(passc_conn, source="src")
    assert dash.scope == "source" and dash.source == "src"
    assert [r.fact_type for r in dash.fact_types] == \
        ["approved_join", "grain", "availability_time"]

    joins = next(r for r in dash.fact_types if r.fact_type == "approved_join")
    assert joins.confirmed == 1 and joins.pending == 1 and joins.rejected == 1
    assert joins.needs_attention == 0
    assert joins.rejected_by_category == {"different_entity": 1}

    grain = next(r for r in dash.fact_types if r.fact_type == "grain")
    assert grain.confirmed == 1 and grain.pending == 0 and grain.rejected == 0

    # availability_time was never proposed — the rollup is still EMITTED, all zeros
    avail = next(r for r in dash.fact_types if r.fact_type == "availability_time")
    assert avail == FactTypeRollup(fact_type="availability_time")


def test_source_filter_is_normalized(passc_conn, seed_governed_facts):
    dash = compute_governance_dashboard(passc_conn, source="  SRC  ")
    assert dash.scope == "source" and dash.source == "src"
    joins = next(r for r in dash.fact_types if r.fact_type == "approved_join")
    assert joins.confirmed == 1


def test_needs_attention_counts_a_demoted_join(passc_conn, human_admin_1, human_admin_2):
    """REVERIFY (expiry-demoted VERIFIED) maps to needs_attention — neither pending nor
    confirmed."""
    ref = _join_ref("transactions", "customers", "cif_id")
    _propose_join(passc_conn, ref)
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)
    _expire_join(passc_conn, ref)   # VERIFIED -> REVERIFY

    dash = compute_governance_dashboard(passc_conn, source="src")
    joins = next(r for r in dash.fact_types if r.fact_type == "approved_join")
    assert joins.needs_attention == 1
    assert joins.confirmed == 0 and joins.pending == 0 and joins.rejected == 0


# ── Calibration seed ─────────────────────────────────────────────────────────────────────────────


def test_calibration_seed_correlates_bucket_with_outcome(passc_conn, seed_governed_facts):
    dash = compute_governance_dashboard(passc_conn, source="src")
    cs = dash.calibration_seed
    strong = cs.confirm_rate_by_bucket["strong"]
    assert strong["confirmed"] == 1 and strong["rejected"] == 1
    assert strong["rate"] == 0.5
    # the REJECTED join's top-score_delta positive signal attributes its category
    assert cs.reject_category_by_top_signal == {"same_identifier_concept": {"different_entity": 1}}


def test_malformed_evidence_json_never_breaks_the_seed(
        passc_conn, seed_governed_facts, human_admin_1):
    """A garbage evidence_json still counts in the bucket tally (outcome is fold-derived) but
    contributes NO top-signal attribution — and never raises."""
    ref = _join_ref("mortgages", "parties", "party_id")
    _propose_join(passc_conn, ref)
    key = fact_key(ref, "approved_join")
    _insert_ledger_row(passc_conn, ref, key, bucket="strong",
                       evidence_json={"positive_signals": "not-a-list"})
    _reject_join_with_category(passc_conn, ref, admin=human_admin_1, category="wrong_direction")
    _drain(passc_conn)

    dash = compute_governance_dashboard(passc_conn, source="src")
    cs = dash.calibration_seed
    assert cs.confirm_rate_by_bucket["strong"]["rejected"] == 2
    assert cs.reject_category_by_top_signal == {"same_identifier_concept": {"different_entity": 1}}


# ── Queue health ─────────────────────────────────────────────────────────────────────────────────


def test_queue_health_and_age(passc_conn, seed_governed_facts):
    dash = compute_governance_dashboard(passc_conn, source="src")
    # only the DRAFT join's two side-labelled tasks are still open (confirm/reject close theirs)
    assert dash.queue_health.open_depth == 2
    assert dash.queue_health.oldest_pending_age_seconds is not None
    assert dash.queue_health.oldest_pending_age_seconds >= 0
    assert dash.queue_health.age_buckets == {"lt_1d": 2, "1_7d": 0, "gt_7d": 0}

    # backdate the open tasks 8 days -> they land in gt_7d and age the oldest-pending clock
    passc_conn.execute(
        "UPDATE human_tasks SET created_at = now() - interval '8 days' WHERE status = 'open'")
    dash = compute_governance_dashboard(passc_conn, source="src")
    assert dash.queue_health.age_buckets == {"lt_1d": 0, "1_7d": 0, "gt_7d": 2}
    assert dash.queue_health.oldest_pending_age_seconds > 7 * 86400


# ── Unknown source / fail-soft ───────────────────────────────────────────────────────────────────


def test_unknown_source_is_zeros_not_error(passc_conn):
    dash = compute_governance_dashboard(passc_conn, source="no-such-source")
    assert dash.scope == "source" and dash.source == "no-such-source"
    assert [r.fact_type for r in dash.fact_types] == \
        ["approved_join", "grain", "availability_time"]
    assert all(r.pending == 0 and r.confirmed == 0 and r.rejected == 0
               and r.needs_attention == 0 for r in dash.fact_types)
    assert dash.queue_health.open_depth == 0
    assert dash.queue_health.oldest_pending_age_seconds is None
    assert dash.calibration_seed.confirm_rate_by_bucket == {}
    assert dash.calibration_seed.reject_category_by_top_signal == {}
    assert dash.recent_activity.confirmed == 0 and dash.recent_activity.rejected == 0


def test_one_corrupt_fact_does_not_blank_the_dashboard(
        passc_conn, seed_governed_facts, seed_corrupt_proposal):
    """THE load-bearing fail-soft test: a proposal row with no readable stream is skipped and
    counted — the good counts still come back."""
    counter = "overlay.governance_analytics.fact_unreadable"
    before = counters.snapshot()["counters"].get(counter, 0)
    dash = compute_governance_dashboard(passc_conn, source="src")
    joins = next(r for r in dash.fact_types if r.fact_type == "approved_join")
    assert joins.confirmed == 1 and joins.pending == 1 and joins.rejected == 1
    assert counters.snapshot()["counters"].get(counter, 0) == before + 1


def test_load_fact_exception_is_fail_soft(passc_conn, seed_governed_facts, monkeypatch):
    """An EXPLODING load_fact (not just an empty stream) on one fact skips that fact only."""
    real = governance_analytics.load_fact
    bad_key = seed_governed_facts["draft_join"]

    def _boom(conn, key):
        if key == bad_key:
            raise RuntimeError("stream exploded")
        return real(conn, key)

    monkeypatch.setattr(governance_analytics, "load_fact", _boom)
    dash = compute_governance_dashboard(passc_conn, source="src")
    joins = next(r for r in dash.fact_types if r.fact_type == "approved_join")
    assert joins.confirmed == 1 and joins.rejected == 1
    assert joins.pending == 0   # the exploding DRAFT was skipped, nothing else lost


# ── Recent activity ──────────────────────────────────────────────────────────────────────────────


def test_recent_activity_counts_the_window(passc_conn, seed_governed_facts):
    dash = compute_governance_dashboard(passc_conn, source="src")
    ra = dash.recent_activity
    # 2 CONFIRMED events (the dual join's VERIFY + the grain's), 1 REJECTED — all just now
    assert ra.days == 7 and ra.confirmed == 2 and ra.rejected == 1

    # the same events fall OUT of a window anchored 30 days in the future
    later = datetime.now(UTC) + timedelta(days=30)
    dash2 = compute_governance_dashboard(passc_conn, source="src", now=later)
    assert dash2.recent_activity.confirmed == 0 and dash2.recent_activity.rejected == 0
    assert dash2.generated_at == later.isoformat()


# ── Cross-source + per-source summaries ──────────────────────────────────────────────────────────


def test_catalog_queue_counts_only_enumerated_governed_tasks(passc_conn, seed_two_sources):
    """The CATALOG headline queue must count ONLY the enumerated governed-fact tasks, so it
    reconciles with the per-source queues (and the rollups). Pre-fix the catalog scope ran an
    UNSCOPED ``status='open'`` query, so a NON-governed open task (here an entity_bridge gate
    task — 3B.2B proposes it via propose_fact -> open_task, but projection.py keeps it out of
    ``overlay_proposal``) inflated the headline open_depth: catalog 4 vs per-source 2+1."""
    bridge_key = _seed_open_bridge_task(passc_conn)
    _drain(passc_conn)
    # the bridge gate task IS open, and its fact is NOT in the governed enumeration
    assert passc_conn.execute(
        "SELECT count(*) FROM human_tasks WHERE status = 'open' AND fact_key = %s",
        (bridge_key,)).fetchone()[0] == 1
    assert passc_conn.execute(
        "SELECT count(*) FROM overlay_proposal WHERE fact_key = %s",
        (bridge_key,)).fetchone()[0] == 0

    catalog = compute_governance_dashboard(passc_conn, source=None)
    per_source = [compute_governance_dashboard(passc_conn, source=s) for s in ("src", "src2")]
    # reconciliation: the catalog queue == the sum of the per-source queues (2 side-labelled
    # tasks for the dual DRAFT join + 1 for the DRAFT grain) — the bridge task is EXCLUDED
    assert catalog.queue_health.open_depth == \
        sum(d.queue_health.open_depth for d in per_source)
    assert catalog.queue_health.open_depth == 3
    assert sum(catalog.queue_health.age_buckets.values()) == 3


def test_cross_source_and_source_summaries(passc_conn, seed_two_sources):
    dash = compute_governance_dashboard(passc_conn, source=None)   # cross-source
    assert dash.scope == "catalog" and dash.source is None
    joins = next(r for r in dash.fact_types if r.fact_type == "approved_join")
    grain = next(r for r in dash.fact_types if r.fact_type == "grain")
    assert joins.pending == 1 and grain.pending == 1

    sums = list_source_governance_summaries(passc_conn)
    assert len(sums) >= 2
    by_source = {s.source: s for s in sums}
    assert by_source["src"].pending == 1 and by_source["src"].confirmed == 0
    assert by_source["src2"].pending == 1
    # both sources have open gate tasks -> a real oldest-pending age
    assert by_source["src"].oldest_pending_age_seconds is not None
    assert by_source["src2"].oldest_pending_age_seconds is not None
