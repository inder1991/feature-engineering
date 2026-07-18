from featuregen.overlay.upload.feature_assist import route_strategies


def _ftr_col(db, table, column, *, data_type="unknown", declared_type=None):
    ref = f"public.{table}.{column}"
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, declared_type) VALUES ('ftr', %s, 'column', %s, %s, %s, %s)",
        (ref, table, column, data_type, declared_type))
    return {"catalog_source": "ftr", "object_ref": ref, "table": table, "column": column}


def test_declared_numeric_enables_ratio_while_data_type_unknown(db):
    cols = [_ftr_col(db, "loans", "balance", declared_type="numeric"),
            _ftr_col(db, "loans", "rate", declared_type="numeric")]
    picks = dict(route_strategies(db, cols))
    assert "ratio" in picks          # declared numeric hint enables the numeric strategy...
    # ...even though operational data_type is permanently 'unknown' for FTR.
    row = db.execute("SELECT data_type FROM graph_node WHERE object_ref = 'public.loans.balance'"
                     ).fetchone()
    assert row[0] == "unknown"


def test_no_declared_and_unknown_data_type_does_not_enable_ratio(db):
    cols = [_ftr_col(db, "loans", "a"), _ftr_col(db, "loans", "b")]   # both unknown, no declared hint
    picks = dict(route_strategies(db, cols))
    assert "ratio" not in picks
