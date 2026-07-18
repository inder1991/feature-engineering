from datetime import UTC, datetime, timedelta

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import RejectCode, _validate_idea
from featuregen.overlay.upload.graph import build_graph

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
