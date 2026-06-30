import numbers

import pytest

from featuregen.overlay.catalog import CatalogObject
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.profiler import (
    ProfilerLimits,
    SchemaNotAllowedError,
    _sampling,
    run_profiler_scan,
)


class _Catalog:
    def __init__(self, objects):
        self._objects = list(objects)

    def list_objects(self):
        return list(self._objects)

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def owner_of(self, ref):
        return None

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
            data_type=data_type,
            native_oid=None,
        )
        for name, data_type in specs
    ]


def _ref(schema, table):
    return CatalogObjectRef(
        catalog_source="pg:core", object_kind="table", schema=schema, table=table
    )


def test_non_allowlisted_schema_is_refused(db):
    ref = _ref("restricted", "secrets")
    adapter = _Catalog(_columns(ref, [("id", "integer")]))
    limits = ProfilerLimits(allowed_schemas=frozenset({"public"}))

    with pytest.raises(SchemaNotAllowedError):
        run_profiler_scan(db, adapter, ref, limits=limits)


def test_metric_values_contain_no_raw_column_values(db):
    db.execute(
        "CREATE TABLE prof_pii (customer_id integer, ssn text, balance numeric, posted_at timestamptz)"
    )
    db.execute(
        "INSERT INTO prof_pii "
        "SELECT g, 'SSN-' || g, (g * 100.5)::numeric, now() FROM generate_series(1, 20) AS g"
    )
    ref = _ref("public", "prof_pii")
    adapter = _Catalog(
        _columns(
            ref,
            [
                ("customer_id", "integer"),
                ("ssn", "text"),
                ("balance", "numeric"),
                ("posted_at", "timestamp with time zone"),
            ],
        )
    )
    limits = ProfilerLimits(allowed_schemas=frozenset({"public"}))

    proposals = run_profiler_scan(db, adapter, ref, limits=limits)

    assert proposals  # at least the grain on customer_id
    raw_markers = {"SSN-1", "SSN-20", "100.5", "2010.0"}
    for p in proposals:
        metrics = p.evidence_metrics["metric_values"]
        # every metric is an aggregate number, never a raw cell value.
        for key, value in metrics.items():
            assert isinstance(value, numbers.Number), f"{key}={value!r} is not aggregate-only"
        assert raw_markers.isdisjoint(set(map(str, metrics.values())))
    # statement timeout was applied to the scanning transaction (§5.2).
    shown = db.execute("SHOW statement_timeout").fetchone()[0]
    assert shown == "5s"


def test_column_combination_cap_is_honored(db):
    db.execute("CREATE TABLE prof_pair (a integer, b integer)")
    # singles are non-unique (distinct=2 each); the (a,b) PAIR is unique (4 distinct rows).
    db.execute("INSERT INTO prof_pair (a, b) VALUES (1,1),(1,2),(2,1),(2,2)")
    ref = _ref("public", "prof_pair")
    adapter = _Catalog(_columns(ref, [("a", "integer"), ("b", "integer")]))

    no_combo = run_profiler_scan(
        db, adapter, ref, limits=ProfilerLimits(allowed_schemas=frozenset({"public"}), max_column_combinations=0)
    )
    assert [p for p in no_combo if p.fact_type == "grain"] == []

    with_combo = run_profiler_scan(
        db, adapter, ref, limits=ProfilerLimits(allowed_schemas=frozenset({"public"}), max_column_combinations=4)
    )
    grain = [p for p in with_combo if p.fact_type == "grain"]
    assert len(grain) == 1
    assert grain[0].proposed_value == {"columns": ["a", "b"], "is_unique": True}


def test_sampling_allows_sub_one_percent(db):
    # Above the threshold a huge table samples HONESTLY at <1% — no 1.0 floor (round-5 fix).
    limits = ProfilerLimits(
        allowed_schemas=frozenset({"public"}),
        sample_threshold_rows=1_000_000,
        sample_size=100_000,
    )
    sample_size, clause = _sampling(100_000_000, limits)
    assert sample_size == 100_000  # the cap is enforced as the reported sample_size
    rendered = clause.as_string(db)
    assert "BERNOULLI" in rendered
    pct = float(rendered.split("(")[1].split(")")[0])
    assert pct < 1.0  # sub-1%: the cap is REAL, not a nominal ~1% scan
    assert pct == pytest.approx(0.1)

    # below the threshold there is no sampling — a full scan with an empty clause.
    full_size, full_clause = _sampling(500, limits)
    assert full_size == 500
    assert full_clause.as_string(db) == ""
