"""Task 9 — the per-table RELATIONSHIP readiness dimension (spec §16).

A read-only diagnostic with its OWN five-value status enum (``RelationshipStatus``), never
overloading the 4-value ``ReadinessRequirement.status`` Literal. Two sources, one precedence:

* the ``approved_join`` fact substrate — enumerated from the ``overlay_proposal`` read model AND
  the ledger's fact-bearing rows (union: a just-proposed Pass-C join is visible through its ledger
  ``fact_key`` even before the projection drains) — with the LIVE status folded from the event log
  (``fold_overlay_state``), never trusted from the ledger ``lifecycle``;
* the ``pass_c_candidate_evidence`` ledger's WEAK rows — read, not recomputed (the AMBIGUOUS
  policy is upstream: ``same_bian_leaf_only``/``mixed_bian_leaf`` arrive as weak rows;
  ``generic_reference_without_context`` was suppressed at write-time and is simply absent).

Precedence per table: conflicting > confirmed > candidate_proposed > weak_candidates_only >
no_candidates. The conflict grain mirrors ``lifecycle.decide_action``: two DIFFERENT active
fact_keys claiming the SAME unordered column pair.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace

import pytest
from tests.featuregen.overlay.upload.passc.conftest import _confirm_join, _drain, _propose_join

from featuregen.overlay.field_decision import record_field_decision
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import (
    build_join_ref,
    candidate_fingerprint,
    unordered_pair,
)
from featuregen.overlay.upload.readiness import (
    RelationshipStatus,
    compute_relationship_readiness,
)

_CIF_TERM = "Customer Information File Identifier"
_ACCT_TERM = "Account Identifier"


# ── Evidence builders (the Task-7 test shapes: one blocked pair, scored) ──────────────────────────


def _c(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def _evidence(a, b):
    pairs = block_candidates([a, b])
    assert len(pairs) == 1, "test setup must yield exactly one blocked pair"
    return score(pairs[0], source_snapshot_id="snap-1")


def _strong_evidence(from_table="transactions", to_table="customers", column="cif_id"):
    """A strong, grain-inferred N:1 candidate: {from_table}.{column} -> {to_table}.{column}."""
    ev = _evidence(_c(from_table, column, term_name=_CIF_TERM),
                   _c(to_table, column, term_name=_CIF_TERM, is_grain=True))
    assert ev.bucket == "strong" and ev.proposed_cardinality == "N:1"
    return ev


def _weak_evidence(from_table="transactions", to_table="customers", column="cif_id",
                   term=_CIF_TERM):
    """NEITHER side is a grain -> MANY_TO_MANY_RISK, forced weak, NO cardinality (scorer rule 1)."""
    ev = _evidence(_c(from_table, column, term_name=term), _c(to_table, column, term_name=term))
    assert ev.bucket == "weak" and ev.proposed_cardinality is None
    return ev


def _ledger_insert(conn, evidence, *, key=None, lifecycle="proposed", source="src"):
    """Simulate the Task-10 ledger write: one row per UNORDERED (sorted) column-ref pair."""
    lo, hi = unordered_pair(evidence)
    conn.execute(
        "INSERT INTO pass_c_candidate_evidence (catalog_source, candidate_id,"
        " candidate_fingerprint, from_ref, to_ref, fact_key, proposed_event_id, bucket,"
        " namespace_compatibility, lifecycle, evidence_json, source_snapshot_id, config_version,"
        " candidate_algorithm_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (source, evidence.candidate_id, candidate_fingerprint(evidence), lo, hi, key, None,
         evidence.bucket, evidence.namespace_compatibility.value, lifecycle,
         json.dumps(asdict(evidence)), evidence.source_snapshot_id,
         evidence.config_version, evidence.candidate_algorithm_version))


# ── Universe seeding: a table is in scope once it has a recorded field decision ───────────────────


def _seed_table(conn, table, column="id", source="src"):
    """Put ``table`` into the readiness universe (the ``field_decision_event`` refs
    ``_scoped_refs`` selects from) with one minimal recorded decision."""
    record_field_decision(
        conn, logical_ref=normalize_ref(source, None, table, column), field_name="concept",
        event_type="resolved", selected_evidence_ids=(), evidence_set_hash="h0",
        display_value_hash=None, load_bearing_value_hash=None, conflict_status="none",
        reason_codes=(), field_policy_version="fp-test", resolver_version="rv-test",
        actor_ref=None, supersedes_event_id=None)


def _status_of(conn, table, source="src"):
    rows = compute_relationship_readiness(conn, source=source, subset=table)
    assert len(rows) == 1, f"expected one row for {table!r}, got {rows}"
    return rows[0].status


# ── The four core states ──────────────────────────────────────────────────────────────────────────


def test_table_with_no_candidates_reads_no_candidates(passc_conn):
    _seed_table(passc_conn, "orphans")
    assert _status_of(passc_conn, "orphans") is RelationshipStatus.NO_CANDIDATES


def test_proposed_join_reads_candidate_proposed_via_ledger_bridge(passc_conn, service_actor):
    """A Pass-C proposed join is visible through the ledger row's fact_key EVEN BEFORE the
    projection drains — the live DRAFT status is folded from the event log, not the read model."""
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    ev = _strong_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev, actor=service_actor)          # no projection drain
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"))
    assert _status_of(passc_conn, "transactions") is RelationshipStatus.CANDIDATE_PROPOSED
    assert _status_of(passc_conn, "customers") is RelationshipStatus.CANDIDATE_PROPOSED


def test_proposed_join_visible_without_ledger_row_once_projected(passc_conn, service_actor):
    """A join proposed OUTSIDE Pass C (no ledger row) is still enumerated — via the
    overlay_proposal read model once the projection has processed the propose."""
    _seed_table(passc_conn, "transactions", "cif_id")
    ev = _strong_evidence()
    _propose_join(passc_conn, build_join_ref(ev, "src"), ev, actor=service_actor)
    _drain(passc_conn)
    row = compute_relationship_readiness(passc_conn, source="src", subset="transactions")[0]
    assert row.status is RelationshipStatus.CANDIDATE_PROPOSED
    assert len(row.proposed_pairs) == 1


def test_dual_confirmed_join_reads_confirmed(passc_conn, service_actor, human_admin_1,
                                             human_admin_2):
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    ev = _strong_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev, actor=service_actor)
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"))
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)  # -> VERIFIED
    for table in ("transactions", "customers"):
        row = compute_relationship_readiness(passc_conn, source="src", subset=table)[0]
        assert row.status is RelationshipStatus.CONFIRMED
        assert len(row.confirmed_pairs) == 1 and not row.proposed_pairs


def test_weak_ledger_row_only_reads_weak_candidates_only(passc_conn):
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    _ledger_insert(passc_conn, _weak_evidence(), lifecycle="weak")   # fact_key stays NULL
    for table in ("transactions", "customers"):
        row = compute_relationship_readiness(passc_conn, source="src", subset=table)[0]
        assert row.status is RelationshipStatus.WEAK_CANDIDATES_ONLY
        assert len(row.weak_pairs) == 1 and not row.proposed_pairs


# ── Precedence + conflict ─────────────────────────────────────────────────────────────────────────


def test_confirmed_outranks_weak_on_the_same_table(passc_conn, service_actor, human_admin_1,
                                                   human_admin_2):
    """A table with one CONFIRMED pair and one weak pair reads confirmed — the weak pair stays
    listed as detail, never masking the verified relationship."""
    for table, col in (("transactions", "cif_id"), ("customers", "cif_id"),
                       ("accounts", "acct_id")):
        _seed_table(passc_conn, table, col)
    ev = _strong_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev, actor=service_actor)
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)
    weak = _weak_evidence(from_table="transactions", to_table="accounts", column="acct_id",
                          term=_ACCT_TERM)
    _ledger_insert(passc_conn, weak, lifecycle="weak")

    txn = compute_relationship_readiness(passc_conn, source="src", subset="transactions")[0]
    assert txn.status is RelationshipStatus.CONFIRMED           # confirmed > weak
    assert len(txn.confirmed_pairs) == 1 and len(txn.weak_pairs) == 1
    # The weak pair's OTHER endpoint has no confirmed/proposed pair of its own -> weak-only.
    assert _status_of(passc_conn, "accounts") is RelationshipStatus.WEAK_CANDIDATES_ONLY


def test_two_active_claims_on_the_same_pair_read_conflicting(passc_conn, service_actor):
    """The decide_action conflict grain, at rest: two DIFFERENT active fact_keys (here N:1 vs 1:1,
    which hash to different keys) claiming the SAME unordered column pair -> conflicting."""
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    ev = _strong_evidence()
    _propose_join(passc_conn, build_join_ref(ev, "src"), ev, actor=service_actor)
    rival = replace(ev, proposed_cardinality="1:1")             # same pair, DIFFERENT fact_key
    _propose_join(passc_conn, build_join_ref(rival, "src"), rival, actor=service_actor)
    _drain(passc_conn)
    for table in ("transactions", "customers"):
        row = compute_relationship_readiness(passc_conn, source="src", subset=table)[0]
        assert row.status is RelationshipStatus.CONFLICTING
        assert len(row.conflicting_pairs) == 1
        assert not row.confirmed_pairs and not row.proposed_pairs


def test_rejected_join_confers_nothing(passc_conn, service_actor, human_admin_1):
    """A rejected proposal is not a candidate: with nothing else, the table reads no_candidates."""
    from tests.featuregen.overlay.upload.passc.conftest import _reject_join
    _seed_table(passc_conn, "transactions", "cif_id")
    ev = _strong_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev, actor=service_actor)
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"))
    _reject_join(passc_conn, ref, admin=human_admin_1)
    assert _status_of(passc_conn, "transactions") is RelationshipStatus.NO_CANDIDATES


# ── Scope safety (the existing readiness table-selection convention) ──────────────────────────────


def test_subset_is_schema_aware_and_scope_safe(passc_conn):
    """A weak pair on public.transactions never bleeds onto a SAME-NAMED table in another schema;
    schema-qualified selectors disambiguate; an ambiguous bare name raises (mirrors _scoped_refs)."""
    _seed_table(passc_conn, "transactions", "cif_id")            # public.transactions
    record_field_decision(  # same table name, DIFFERENT schema: legacy.transactions
        passc_conn, logical_ref=normalize_ref("src", "legacy", "transactions", "cif_id"),
        field_name="concept", event_type="resolved", selected_evidence_ids=(),
        evidence_set_hash="h0", display_value_hash=None, load_bearing_value_hash=None,
        conflict_status="none", reason_codes=(), field_policy_version="fp-test",
        resolver_version="rv-test", actor_ref=None, supersedes_event_id=None)
    _seed_table(passc_conn, "customers", "cif_id")
    _ledger_insert(passc_conn, _weak_evidence(), lifecycle="weak")  # touches public.transactions

    rows = compute_relationship_readiness(passc_conn, source="src", subset="public.transactions")
    assert [(r.schema, r.table, r.status) for r in rows] == [
        ("public", "transactions", RelationshipStatus.WEAK_CANDIDATES_ONLY)]
    rows = compute_relationship_readiness(passc_conn, source="src", subset="legacy.transactions")
    assert [(r.schema, r.table, r.status) for r in rows] == [
        ("legacy", "transactions", RelationshipStatus.NO_CANDIDATES)]
    with pytest.raises(ValueError, match="ambiguous"):
        compute_relationship_readiness(passc_conn, source="src", subset="transactions")


def test_read_scope_hides_pii_column_pairs(passc_conn):
    """Audit finding [6]: a join on a sensitivity-hidden column NEVER surfaces as a pair (nor flips
    the table's status) for a caller who can't see it — the relationship diagnostic omits it exactly
    as asset_detail's approved_joins omit a join to a hidden endpoint. A pii_reader still sees it;
    the unscoped (roles=None) default is unchanged."""
    # Both tables enter the universe via a VISIBLE sibling column (id) — so they survive the ref
    # prune — while the JOIN itself is on a pii-hidden column (ssn).
    _seed_table(passc_conn, "transactions", "id")
    _seed_table(passc_conn, "customers", "id")
    for table in ("transactions", "customers"):
        passc_conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, sensitivity) VALUES ('src', %s, 'column', %s, 'ssn', 'text', 'pii')",
            (f"public.{table}.ssn", table))
    _ledger_insert(passc_conn, _weak_evidence(column="ssn"), lifecycle="weak")

    # A non-pii caller: the pii pair is OMITTED -> both tables read no_candidates, ssn never named.
    scoped = compute_relationship_readiness(passc_conn, source="src", roles=())
    assert {r.table: r.status for r in scoped} == {
        "transactions": RelationshipStatus.NO_CANDIDATES,
        "customers": RelationshipStatus.NO_CANDIDATES}
    assert all("ssn" not in p for r in scoped for p in r.weak_pairs)
    # A pii_reader: the weak pair on ssn is visible.
    seen = compute_relationship_readiness(passc_conn, source="src", roles=("pii_reader",))
    assert {r.status for r in seen} == {RelationshipStatus.WEAK_CANDIDATES_ONLY}
    assert any("ssn" in p for r in seen for p in r.weak_pairs)
    # Unscoped (roles=None) preserves today's behaviour.
    assert {r.status for r in compute_relationship_readiness(passc_conn, source="src")} == {
        RelationshipStatus.WEAK_CANDIDATES_ONLY}


def test_catalog_scope_reports_every_table_and_unknown_subset_is_empty(passc_conn):
    _seed_table(passc_conn, "transactions", "cif_id")
    _seed_table(passc_conn, "customers", "cif_id")
    _ledger_insert(passc_conn, _weak_evidence(), lifecycle="weak")
    rows = compute_relationship_readiness(passc_conn, source="src")
    assert [(r.schema, r.table) for r in rows] == [("public", "customers"),
                                                   ("public", "transactions")]
    assert all(r.status is RelationshipStatus.WEAK_CANDIDATES_ONLY for r in rows)
    assert compute_relationship_readiness(passc_conn, source="src", subset="no_such") == ()
