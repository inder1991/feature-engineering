"""S3 — the generation inventory observation and the bound input set, persisted (1074).

Two acceptance clauses: *"binding is unreachable without an inventory"* and *"the bound set is
addressable independently of any policy"*. The first is tested three ways — the named refusal, the
database's own foreign key, and the absence of any writer that could bypass both. The second is
tested by absence, which is the only way to test it: a policy dependency that does not exist cannot
be asserted into view.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from featuregen.materialize.inventory import ClusterInventoryV1, EngineVersions, TableLayout
from featuregen.overlay.upload import inventory_store
from featuregen.overlay.upload.inventory_revisions import (
    BoundInputSetRevisionV2,
    BoundInputV2,
    GenerationInventoryObservationV1,
)
from featuregen.overlay.upload.inventory_store import (
    BindingWithoutInventory,
    read_bound_input_set,
    record_bound_input_set,
    record_inventory_observation,
    same_identity_observations,
)

TXN = "hdfc::public.transactions.txn_amt"
ACCT = "hdfc::public.transactions.acct_id"
MIGRATION = Path("src/featuregen/db/migrations/1074_inventory_and_bound_input_set.sql")


def _code_only(module) -> str:
    """A module's source with every docstring and comment removed.

    Written rather than reached for because the alternative — grepping whole files — reads a
    module's explanation of what it does NOT do as evidence that it does it.
    """
    lines, inside = [], False
    for raw in inspect.getsource(module).splitlines():
        line = raw.split("#", 1)[0] if not inside else raw
        fences = line.count('"""')
        if inside:
            if fences:
                inside = False
                line = line.split('"""', 1)[1]
            else:
                continue
        elif fences == 1:
            inside = True
            line = line.split('"""', 1)[0]
        elif fences >= 2:
            line = line.split('"""')[0] + line.rsplit('"""', 1)[1]
        lines.append(line)
    return "\n".join(lines)


def _versions(**overrides) -> EngineVersions:
    kwargs = dict(hive="3.1.2", spark="3.3.0", metastore="3.1.2", python="3.11.14",
                  java="11.0.20", pyspark="3.3.0", kedro="0.19.3", kedro_datasets="2.1.0")
    kwargs.update(overrides)
    return EngineVersions(**kwargs)


def _layout(table: str = "transactions") -> TableLayout:
    return TableLayout(
        schema="public", table=table, partition_columns=(("load_dt", "string"),),
        partition_mapping=None, columns=(("txn_amt", "decimal(18,2)"), ("acct_id", "string")),
        location=f"hdfs://nn/warehouse/public.db/{table}", rewritten_in_place=False)


def _inventory(*, tables=None, versions=None, captured_at="2026-08-17T00:00:00Z"):
    return ClusterInventoryV1(
        environment_id="hdfc-local",
        tables=tables if tables is not None else {"public.transactions": _layout()},
        logical_schema_map={"hdfc::public.transactions": "public"},
        engine_versions=versions or _versions(), captured_at=captured_at)


def _observation(observation_id="obs-1", **overrides) -> GenerationInventoryObservationV1:
    kwargs = dict(observation_id=observation_id, inventory=_inventory(),
                  used_logical_schema_refs=("hdfc::public.transactions",),
                  read_set=(TXN, ACCT))
    kwargs.update(overrides)
    return GenerationInventoryObservationV1(**kwargs)


def _bound(revision_id="bis-1") -> BoundInputSetRevisionV2:
    return BoundInputSetRevisionV2(
        revision_id=revision_id, environment_id="hdfc-local",
        inputs=(BoundInputV2(TXN, "public.transactions", "txn_amt"),
                BoundInputV2(ACCT, "public.transactions", "acct_id")))


def _seed(db, observation_id="obs-1") -> str:
    return record_inventory_observation(
        db, _observation(observation_id), captured_at="2026-08-17T00:00:00Z")


# ══ ACCEPTANCE 1 — binding is UNREACHABLE without an inventory ═══════════════════════════════════
def test_BINDING_WITHOUT_AN_INVENTORY_REFUSES_BY_NAME(db):
    """A binding resolves logical refs against an environment somebody looked at. Without one it
    describes a resolution that could not have happened — and would be indistinguishable from one
    that did."""
    with pytest.raises(BindingWithoutInventory, match="could not have happened"):
        record_bound_input_set(db, _bound(), inventory_observation_id="  ")


def test_THE_DATABASE_ENFORCES_IT_TOO(db):
    """The same rule reached by a caller that bypasses the writer — a foreign key, not a check
    somebody remembers to run."""
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        record_bound_input_set(db, _bound(), inventory_observation_id="obs-does-not-exist")


def test_the_writer_CANNOT_be_called_without_an_observation():
    """Not a defaulted parameter: a default would make the unbound call the easy one to write."""
    parameter = inspect.signature(record_bound_input_set).parameters["inventory_observation_id"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_a_binding_WITH_an_inventory_records_and_reads_back(db):
    _seed(db)
    record_bound_input_set(db, _bound(), inventory_observation_id="obs-1")

    loaded = read_bound_input_set(db, "bis-1")
    assert loaded == _bound()
    assert loaded.datasets == ("public.transactions",)


def test_the_migration_states_the_rule_as_a_FOREIGN_KEY():
    sql = MIGRATION.read_text()
    assert "inventory_observation_id text NOT NULL" in sql
    assert "REFERENCES generation_inventory_observation(observation_id)" in sql


# ══ ACCEPTANCE 2 — the bound set is addressable independently of any POLICY ══════════════════════
def test_NOTHING_IN_THE_BOUND_SET_PATH_TOUCHES_A_POLICY():
    """Tested by absence, which is the only way: a dependency that does not exist cannot be
    asserted into view. C-C7's occurrence derivation CONSUMES a bound set — the reverse dependency
    would leave neither constructible first."""
    # The CODE, not the prose. This module's docstrings explain at length that it touches no
    # policy, and a whole-file grep would read that explanation as the thing it disclaims — so
    # docstrings and comments are stripped before the check.
    for policy_shaped in ("policy", "occurrence", "realization", "authority_ref"):
        assert policy_shaped not in _code_only(inventory_store).lower(), policy_shaped


def test_the_SCHEMA_carries_no_policy_column_either():
    sql = MIGRATION.read_text()
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    for policy_shaped in ("policy", "occurrence", "realization"):
        assert policy_shaped not in statements.lower(), policy_shaped


def test_a_bound_set_is_READABLE_with_no_policy_in_the_database(db):
    """The behavioural half: nothing is seeded but an inventory, and the bound set still resolves
    completely."""
    _seed(db)
    record_bound_input_set(db, _bound(), inventory_observation_id="obs-1")
    assert len(read_bound_input_set(db, "bis-1").inputs) == 2


# ══ C-B7's two gates, now against the database ═══════════════════════════════════════════════════
def test_AN_IDENTICAL_RECAPTURE_SHARES_ONE_IDENTITY(db):
    """New id, new capture time, same environment. Both rows exist — they are different
    observations — and a compilation treats them as the same environment."""
    record_inventory_observation(db, _observation("obs-1"), captured_at="2026-08-17T00:00:00Z")
    record_inventory_observation(
        db, _observation("obs-2", inventory=_inventory(captured_at="2026-12-25T09:30:00Z")),
        captured_at="2026-12-25T09:30:00Z")

    assert same_identity_observations(db, _observation().content_hash) == ("obs-1", "obs-2")


def test_AN_UNRELATED_TABLE_DOES_NOT_INVALIDATE_ANYTHING(db):
    """Otherwise adding a table to a cluster would invalidate every compiled feature."""
    wider = _inventory(tables={"public.transactions": _layout(),
                               "public.marketing_campaigns": _layout("marketing_campaigns")})
    record_inventory_observation(db, _observation("obs-1"), captured_at="t1")
    record_inventory_observation(db, _observation("obs-wide", inventory=wider), captured_at="t2")

    assert set(same_identity_observations(db, _observation().content_hash)) == {
        "obs-1", "obs-wide"}


def test_a_layout_change_IN_THE_READ_SET_does_change_identity(db):
    """The other side of the same rule — otherwise the narrowing would make identity meaningless."""
    changed = _inventory(tables={"public.transactions": TableLayout(
        schema="public", table="transactions", partition_columns=(("load_dt", "string"),),
        partition_mapping=None, columns=(("txn_amt", "decimal(38,2)"), ("acct_id", "string")),
        location="hdfs://nn/warehouse/public.db/transactions", rewritten_in_place=False)})
    record_inventory_observation(db, _observation("obs-1"), captured_at="t1")
    record_inventory_observation(db, _observation("obs-changed", inventory=changed),
                                 captured_at="t2")

    assert same_identity_observations(db, _observation().content_hash) == ("obs-1",)


def test_the_COMPLETE_observation_is_stored_even_though_it_is_not_hashed(db):
    """Audit needs the whole snapshot; identity needs a slice of it."""
    wider = _inventory(tables={"public.transactions": _layout(),
                               "public.marketing_campaigns": _layout("marketing_campaigns")})
    record_inventory_observation(db, _observation("obs-wide", inventory=wider), captured_at="t1")
    stored = db.execute(
        "SELECT inventory_json FROM generation_inventory_observation WHERE observation_id = %s",
        ("obs-wide",)).fetchone()[0]
    assert "public.marketing_campaigns" in stored["tables"]


def test_the_capture_time_is_stored_and_NOT_hashed(db):
    _seed(db)
    row = db.execute(
        "SELECT captured_at, content_hash FROM generation_inventory_observation "
        "WHERE observation_id = %s", ("obs-1",)).fetchone()
    assert row[0] == "2026-08-17T00:00:00Z"
    assert row[1] == _observation().content_hash


# ══ append-only ══════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("table,column,key", [
    ("generation_inventory_observation", "content_hash", "observation_id"),
    ("bound_input_set_revision", "content_hash", "revision_id"),
])
def test_both_records_are_APPEND_ONLY(db, table, column, key):
    """An observation is what a compilation's identity was computed FROM and a bound set is what it
    resolved TO. Either being editable would let a sealed artifact's inputs be restated without the
    artifact changing."""
    import psycopg

    _seed(db)
    record_bound_input_set(db, _bound(), inventory_observation_id="obs-1")
    identifier = "obs-1" if key == "observation_id" else "bis-1"
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute(f"UPDATE {table} SET {column} = %s WHERE {key} = %s",
                   ("sha256:rewritten", identifier))


def test_recording_is_idempotent(db):
    _seed(db)
    _seed(db)
    record_bound_input_set(db, _bound(), inventory_observation_id="obs-1")
    record_bound_input_set(db, _bound(), inventory_observation_id="obs-1")
    assert len(read_bound_input_set(db, "bis-1").inputs) == 2


def test_a_ref_bound_twice_is_refused_before_it_reaches_the_database():
    """One ref resolves to one place; two bindings make which applies a row-order accident."""
    with pytest.raises(ValueError, match="tuple-order accident"):
        BoundInputSetRevisionV2(
            revision_id="bis-x", environment_id="hdfc-local",
            inputs=(BoundInputV2(TXN, "public.a", "txn_amt"),
                    BoundInputV2(TXN, "public.b", "txn_amt")))
