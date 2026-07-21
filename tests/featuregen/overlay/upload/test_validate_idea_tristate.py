from datetime import UTC, datetime, timedelta

import pytest

from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import read_column_facts
from featuregen.overlay.upload.feature_assist import RejectCode, _validate_idea
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.operational_facts import read_operational_value

NOW = datetime(2026, 7, 18, tzinfo=UTC)
FRESH = timedelta(hours=24)


def _fresh(db, source):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET "
        "last_completed_at = %s", (source, NOW, NOW))


def _kv(refs, catalog):
    known = set(refs)
    src_of = {r: {catalog} for r in refs}
    return known, src_of


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "churned", "boolean"),
    ])
    _fresh(db, "bank")


def test_clean_idea_is_design_checked_with_typed_operands(db):
    _bank(db)
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "avg_balance", "derives_from": ["public.accounts.balance"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "DESIGN_CHECKED"
    assert idea.requirements == ()
    assert idea.operation_kind == "avg"
    assert idea.measure_refs == (("bank", "public.accounts.balance"),)


def test_ungrounded_is_rejected(db):
    _bank(db)
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "x", "derives_from": ["public.accounts.nonexistent"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.UNGROUNDED


def test_ambiguous_catalog_is_rejected(db):
    _bank(db)
    known = {"public.accounts.balance"}
    src_of = {"public.accounts.balance": {"bank", "other"}}   # two catalogs -> cannot resolve
    raw = {"name": "x", "derives_from": ["public.accounts.balance"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.AMBIGUOUS_CATALOG


def test_unknown_column_pair_is_rejected(db):
    _bank(db)
    known = {"public.accounts.balance"}
    src_of = {"public.accounts.balance": {"ghost"}}   # resolves to a catalog the pair doesn't live in
    raw = {"name": "x", "derives_from": ["public.accounts.balance"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.UNKNOWN_COLUMN


def test_leakage_is_rejected(db):
    _bank(db)
    known, src_of = _kv(["public.accounts.churned"], "bank")
    raw = {"name": "x", "derives_from": ["public.accounts.churned"], "aggregation": "latest"}
    idea, rej = _validate_idea(db, raw, known, src_of, "public.accounts.churned", NOW, FRESH)
    assert idea is None and rej.code == RejectCode.LEAKAGE


def test_stale_source_is_rejected(db):
    _bank(db)
    db.execute("UPDATE overlay_drift_watermark SET last_completed_at = %s WHERE catalog_source = 'bank'",
               (NOW - timedelta(days=30),))
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "x", "derives_from": ["public.accounts.balance"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.STALE


def _ftr_col(db, table, column, *, data_type="unknown", declared_type=None):
    ref = f"public.{table}.{column}"
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, declared_type) VALUES ('ftr', %s, 'column', %s, %s, %s, %s)",
        (ref, table, column, data_type, declared_type))
    _fresh(db, "ftr")
    return ref


def test_type_is_numeric_when_data_type_unknown_but_declared_numeric(db):
    ref = _ftr_col(db, "loans", "balance", data_type="unknown", declared_type="numeric")
    known, src_of = _kv([ref], "ftr")
    raw = {"name": "avg_balance", "derives_from": [ref], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    codes = [(r.code, r.operand) for r in idea.requirements]
    assert ("TYPE_IS_NUMERIC", ("ftr", ref)) in codes


def test_declared_non_numeric_is_rejected(db):
    ref = _ftr_col(db, "loans", "status", data_type="unknown", declared_type="varchar")
    known, src_of = _kv([ref], "ftr")
    raw = {"name": "avg_status", "derives_from": [ref], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.NON_NUMERIC


def test_operational_numeric_data_type_clears_type_check(db):
    ref = _ftr_col(db, "loans", "amt", data_type="numeric", declared_type=None)
    known, src_of = _kv([ref], "ftr")
    raw = {"name": "avg_amt", "derives_from": [ref], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "DESIGN_CHECKED"
    assert all(r.code != "TYPE_IS_NUMERIC" for r in idea.requirements)


def _govern(db, catalog, ref, field_name, value):
    lref = normalize_ref(catalog, "public", ref.split(".")[-2], ref.split(".")[-1])
    record_field_decision(
        db, logical_ref=lref, field_name=field_name,
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash(value),
        load_bearing_value_hash=canonical_hash(value), conflict_status="resolved",
        reason_codes=[], field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None, supersedes_event_id=None)


def test_governed_non_additive_sum_is_rejected(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="non_additive")])
    _fresh(db, "bank")
    _govern(db, "bank", "public.accounts.balance", "additivity", "non_additive")
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.ADDITIVITY


def test_unresolved_additivity_sum_needs_external_validation(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive")])
    _fresh(db, "bank")   # additivity is file-declared only -> NOT governed
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "ADDITIVITY_SUPPORTS_OPERATION" for r in idea.requirements)
    operands = [r.operand for r in idea.requirements if r.code == "ADDITIVITY_SUPPORTS_OPERATION"]
    assert operands == [("bank", "public.accounts.balance")]


def test_governed_additive_sum_clears_additivity_check(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="additive")])
    _fresh(db, "bank")
    _govern(db, "bank", "public.accounts.balance", "additivity", "additive")
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "DESIGN_CHECKED"
    assert all(r.code != "ADDITIVITY_SUPPORTS_OPERATION" for r in idea.requirements)


# ── C1 tamper gates on the customer feature path — a DRIFTED / forked / retired / projection-lagged
#    governed value can NO LONGER clear a design check (the confirmed blocker's fix). Only the
#    hash-verified status=="resolved" head clears; the rest are honest needs-check / abort. ──────────
def test_drifted_additivity_no_longer_clears_and_old_reader_would_have(db):
    """THE FIX. The approved decision is ``non_additive``; the flat graph value DRIFTED to
    ``additive`` (legacy API / faulty projection / manual DB change). The OLD permissive
    ``read_column_facts`` still serves it as GOVERNED-additive (which cleared a SUM); C1 hash-verifies
    the flat value against the approved decision → ``hash_mismatch`` → does NOT clear → emits
    ADDITIVITY_SUPPORTS_OPERATION (NEEDS_EXTERNAL_VALIDATION, never DESIGN_CHECKED)."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="non_additive")])
    _fresh(db, "bank")
    _govern(db, "bank", "public.accounts.balance", "additivity", "non_additive")   # approved value
    _govern(db, "bank", "public.accounts.balance", "logical_representation", "numeric")
    lref = normalize_ref("bank", "public", "accounts", "balance")
    # DRIFT: mutate ONLY the flat graph value out from under the approved decision.
    db.execute("UPDATE graph_node SET additivity = 'additive' WHERE object_ref = %s",
               ("public.accounts.balance",))
    # OLD-reader CONTROL: the permissive reader STILL serves governed-additive (the bypass existed) —
    # under the old wiring "additive" is a safe additive value, so the SUM would have cleared.
    old = read_column_facts(db, lref, "additivity")
    assert old.authority == "governed" and old.value == "additive"
    # C1 refuses: the drifted flat value no longer hashes to the approved decision.
    assert read_operational_value(db, lref, "additivity").status == "hash_mismatch"
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "ADDITIVITY_SUPPORTS_OPERATION" for r in idea.requirements)


def test_drifted_numeric_type_no_longer_clears(db):
    """The approved logical_representation is ``varchar`` (non-numeric); the flat ``data_type``
    DRIFTED to ``numeric``. The OLD reader serves numeric (which cleared an AVG's type check); C1
    hash-mismatches → does NOT clear → emits TYPE_IS_NUMERIC."""
    ref = _ftr_col(db, "loans", "amt", data_type="varchar", declared_type=None)
    _govern(db, "ftr", ref, "logical_representation", "varchar")   # approved value
    lref = normalize_ref("ftr", "public", "loans", "amt")
    db.execute("UPDATE graph_node SET data_type = 'numeric' WHERE object_ref = %s", (ref,))   # DRIFT
    old = read_column_facts(db, lref, "logical_representation")
    assert old.authority == "governed" and old.value == "numeric"   # OLD reader would clear AVG
    assert read_operational_value(db, lref, "logical_representation").status == "hash_mismatch"
    known, src_of = _kv([ref], "ftr")
    raw = {"name": "avg_amt", "derives_from": [ref], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "TYPE_IS_NUMERIC" for r in idea.requirements)


def test_retired_additivity_decision_does_not_clear(db):
    """A RETIRED (staled) additivity decision is never served operational → C1 ``status="retired"`` →
    SUM cannot clear → honest needs-check."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="additive")])
    _fresh(db, "bank")
    _govern(db, "bank", "public.accounts.balance", "logical_representation", "numeric")
    lref = normalize_ref("bank", "public", "accounts", "balance")
    record_field_decision(
        db, logical_ref=lref, field_name="additivity",
        event_type=FieldDecisionEventType.STALED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=None, load_bearing_value_hash=None,
        conflict_status="staled", reason_codes=["evidence_staled"],
        field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None, supersedes_event_id=None)
    assert read_operational_value(db, lref, "additivity").status == "retired"
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "ADDITIVITY_SUPPORTS_OPERATION" for r in idea.requirements)


def test_forked_additivity_decision_does_not_clear(db):
    """A FORKED head — two disagreeing non-retired decisions at the same instant — is ambiguous, so
    C1 ``status="fork"`` serves no operational value → SUM cannot clear → honest needs-check."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="additive")])
    _fresh(db, "bank")
    _govern(db, "bank", "public.accounts.balance", "logical_representation", "numeric")
    lref = normalize_ref("bank", "public", "accounts", "balance")
    for lb in ("additive", "non_additive"):   # two heads at the SAME created_at that DISAGREE
        record_field_decision(
            db, logical_ref=lref, field_name="additivity",
            event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
            evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash(lb),
            load_bearing_value_hash=canonical_hash(lb), conflict_status="resolved", reason_codes=[],
            field_policy_version="upload-field-policy-v1",
            resolver_version="upload-resolve-and-project-v1", actor_ref=None,
            supersedes_event_id=None, now=NOW)
    assert read_operational_value(db, lref, "additivity").status == "fork"
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert any(r.code == "ADDITIVITY_SUPPORTS_OPERATION" for r in idea.requirements)


def test_projection_unavailable_aborts_generation_retryably(db):
    """A DEGRADED/lagged load-bearing overlay projection makes every C1 read potentially stale
    (GATE 3): generation ABORTS with the retryable :class:`CatalogProjectionUnavailable` (the route
    maps it to a 503) rather than silently clearing on a stale value."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="additive")])
    _fresh(db, "bank")
    _govern(db, "bank", "public.accounts.balance", "additivity", "additive")
    _govern(db, "bank", "public.accounts.balance", "logical_representation", "numeric")
    db.execute(
        "INSERT INTO projection_degraded "
        "(projection_name, aggregate, aggregate_id, reason, poison_seq) "
        "VALUES (%s, %s, %s, %s, %s)", ["overlay", "overlay_fact", "poisoned", "poison", 1])
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    with pytest.raises(CatalogProjectionUnavailable) as excinfo:
        _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert excinfo.value.code == CATALOG_PROJECTION_UNAVAILABLE


def test_windowed_declared_as_of_needs_temporal(db):
    _bank(db)   # posted_at as_of=True but file-declared (no availability_fact_event_id)
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "avg_bal_90d", "derives_from": ["public.accounts.balance"],
           "aggregation": "avg_90d"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    temporal = [r for r in idea.requirements if r.code == "TEMPORAL_IS_POPULATED"]
    assert temporal and temporal[0].operand == ("bank", "public.accounts.posted_at")
    assert idea.time_ref == ("bank", "public.accounts.posted_at")


def test_governed_as_of_clears_temporal(db):
    _bank(db)
    db.execute("UPDATE graph_node SET availability_fact_event_id = 'evt_av' "
               "WHERE object_ref = 'public.accounts.posted_at'")
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "avg_bal_90d", "derives_from": ["public.accounts.balance"],
           "aggregation": "avg_90d"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert all(r.code != "TEMPORAL_IS_POPULATED" for r in idea.requirements)


def test_windowed_with_no_as_of_column_is_rejected(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "balance", "numeric")])   # no as_of column at all
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.balance"], "t")
    raw = {"name": "avg_bal_90d", "derives_from": ["public.accounts.balance"],
           "aggregation": "avg_90d"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.NO_POINT_IN_TIME


def test_grain_declared_not_confirmed_needs_grain_is_unique(db):
    _bank(db)   # id is_grain=True but file-declared (no grain_fact_event_id)
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "cnt_per_account", "derives_from": ["public.accounts.balance"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    grain = [r for r in idea.requirements if r.code == "GRAIN_IS_UNIQUE"]
    assert grain and grain[0].operand == ("bank", "public.accounts.id")
    assert idea.grain_ref == ("bank", "public.accounts.id")


def test_governed_grain_clears_grain_check(db):
    _bank(db)
    db.execute("UPDATE graph_node SET grain_fact_event_id = 'evt_g' "
               "WHERE object_ref = 'public.accounts.id'")
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "cnt_per_account", "derives_from": ["public.accounts.balance"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert all(r.code != "GRAIN_IS_UNIQUE" for r in idea.requirements)
    assert idea.grain_ref == ("bank", "public.accounts.id")


def test_absent_units_across_combining_op_needs_unit_and_currency_consistent(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "a", "numeric"),   # no unit / currency
        CanonicalRow("t", "accounts", "b", "numeric")])
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a", "public.accounts.b"], "t")
    raw = {"name": "ratio_ab", "derives_from": ["public.accounts.a", "public.accounts.b"],
           "aggregation": "ratio"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    codes = {r.code for r in idea.requirements}
    assert "UNIT_CONSISTENT" in codes and "CURRENCY_CONSISTENT" in codes


def test_mixed_units_still_hard_rejected(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "a", "numeric", unit="dollars"),
        CanonicalRow("t", "accounts", "b", "numeric", unit="cents")])
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a", "public.accounts.b"], "t")
    raw = {"name": "sum_ab", "derives_from": ["public.accounts.a", "public.accounts.b"],
           "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.MIXED_UNITS


def test_mixed_currency_still_hard_rejected(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "a", "numeric", currency="USD"),
        CanonicalRow("t", "accounts", "b", "numeric", currency="EUR")])
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a", "public.accounts.b"], "t")
    raw = {"name": "sum_ab", "derives_from": ["public.accounts.a", "public.accounts.b"],
           "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert idea is None and rej.code == RejectCode.MIXED_CURRENCY


def test_partially_absent_unit_needs_check_on_the_absent_operand_only(db):
    # a declares dollars, b declares NOTHING: no positive contradiction (no reject), but the silent
    # pass is closed — the ABSENT operand carries the UNIT_CONSISTENT check. Currency agrees on both
    # so no CURRENCY_CONSISTENT (a matching hint adds no requirement — and promotes nothing).
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "a", "numeric", unit="dollars", currency="USD"),
        CanonicalRow("t", "accounts", "b", "numeric", currency="USD")])
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a", "public.accounts.b"], "t")
    raw = {"name": "ratio_ab", "derives_from": ["public.accounts.a", "public.accounts.b"],
           "aggregation": "ratio"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    units = [r.operand for r in idea.requirements if r.code == "UNIT_CONSISTENT"]
    assert units == [("t", "public.accounts.b")]
    assert all(r.code != "CURRENCY_CONSISTENT" for r in idea.requirements)


def test_single_measure_absent_unit_adds_no_requirement(db):
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "a", "numeric")])
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a"], "t")
    raw = {"name": "avg_a", "derives_from": ["public.accounts.a"], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert all(r.code not in ("UNIT_CONSISTENT", "CURRENCY_CONSISTENT") for r in idea.requirements)


def _two_table(db, *, fact_key=None, status=None, acct_sensitivity=None):
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "data_type) VALUES ('bank', 'public.transactions.amount', 'column', 'transactions', "
               "'amount', 'numeric')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name) "
               "VALUES ('bank', 'public.transactions.acct_id', 'column', 'transactions', 'acct_id')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "is_grain, sensitivity) VALUES ('bank', 'public.accounts.account_id', 'column', "
               "'accounts', 'account_id', true, %s)", (acct_sensitivity,))
    db.execute("INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, "
               "authority, approved_join_fact_key, approved_join_status) VALUES ('bank', 'joins', "
               "'public.transactions.acct_id', 'public.accounts.account_id', 'N:1', 'operational', "
               "%s, %s)", (fact_key, status))
    _fresh(db, "bank")


def test_cross_table_operational_join_clears(db):
    _two_table(db)   # declared edge (no fact link) -> OPERATIONAL
    known, src_of = _kv(["public.transactions.amount"], "bank")
    raw = {"name": "sum_txn_per_acct", "derives_from": ["public.transactions.amount"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=())
    assert rej is None
    assert all(r.code != "JOIN_CONNECTIVITY" for r in idea.requirements)


def test_cross_table_unverified_join_needs_join_connectivity(db):
    # RF-I1: "authorized but unverified" = fact-linked edge with a status IN the
    # graph_edge_approved_join_status_check vocabulary that is not yet VERIFIED -> 'DRAFT'.
    _two_table(db, fact_key="ajf-1", status="DRAFT")
    known, src_of = _kv(["public.transactions.amount"], "bank")
    raw = {"name": "sum_txn_per_acct", "derives_from": ["public.transactions.amount"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=())
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    joins = [r for r in idea.requirements if r.code == "JOIN_CONNECTIVITY"]
    assert joins and joins[0].operand == ("bank", "public.transactions.amount")


def test_cross_table_no_path_is_rejected(db):
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "data_type) VALUES ('bank', 'public.transactions.amount', 'column', 'transactions', "
               "'amount', 'numeric')")
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               "is_grain) VALUES ('bank', 'public.accounts.account_id', 'column', 'accounts', "
               "'account_id', true)")
    _fresh(db, "bank")   # no join edge between transactions and accounts
    known, src_of = _kv(["public.transactions.amount"], "bank")
    raw = {"name": "sum_txn_per_acct", "derives_from": ["public.transactions.amount"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=())
    assert idea is None and rej.code == RejectCode.NO_JOIN_PATH


def test_cross_table_read_scope_denied_hop_is_rejected(db):
    _two_table(db, acct_sensitivity="pii")   # the only hop's endpoint is pii-hidden for roles=()
    known, src_of = _kv(["public.transactions.amount"], "bank")
    raw = {"name": "sum_txn_per_acct", "derives_from": ["public.transactions.amount"],
           "aggregation": "count", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH, roles=())
    assert idea is None and rej.code == RejectCode.JOIN_DENIED


# ── C2-C3 Task 2: requirements minted through the validated registry factory ──────────────────────
def test_type_is_numeric_requirement_is_registry_built(db):
    # RF-C2 operational-unknown fixture: same code/operand as before, now registry-validated —
    # every minted requirement carries the pinned schema_version and (here) no params.
    ref = _ftr_col(db, "loans", "balance", data_type="unknown", declared_type="numeric")
    known, src_of = _kv([ref], "ftr")
    raw = {"name": "avg_balance", "derives_from": [ref], "aggregation": "avg"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    numeric = [r for r in idea.requirements if r.code == "TYPE_IS_NUMERIC"]
    assert numeric and numeric[0].operand == ("ftr", ref)   # code/operand unchanged
    assert numeric[0].schema_version == "v1"                # came through build_requirement
    assert numeric[0].params == ()                          # a no-param code


def test_additivity_requirement_carries_operation_param(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive")])
    _fresh(db, "bank")   # file-declared additivity -> NOT governed -> needs-check
    known, src_of = _kv(["public.accounts.balance"], "bank")
    raw = {"name": "sum_bal", "derives_from": ["public.accounts.balance"], "aggregation": "sum"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    add = [r for r in idea.requirements if r.code == "ADDITIVITY_SUPPORTS_OPERATION"]
    assert add and add[0].operand == ("bank", "public.accounts.balance")
    assert add[0].schema_version == "v1"
    assert dict(add[0].params) == {"operation": "sum"}   # the normalized server-known operation


def test_full_requirement_set_is_registry_validated_and_unchanged(db):
    # A representative multi-requirement feature exercising 5 of the codes at once (additivity, unit,
    # currency, temporal, grain). The SET of (code, operand) is the pre-rewire behavior — locked as a
    # regression guard — and every requirement is now registry-validated (schema_version "v1").
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),   # file-declared grain
        CanonicalRow("t", "accounts", "posted_at", "timestamp", as_of=True),   # file-declared as-of
        CanonicalRow("t", "accounts", "a", "numeric", additivity="semi_additive"),   # no unit/ccy
        CanonicalRow("t", "accounts", "b", "numeric")])   # no unit/ccy
    _fresh(db, "t")
    known, src_of = _kv(["public.accounts.a", "public.accounts.b"], "t")
    raw = {"name": "sum_ab_90d", "derives_from": ["public.accounts.a", "public.accounts.b"],
           "aggregation": "sum_90d", "grain_table": "accounts"}
    idea, rej = _validate_idea(db, raw, known, src_of, None, NOW, FRESH)
    assert rej is None
    assert idea.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert {(r.code, r.operand) for r in idea.requirements} == {
        ("ADDITIVITY_SUPPORTS_OPERATION", ("t", "public.accounts.a")),
        ("ADDITIVITY_SUPPORTS_OPERATION", ("t", "public.accounts.b")),
        ("UNIT_CONSISTENT", ("t", "public.accounts.a")),
        ("UNIT_CONSISTENT", ("t", "public.accounts.b")),
        ("CURRENCY_CONSISTENT", ("t", "public.accounts.a")),
        ("CURRENCY_CONSISTENT", ("t", "public.accounts.b")),
        ("TEMPORAL_IS_POPULATED", ("t", "public.accounts.posted_at")),
        ("GRAIN_IS_UNIQUE", ("t", "public.accounts.id")),
    }
    # every requirement is registry-validated (pinned schema version)
    assert all(r.schema_version == "v1" for r in idea.requirements)
    # the additivity checks carry their typed operation param; currency omits its OPTIONAL ref
    for r in idea.requirements:
        if r.code == "ADDITIVITY_SUPPORTS_OPERATION":
            assert dict(r.params) == {"operation": "sum_90d"}
        if r.code == "CURRENCY_CONSISTENT":
            assert r.params == ()
