"""The Release-B pilot bank: real tables, a catalog that describes them, and the policies that
decide which copy and which rows answer a question.

Extracted from `test_release_b_worked_acceptance`, unchanged in behaviour, because Task 9's seal,
revalidation, explain and execute tests all need the SAME bank — and three hand-rolled copies of a
fixture this load-bearing is how two suites end up asserting against two different banks and both
passing.

FIXTURES ONLY. No live engine, no cluster, no LLM: `create_pilot_tables` writes into the suite's
own rolled-back Postgres transaction, which is the connection `run_analysis` then reads.
"""
from __future__ import annotations

from tests.featuregen.data_agent.pilot_fixture import (
    CUSTOMER_SCHEMA,
    CUSTOMER_TABLE,
    DIMENSION_TABLE,
    SNAPSHOT_TABLE,
    TRANSACTION_SCHEMA,
    TRANSACTION_TABLE,
    create_pilot_tables,
)

from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure, Window
from featuregen.data_agent.binding_store import declare_catalog_engine, record_connection
from featuregen.data_agent.connection import DataSourceConnectionV1
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.serving_policy_store import publish_serving_policy
from featuregen.overlay.upload.source_selection import (
    DatasetNeedRole,
    DatasetServingPolicyRevisionV1,
    ServingPurpose,
    human_declaration_provenance,
)
from featuregen.overlay.upload.temporal_policy import (
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
)

SRC = "ftr"
ARCHIVE_TABLE = "tran_archive"

CUST = normalize_ref(SRC, CUSTOMER_SCHEMA, CUSTOMER_TABLE)
CUST_KEY = normalize_ref(SRC, CUSTOMER_SCHEMA, CUSTOMER_TABLE, "cif_id")
TRAN = normalize_ref(SRC, TRANSACTION_SCHEMA, TRANSACTION_TABLE)
ARCHIVE = normalize_ref(SRC, TRANSACTION_SCHEMA, ARCHIVE_TABLE)
TRAN_KEY = normalize_ref(SRC, TRANSACTION_SCHEMA, TRANSACTION_TABLE, "cif_id")
TRAN_MONTH = normalize_ref(SRC, TRANSACTION_SCHEMA, TRANSACTION_TABLE, "tran_month")
SEG = normalize_ref(SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE)
SEG_COL = normalize_ref(SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "segment")
SEC_COL = normalize_ref(SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "sector")
SEG_FROM = normalize_ref(SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "effective_from")
SEG_TO = normalize_ref(SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "effective_to")
SEG_FLAG = normalize_ref(SRC, CUSTOMER_SCHEMA, DIMENSION_TABLE, "is_current")

#: The SNAPSHOT-shaped dimension source — ENGINE B's half of the pilot bank. The SAME classification
#: kept the other way a bank keeps it (one row per customer per publication instant), beside the
#: SCD2 table rather than instead of it, because the two row rules give DIFFERENT answers and which
#: one applies is a declaration rather than a guess.
SNAP = normalize_ref(SRC, CUSTOMER_SCHEMA, SNAPSHOT_TABLE)
SNAP_SEG = normalize_ref(SRC, CUSTOMER_SCHEMA, SNAPSHOT_TABLE, "segment")
SNAP_SEC = normalize_ref(SRC, CUSTOMER_SCHEMA, SNAPSHOT_TABLE, "sector")
SNAP_DATE = normalize_ref(SRC, CUSTOMER_SCHEMA, SNAPSHOT_TABLE, "snapshot_date")
SNAP_SEQ = normalize_ref(SRC, CUSTOMER_SCHEMA, SNAPSHOT_TABLE, "load_seq")

CUTOFF_REF = "report_cutoff_param"
QUESTION = ("customers whose transaction count decreased last completed month, "
            "by segment and sector")


def build_bank(db, monkeypatch, *, restricted_archive: bool = False,
               snapshot_dimension: bool = False):
    """The pilot's real tables PLUS the catalog, policies and physical routing that address them.

    `database_name` is the test database on purpose: the derived binding's `table_id` must equal
    the one `PILOT_JOIN_EVIDENCE` was observed against, or the verified-join gate would refuse the
    very plan the acceptance is about.

    `restricted_archive` puts the LOSING candidate behind a role claim the default caller does not
    hold — the shape Task 9's read-scoped explain has to render as a COUNT and never as a name.

    `snapshot_dimension` describes and governs the SNAPSHOT-shaped dimension table as well, so a
    plan can ask the SAME question of the other row rule. Off by default: adding it unconditionally
    would put a second dimension table in every suite's catalog, and the tables are deliberately
    indistinguishable in shape.
    """
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")
    create_pilot_tables(db)
    build_graph(db, SRC, [
        CanonicalRow(SRC, CUSTOMER_TABLE, "cif_id", "text", is_grain=True, entity="Customer"),
        CanonicalRow(SRC, TRANSACTION_TABLE, "cif_id", "text"),
        CanonicalRow(SRC, TRANSACTION_TABLE, "tran_month", "text", as_of=True),
        # The ARCHIVE copy: same shape, same entity — indistinguishable to the catalog, which is
        # exactly why a declaration rather than a ranking has to choose between them.
        CanonicalRow(SRC, ARCHIVE_TABLE, "cif_id", "text"),
        CanonicalRow(SRC, ARCHIVE_TABLE, "tran_month", "text", as_of=True),
        CanonicalRow(SRC, DIMENSION_TABLE, "cif_id", "text", is_grain=True),
        CanonicalRow(SRC, DIMENSION_TABLE, "segment", "text"),
        CanonicalRow(SRC, DIMENSION_TABLE, "sector", "text"),
        CanonicalRow(SRC, DIMENSION_TABLE, "effective_from", "timestamp"),
        CanonicalRow(SRC, DIMENSION_TABLE, "effective_to", "timestamp"),
        CanonicalRow(SRC, DIMENSION_TABLE, "is_current", "boolean"),
        *([
            CanonicalRow(SRC, SNAPSHOT_TABLE, "cif_id", "text", is_grain=True),
            CanonicalRow(SRC, SNAPSHOT_TABLE, "segment", "text"),
            CanonicalRow(SRC, SNAPSHOT_TABLE, "sector", "text"),
            CanonicalRow(SRC, SNAPSHOT_TABLE, "snapshot_date", "text", as_of=True),
            CanonicalRow(SRC, SNAPSHOT_TABLE, "load_seq", "int"),
        ] if snapshot_dimension else []),
    ])
    db.execute("UPDATE graph_node SET schema_name = %s WHERE catalog_source = %s",
               (CUSTOMER_SCHEMA, SRC))
    record_connection(db, DataSourceConnectionV1(
        connection_id="pilot-pg", environment_id="dev", kind="postgres", host="127.0.0.1",
        port=5432, auth_mechanism="password", secret_ref="vault://featuregen/pg",
        execution_principal="svc_ro", allowed_schemas=frozenset({CUSTOMER_SCHEMA}), active=True),
        tier="pilot", database_name="featuregen_test")
    declare_catalog_engine(db, catalog_source=SRC, engine="postgres", tier="pilot",
                           declared_by="user:priya")

    # The EVENT source is declared by policy — the archive is eligible, the live table preferred.
    publish_serving_policy(db, DatasetServingPolicyRevisionV1(
        entity_id="customer", need_role=DatasetNeedRole.EVENT_SOURCE,
        serving_purpose=ServingPurpose.ANALYTICAL,
        eligible_dataset_refs=(TRAN, ARCHIVE), preferred_dataset_refs=(TRAN,),
        provenance=human_declaration_provenance(producer_ref="user:priya")),
        expected_pointer_version=0, actor="user:priya")
    # The dimension source keeps SCD2 history: a HISTORICAL question reads the row valid at the
    # cutoff, and a CURRENT one would read the flagged row — two different answers, declared.
    publish_temporal_policy_for(db)
    if snapshot_dimension:
        publish_snapshot_policy_for(db)
    if restricted_archive:
        # AFTER the policies are published, deliberately: `publish_serving_policy` validates that
        # its AUTHOR can see every dataset it names, so hiding the archive first would refuse the
        # very policy that makes it a considered candidate. The shape being built is a policy
        # authored by someone with wide scope and READ by someone without it.
        #
        # D11 derived anchor scope: a table is visible when >= 1 of its COLUMNS is, so hiding the
        # archive means hiding every one of its columns. The enforcement column `visible_requires`
        # is GENERATED (migration 1032) — the SENSITIVITY tag is its input, and the database
        # refuses a direct write to the generated column.
        db.execute(
            "UPDATE graph_node SET sensitivity = 'restricted' "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
            (SRC, ARCHIVE_TABLE))
    return db


def publish_temporal_policy_for(db, **over):
    from featuregen.overlay.upload.temporal_policy_store import publish_temporal_policy

    kw = {"dataset_logical_ref": SEG, "temporal_storage_model": TemporalStorageModel.SCD2,
          "current_selection": TemporalSelectionKind.CURRENT_RECORD,
          "historical_selection": TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
          "effective_from_ref": SEG_FROM, "effective_to_ref": SEG_TO,
          "current_flag_ref": SEG_FLAG,
          "provenance": human_declaration_provenance(producer_ref="user:priya")}
    expected = over.pop("expected_pointer_version", 0)
    kw.update(over)
    return publish_temporal_policy(db, DatasetTemporalPolicyRevisionV1(**kw),
                                   expected_pointer_version=expected, actor="user:priya")


def publish_snapshot_policy_for(db, **over):
    """ENGINE B's declaration for the snapshot table.

    `tie_break_refs` is `load_seq` and that is load-bearing rather than decorative: C4 has TWO rows
    stamped on the same snapshot date, and without a governed tie-breaker the engine REFUSES
    (`TEMPORAL_SNAPSHOT_TIE`) rather than picking one — which is the honest behaviour and the reason
    the pin exists.
    """
    from featuregen.overlay.upload.temporal_policy_store import publish_temporal_policy

    kw = {"dataset_logical_ref": SNAP, "temporal_storage_model": TemporalStorageModel.SNAPSHOT,
          # The dataset has no notion of "the current row" other than its latest snapshot, so both
          # questions read the same rule. Declaring `current_record` would need a flag column the
          # table does not have.
          "current_selection": TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
          "historical_selection": TemporalSelectionKind.LATEST_SNAPSHOT_AS_OF,
          "snapshot_ref": SNAP_DATE, "tie_break_refs": (SNAP_SEQ,),
          "provenance": human_declaration_provenance(producer_ref="user:priya")}
    expected = over.pop("expected_pointer_version", 0)
    kw.update(over)
    return publish_temporal_policy(db, DatasetTemporalPolicyRevisionV1(**kw),
                                   expected_pointer_version=expected, actor="user:priya")


def pilot_snapshot_plan(**over) -> AnalysisPlanV1:
    """The SAME pilot question, asked of the snapshot-shaped dimension source."""
    kw = {"dimensions": (Dimension(logical_ref=SNAP_SEG), Dimension(logical_ref=SNAP_SEC))}
    kw.update(over)
    return pilot_plan(**kw)


def pilot_plan(**over) -> AnalysisPlanV1:
    kw = {
        "question": QUESTION, "entity": "customer", "entity_ref": TRAN_KEY,
        "base_table_ref": TRAN, "measure": Measure(op="count"),
        "windows": (
            Window(anchor_ref=TRAN_MONTH, length_days=30, offset_days=0, label="current",
                   calendar_unit="month", calendar_length=1, calendar_offset=0),
            Window(anchor_ref=TRAN_MONTH, length_days=30, offset_days=30, label="previous",
                   calendar_unit="month", calendar_length=1, calendar_offset=1)),
        "dimensions": (Dimension(logical_ref=SEG_COL), Dimension(logical_ref=SEC_COL)),
        "comparison": "decrease",
        # THE DECLARATION. `spine.py`'s doctrine: governed facts may VALIDATE this, never make it.
        "population_table_ref": CUST, "population_key_ref": CUST_KEY,
    }
    kw.update(over)
    return AnalysisPlanV1(**kw)
