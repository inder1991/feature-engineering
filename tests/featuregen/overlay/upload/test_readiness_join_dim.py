"""Tier-1 polish Task 2 — the FeatureReadiness "join" dimension wired to REAL relationship state.

Pre-fix, the ``("join", "approved_join")`` entry in ``_PHASE1_UNPROMOTED`` had no readable fact
stream (it was absent from ``_FACT_TYPE_BY_REQUIREMENT``), so ``_table_fact_status`` returned
``("missing", not_promoted_in_phase1)`` for EVERY table and every table read as "blocked on
joins" regardless of real approved_join state — pure noise once Phase 3A shipped.

Now the join requirement coarsens the table's :func:`compute_relationship_readiness` verdict
(the ONE source of truth — the five-value view itself is untouched):

* ``NO_CANDIDATES`` / ``CONFIRMED`` -> ``"confirmed"`` — satisfied; surfaces in NEITHER
  actionable list (a table with no relationships is NOT a blocker);
* ``CANDIDATE_PROPOSED`` / ``WEAK_CANDIDATES_ONLY`` -> ``"proposed"`` — a review ask,
  non-blocking (cause ``proposed_unconfirmed``);
* ``CONFLICTING`` -> ``"conflicting"`` — the ONE join state that blocks (cause
  ``ingestion_error``: two active fact_keys claiming the same unordered column pair).

Seeding mirrors ``test_readiness_relationships.py`` (universe membership via a recorded field
decision; joins via the passc conftest's ``_propose_join``/``_confirm_join``; weak candidates via
a ``pass_c_candidate_evidence`` ledger row).
"""
# ruff: noqa: F811 — the passc conftest fixtures are IMPORTED by name (this module lives outside
# tests/.../passc/, so its conftest does not apply); pytest resolves them via the test parameters,
# which ruff sees as redefinitions of the imports.
from __future__ import annotations

import json
from dataclasses import asdict

from tests.featuregen.overlay.upload.passc.conftest import (  # noqa: F401 — pytest fixtures
    _confirm_join,
    _drain,
    _propose_join,
    human_admin_1,
    human_admin_2,
    passc_conn,
    service_actor,
)

from featuregen.overlay.field_decision import record_field_decision
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import candidate_fingerprint, unordered_pair
from featuregen.overlay.upload.readiness import (
    CAUSE_INGESTION_ERROR,
    CAUSE_PROPOSED_UNCONFIRMED,
    ReadinessScopeType,
    compute_readiness,
)

_CIF_TERM = "Customer Information File Identifier"


# ── Seeding (the test_readiness_relationships / test_join_governance shapes) ─────────────────────


def _seed_table(conn, table, column="id"):
    """Put ``table`` into the readiness universe (the ``field_decision_event`` refs
    ``_scoped_refs`` selects from) with one minimal recorded decision."""
    record_field_decision(
        conn, logical_ref=normalize_ref("src", None, table, column), field_name="concept",
        event_type="resolved", selected_evidence_ids=(), evidence_set_hash="h0",
        display_value_hash=None, load_bearing_value_hash=None, conflict_status="none",
        reason_codes=(), field_policy_version="fp-test", resolver_version="rv-test",
        actor_ref=None, supersedes_event_id=None)


def _bare_ref(from_table, to_table, column, cardinality="N:1"):
    return ApprovedJoinRef(
        from_ref=CatalogObjectRef("src", "column", "public", from_table, column),
        to_ref=CatalogObjectRef("src", "column", "public", to_table, column),
        column_pairs=(ColumnPair(column, column),),
        cardinality=cardinality)


def _c(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def _weak_evidence():
    """NEITHER side is a grain -> MANY_TO_MANY_RISK, forced weak (scorer rule 1)."""
    pairs = block_candidates([_c("transactions", "cif_id", term_name=_CIF_TERM),
                              _c("customers", "cif_id", term_name=_CIF_TERM)])
    assert len(pairs) == 1, "test setup must yield exactly one blocked pair"
    ev = score(pairs[0], source_snapshot_id="snap-1")
    assert ev.bucket == "weak"
    return ev


def _ledger_insert(conn, evidence, *, key=None, lifecycle="weak"):
    """Simulate the Task-10 ledger write: one row per UNORDERED (sorted) column-ref pair."""
    lo, hi = unordered_pair(evidence)
    conn.execute(
        "INSERT INTO pass_c_candidate_evidence (catalog_source, candidate_id,"
        " candidate_fingerprint, from_ref, to_ref, fact_key, proposed_event_id, bucket,"
        " namespace_compatibility, lifecycle, evidence_json, source_snapshot_id, config_version,"
        " candidate_algorithm_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("src", evidence.candidate_id, candidate_fingerprint(evidence), lo, hi, key, None,
         evidence.bucket, evidence.namespace_compatibility.value, lifecycle,
         json.dumps(asdict(evidence)), evidence.source_snapshot_id,
         evidence.config_version, evidence.candidate_algorithm_version))


# ── Readiness accessors (the REAL field names: requirement_id "join:<source>.<schema>.<table>") ──


def _readiness(conn, table):
    return compute_readiness(conn, source="src", scope=ReadinessScopeType.TABLE, subset=table)


def _join_blocking(fr):
    return [r for r in fr.blocking_requirements if r.requirement_id.startswith("join:")]


def _join_review(fr):
    return [r for r in fr.review_requirements if r.requirement_id.startswith("join:")]


# ── The five relationship states, coarsened into the join requirement ────────────────────────────


def test_no_joins_is_not_blocking(passc_conn):
    """A table with NO relationships must NOT read as "blocked on joins" (the pre-fix noise:
    join was a static missing/not-promoted blocker on EVERY table)."""
    _seed_table(passc_conn, "orphans")
    fr = _readiness(passc_conn, "orphans")
    assert _join_blocking(fr) == []
    assert _join_review(fr) == []          # nothing to review either — there are no candidates
    # grain/availability (no Pass B facts here) still block — only the join dim was freed.
    assert any(r.requirement_id.startswith("grain:") for r in fr.blocking_requirements)
    # NO_CANDIDATES maps to "confirmed" (satisfied): the join requirement is the ONLY confirmed
    # requirement in this scenario, so the display score pins its status.
    assert fr.summary_scores["confirmed"] == 1.0


def test_verified_join_is_confirmed_non_blocking(passc_conn, service_actor, human_admin_1,
                                                 human_admin_2):
    """A dual-confirmed (VERIFIED) approved_join reads "confirmed" — satisfied, in NEITHER
    actionable list — on BOTH endpoint tables."""
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    ref = _bare_ref("transactions", "customers", "cif_id")
    _propose_join(passc_conn, ref, actor=service_actor)
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)  # -> VERIFIED
    for table in ("transactions", "customers"):
        fr = _readiness(passc_conn, table)
        assert _join_blocking(fr) == []
        assert _join_review(fr) == []
        # CONFIRMED -> "confirmed": the only confirmed requirement here (grain/availability stay
        # missing) — the display score pins the join requirement's status.
        assert fr.summary_scores["confirmed"] == 1.0


def test_proposed_join_is_review_not_blocking(passc_conn, service_actor):
    """A DRAFT-proposed join is a REVIEW ask (status "proposed", cause proposed_unconfirmed),
    never a blocker."""
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    _propose_join(passc_conn, _bare_ref("transactions", "customers", "cif_id"),
                  actor=service_actor)
    _drain(passc_conn)          # visible via the overlay_proposal read model
    fr = _readiness(passc_conn, "transactions")
    assert _join_blocking(fr) == []
    (jr,) = _join_review(fr)
    assert jr.requirement_id == "join:src.public.transactions"
    assert jr.status == "proposed"
    assert jr.blocking is False
    assert jr.cause == CAUSE_PROPOSED_UNCONFIRMED


def test_weak_join_is_review_not_blocking(passc_conn):
    """A weak-only ledger candidate is advisory/review (status "proposed"), never a blocker."""
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    _ledger_insert(passc_conn, _weak_evidence(), lifecycle="weak")   # fact_key stays NULL
    fr = _readiness(passc_conn, "customers")
    assert _join_blocking(fr) == []
    (jr,) = _join_review(fr)
    assert jr.status == "proposed"
    assert jr.cause == CAUSE_PROPOSED_UNCONFIRMED


# ── PERF (whole-branch review): the relationship fold must run ONCE per compute_readiness ────────
#
# compute_relationship_readiness folds SOURCE-WIDE candidate stores (three full scans of
# overlay_proposal + pass_c_candidate_evidence, plus one load_fact round-trip per approved_join
# fact) regardless of its subset — the subset only narrows the RESULT. Calling it once per table
# from the requirement loop therefore did O(T x F) DB work for a T-table catalog with F facts.
# The fix hoists it: ONE call per compute_readiness, indexed by (schema, table). The spy wraps
# the module global both the pre-fix helper and the post-fix hoist resolve at call time.


def _spy_relationship_fold(monkeypatch):
    """Wrap readiness.compute_relationship_readiness with a call counter (delegating to the
    real implementation, so results — and thus behavior assertions — are untouched)."""
    import featuregen.overlay.upload.readiness as readiness_mod

    calls: list[tuple] = []
    real = readiness_mod.compute_relationship_readiness

    def counting(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(readiness_mod, "compute_relationship_readiness", counting)
    return calls


def test_catalog_readiness_folds_relationships_once(passc_conn, service_actor, monkeypatch):
    """A CATALOG compute_readiness over MULTIPLE tables must call the source-wide relationship
    fold exactly ONCE — not once per table (pre-fix: len(calls) == number of tables)."""
    for table in ("orphans", "transactions", "customers"):
        _seed_table(passc_conn, table, "cif_id")
    _propose_join(passc_conn, _bare_ref("transactions", "customers", "cif_id"),
                  actor=service_actor)
    _drain(passc_conn)

    calls = _spy_relationship_fold(monkeypatch)
    fr = compute_readiness(passc_conn, source="src", scope=ReadinessScopeType.CATALOG,
                           subset=None)
    assert len(calls) == 1, f"expected ONE source-wide relationship fold, got {len(calls)}"

    # Behavior is byte-identical to the per-table calls: the DRAFT join is a review ask on BOTH
    # endpoint tables; the orphan (NO_CANDIDATES) is satisfied; nothing join-blocks.
    assert _join_blocking(fr) == []
    assert sorted(r.requirement_id for r in _join_review(fr)) == [
        "join:src.public.customers",
        "join:src.public.transactions",
    ]


def test_table_subset_readiness_folds_relationships_once(passc_conn, service_actor, monkeypatch):
    """A TABLE-subset compute_readiness threads its OWN subset into the single fold — one call,
    scoped to the one table, same status as before the hoist."""
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    _propose_join(passc_conn, _bare_ref("transactions", "customers", "cif_id"),
                  actor=service_actor)
    _drain(passc_conn)

    calls = _spy_relationship_fold(monkeypatch)
    fr = _readiness(passc_conn, "transactions")
    assert len(calls) == 1
    assert calls[0][1].get("subset") == "transactions"  # compute_readiness's own subset, threaded
    (jr,) = _join_review(fr)
    assert jr.status == "proposed"
    assert jr.cause == CAUSE_PROPOSED_UNCONFIRMED


def test_conflicting_join_blocks(passc_conn, service_actor):
    """The ONE join state that blocks: two active fact_keys claiming the SAME unordered column
    pair (N:1 vs 1:1 hash to different keys) -> status "conflicting", cause ingestion_error."""
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    _propose_join(passc_conn, _bare_ref("transactions", "customers", "cif_id", "N:1"),
                  actor=service_actor)
    _propose_join(passc_conn, _bare_ref("transactions", "customers", "cif_id", "1:1"),
                  actor=service_actor)
    _drain(passc_conn)
    fr = _readiness(passc_conn, "transactions")
    (jr,) = _join_blocking(fr)
    assert jr.status == "conflicting"
    assert jr.blocking is True
    assert jr.cause == CAUSE_INGESTION_ERROR
    assert _join_review(fr) == []
    assert fr.operational_status == "blocked"
