from datetime import UTC, datetime, timedelta

from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import RejectCode, _validate_idea
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

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
