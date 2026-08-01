"""Attested data types — `graph_node.data_type` upgraded ONLY from a real engine read (Task 7).

The live defect this closes: every column on the deployed catalogs is `data_type='unknown'` with
only a glossary-declared type, so every `type_basis` derivation reports the weaker `declared`
basis. The fix must be HONEST end to end:

* an engine-reported type upgrades `unknown` and records provenance (the observation ref);
* a declared value NEVER becomes the attested value — not even its own column's;
* a re-run with a CHANGED engine type never silently overwrites — it records a typed conflict
  for drift handling;
* a column the physical table does not have stays `unknown`, and the absence is itself recorded;
* downstream, `bridge_candidates._resolve_family` (data_type first, declared_type fallback)
  automatically sees `attested` — pinned here on a real candidate re-derivation.
"""
from __future__ import annotations

from featuregen.data_agent.results import ColumnTypeObservationV1, SchemaObservationResultV1
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.type_attestation import attest_types_from_observation

_PHYSICAL_ID = "ftr::banking::dpl_eib::tran_repos"


def _observation(types: dict[str, str], *, complete: bool = True,
                 failures: tuple[str, ...] = ()) -> SchemaObservationResultV1:
    return SchemaObservationResultV1(
        physical_id=_PHYSICAL_ID,
        columns=tuple(ColumnTypeObservationV1(column=c, engine_type=t)
                      for c, t in types.items()),
        complete=complete, failures=failures)


def _glossary_table(db, source="ftr", table="tran_repos",
                    columns=(("cif_id", "string"), ("tran_amt", "decimal(18,2)"))):
    """A GLOSSARY-sourced table: `data_type='unknown'` on every column, the file's own answer in
    `declared_type` — exactly what the FTR adapter produces and what the live catalogs hold."""
    rows = [CanonicalRow(source, table, name, "unknown") for name, _ in columns]
    build_graph(db, source, rows,
                declared_types={f"public.{table}.{name}": declared for name, declared in columns})
    return rows


def _data_type(db, source, table, column):
    return db.execute(
        "SELECT data_type FROM graph_node WHERE catalog_source=%s AND kind='column' "
        "AND table_name=%s AND column_name=%s", (source, table, column)).fetchone()[0]


# ── the upgrade: engine-reported types only, with provenance ─────────────────────────────────────

def test_an_engine_reported_type_upgrades_unknown_and_records_provenance(db):
    _glossary_table(db)
    report = attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({"cif_id": "string", "tran_amt": "decimal(18,2)"}))
    assert _data_type(db, "ftr", "tran_repos", "cif_id") == "string"
    assert _data_type(db, "ftr", "tran_repos", "tran_amt") == "decimal(18,2)"
    assert set(report.upgraded) == {"public.tran_repos.cif_id", "public.tran_repos.tran_amt"}
    assert report.conflicts == () and report.absent == ()
    assert report.observation_ref, "the report must carry the ref the evidence cites"

    evidence = read_active_field_evidence(db, "ftr::public.tran_repos.cif_id", "data_type")
    assert len(evidence) == 1
    assert evidence[0].proposed_value == "string"
    assert evidence[0].producer == "structural_connector"
    assert evidence[0].strength == "attested"
    assert evidence[0].producer_ref == report.observation_ref


def test_a_declared_only_column_is_never_upgraded_from_its_declared_value(db):
    """THE honesty rule. `declared_type='string'` is someone's spreadsheet entry; if the physical
    table does not carry the column, `data_type` stays `unknown` — the declared value must never
    launder itself into the attested slot, and the absence is recorded, not silent."""
    _glossary_table(db)
    report = attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({"tran_amt": "decimal(18,2)"}))   # cif_id physically absent
    assert _data_type(db, "ftr", "tran_repos", "cif_id") == "unknown"
    assert report.absent == ("public.tran_repos.cif_id",)
    assert read_active_field_evidence(db, "ftr::public.tran_repos.cif_id", "data_type") == []


def test_an_empty_observation_upgrades_nothing(db):
    _glossary_table(db)
    report = attest_types_from_observation(
        db, source="ftr", table="tran_repos", observation=_observation({}))
    assert _data_type(db, "ftr", "tran_repos", "cif_id") == "unknown"
    assert _data_type(db, "ftr", "tran_repos", "tran_amt") == "unknown"
    assert report.upgraded == ()
    assert set(report.absent) == {"public.tran_repos.cif_id", "public.tran_repos.tran_amt"}


def test_an_incomplete_observation_attests_nothing(db):
    """A failed DESCRIBE proves nothing — in particular it cannot distinguish "absent" from
    "unread", so acting on it would record absences that are actually engine errors."""
    _glossary_table(db)
    report = attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({}, complete=False, failures=("Table not found",)))
    assert report.skipped_reason == "observation_incomplete"
    assert report.upgraded == () and report.absent == ()
    assert _data_type(db, "ftr", "tran_repos", "cif_id") == "unknown"


# ── drift: a changed engine type is a conflict, never a silent overwrite ─────────────────────────

def test_a_changed_engine_type_records_a_conflict_not_an_overwrite(db):
    _glossary_table(db)
    attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({"cif_id": "string", "tran_amt": "decimal(18,2)"}))
    report = attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({"cif_id": "bigint", "tran_amt": "decimal(18,2)"}))
    assert _data_type(db, "ftr", "tran_repos", "cif_id") == "string", "no silent overwrite"
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.object_ref == "public.tran_repos.cif_id"
    assert conflict.stored_type == "string"
    assert conflict.observed_type == "bigint"
    assert conflict.observation_ref == report.observation_ref


def test_an_upload_attested_type_is_not_overwritten_either(db):
    """A technical upload writes a real operational `data_type`. The engine disagreeing with it is
    the same drift signal — recorded, never resolved by overwriting the uploader's attestation."""
    row = CanonicalRow("core", "customer_master", "customer_id", "integer")
    build_graph(db, "core", [row])
    report = attest_types_from_observation(
        db, source="core", table="customer_master",
        observation=_observation({"customer_id": "varchar(20)"}))
    assert _data_type(db, "core", "customer_master", "customer_id") == "integer"
    assert len(report.conflicts) == 1
    assert (report.conflicts[0].stored_type, report.conflicts[0].observed_type) == \
        ("integer", "varchar(20)")


def test_an_agreeing_rerun_is_idempotent(db):
    """Same engine type again: no second upgrade, no duplicate evidence (input-hash reuse — the
    module convention), the column reported as corroborated."""
    _glossary_table(db)
    first = attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({"cif_id": "string", "tran_amt": "decimal(18,2)"}))
    second = attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({"cif_id": "string", "tran_amt": "decimal(18,2)"}))
    assert first.upgraded and second.upgraded == ()
    assert set(second.corroborated) == {"public.tran_repos.cif_id", "public.tran_repos.tran_amt"}
    assert second.conflicts == ()
    evidence = read_active_field_evidence(db, "ftr::public.tran_repos.cif_id", "data_type")
    assert len(evidence) == 1, "an unchanged engine claim is reused, not re-written"


def test_a_physical_only_column_is_reported_not_invented(db):
    """The engine holding a column the catalog does not know about is reconciliation input — it
    must never create a graph node as a side effect of type attestation."""
    _glossary_table(db, columns=(("cif_id", "string"),))
    report = attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({"cif_id": "string", "audit_ts": "timestamp"}))
    assert report.physical_only == ("audit_ts",)
    assert db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='ftr' AND column_name='audit_ts'"
    ).fetchone()[0] == 0


# ── downstream: type_basis flips declared -> attested on re-derivation ───────────────────────────

def _glossary_identifier(db, source, table, column, *, declared="string"):
    row = CanonicalRow(source, table, column, "unknown")
    build_graph(db, source, [row], concepts={content_hash(row): "customer_id"},
                declared_types={f"public.{table}.{column}": declared})


def test_an_attested_type_flips_a_candidates_type_basis_on_rederivation(db):
    """`_resolve_family` reads `data_type` first and falls back to `declared_type` — so once the
    engine attests both endpoints, the SAME candidate (identity deliberately excludes the basis)
    re-derives as `attested` with no further code in the loop."""
    _glossary_identifier(db, "ftr", "tran_repos", "cif_id", declared="string")
    _glossary_identifier(db, "cib", "bo_cib_customer", "cust_num", declared="varchar(150)")
    before = derive_bridge_candidates(db)
    assert len(before) == 1 and before[0].type_basis == "declared"

    attest_types_from_observation(
        db, source="ftr", table="tran_repos",
        observation=_observation({"cif_id": "string"}))
    attest_types_from_observation(
        db, source="cib", table="bo_cib_customer",
        observation=_observation({"cust_num": "varchar(150)"}))

    after = derive_bridge_candidates(db)
    assert len(after) == 1
    assert after[0].type_basis == "attested"
    assert after[0].data_type_family == "text"
    assert after[0].candidate_id == before[0].candidate_id, \
        "attesting the type strengthens the SAME candidate — it must not fork a second one"
