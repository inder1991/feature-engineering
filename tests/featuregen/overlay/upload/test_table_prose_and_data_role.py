"""Migration-1052 projections: `graph_node.data_role` + the table-prose search-doc slots.

Two mechanisms, both required by the Release-A consumption step and both DISPLAY-ONLY:

* **`data_role`** is DERIVED at projection time from the node's own `table_role` (+ the
  `event_or_snapshot` that splits the legacy `fact` role), through the ONE adapter
  `profile_vocab.data_role_from_table_role`. It exists as a literal column because the facet
  mechanism reads literal `graph_node` columns and nothing else; it is never a second evidence
  field, and the flat column must agree with `build_dataset_profile` — which derives the same
  value from the same two resolutions — or search and the dossier would disagree about what a
  table is.

* **The table search-doc slots.** `_search_doc_params` hardcoded `""` for a table's definition,
  and `business_context` had no flat column at all — so the only prose a TECHNICAL catalog ever
  has about a table was unmatchable by full-text search. The parity invariant is the point: ONE
  expression, ONE parameter builder, shared by the INSERT sites and by `rebuild_search_doc`, so an
  insert-time document and a rebuilt one can never disagree.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.dataset_profiles import build_dataset_profile
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import (
    _SEARCH_DOC,
    _SEARCH_DOC_SLOTS,
    _search_doc_params,
    rebuild_search_doc,
    rebuild_search_docs,
)
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_vocab import DataRole
from featuregen.overlay.upload.search import search

_SRC = "ftr"
_TABLE = "party_xref"
_TABLE_OBJECT_REF = f"public.{_TABLE}"
_TABLE_REF = normalize_ref(_SRC, None, _TABLE)

ACTOR = mint_test_identity(subject="user:curator", role_claims=("data_owner",))


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


_NOW = datetime(2026, 8, 1, tzinfo=UTC)
#: Long enough that no test in this file is about the freshness SLA (search joins the drift
#: watermark, which only a real ingest writes).
_FRESH = timedelta(days=3650)


def _seed_graph(db) -> None:
    """A real two-column upload — `ingest_upload`, not a bare `build_graph`, because search joins
    the source's drift watermark and a graph without one is invisible to every query."""
    ingest_upload(db, _SRC, [CanonicalRow(_SRC, _TABLE, "cif_id", "text"),
                             CanonicalRow(_SRC, _TABLE, "legacy_id", "text")],
                  actor=ACTOR, now=_NOW)


def _hits(db, query: str):
    return search(db, query, now=_NOW, fresh_within=_FRESH).hits


def _seed_evidence(db, field: str, value: str, *,
                   producer: EvidenceProducer = EvidenceProducer.SOURCE,
                   strength: AssertionStrength = AssertionStrength.ATTESTED) -> None:
    record_field_evidence(
        db, logical_ref=_TABLE_REF, field_name=field, proposed_value=value,
        producer=producer, strength=strength, producer_ref="test-producer",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=_TABLE_REF, field_name=field,
                                    material=f"{value}:{producer.value}:{strength.value}"))


def _project(db) -> None:
    resolve_and_project(db, source=_SRC, logical_refs=[_TABLE_REF])


def _table_row(db, *columns: str):
    return db.execute(
        f"SELECT {', '.join(columns)} FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'table'",
        (_SRC, _TABLE_OBJECT_REF)).fetchone()


# ── data_role: a derived projection, never a second evidence field ──────────────────────────────


def test_data_role_projects_legacy_bridge_as_crosswalk_without_rewriting_evidence(db) -> None:
    _seed_graph(db)
    _seed_evidence(db, "table_role", "bridge")
    _project(db)

    assert _table_row(db, "table_role", "data_role") == ("bridge", DataRole.CROSSWALK.value)
    # The EVIDENCE still says `bridge` — the display remap must not re-key one stored row.
    stored = db.execute(
        "SELECT proposed_value FROM field_evidence WHERE logical_ref = %s "
        "AND field_name = 'table_role'", (_TABLE_REF,)).fetchall()
    assert [r[0] for r in stored] == ["bridge"]


def test_data_role_follows_the_event_or_snapshot_split_whichever_lands_second(db) -> None:
    """`data_role` has two inputs and either can move alone, so BOTH projections must re-derive it.
    Projecting `table_role='fact'` first yields the legacy `fact` role; the later
    `event_or_snapshot` projection must then sharpen it — otherwise the flat column keeps a value
    derived from an input that no longer holds."""
    _seed_graph(db)
    _seed_evidence(db, "table_role", "fact")
    _project(db)
    assert _table_row(db, "data_role") == (DataRole.FACT.value,)

    _seed_evidence(db, "event_or_snapshot", "event")
    _project(db)
    assert _table_row(db, "data_role") == (DataRole.EVENT_FACT.value,)


def test_data_role_is_null_not_unknown_when_nobody_has_classified_the_table(db) -> None:
    """`unknown` is a vocabulary member meaning "we looked and it is off-vocabulary"; absence means
    nobody has said anything. The facet must be able to tell those apart, so an unclassified table
    projects NULL — never a fabricated `unknown` bucket."""
    _seed_graph(db)
    _seed_evidence(db, "business_context", "Maps legacy party ids to CIF.")
    _project(db)
    assert _table_row(db, "table_role", "data_role") == (None, None)


def test_flat_data_role_agrees_with_the_assembled_dataset_profile(db) -> None:
    """One value, two surfaces (search facet and dossier). They derive from the SAME two
    resolutions through the SAME adapter; a disagreement here would mean the catalog says a table
    is a crosswalk in one place and a dimension in another."""
    _seed_graph(db)
    _seed_evidence(db, "table_role", "dim")
    _project(db)

    profile = build_dataset_profile(db, source=_SRC, dataset_logical_ref=_TABLE_REF)
    assert profile is not None
    assert profile.data_role.display is not None
    assert _table_row(db, "data_role") == (profile.data_role.display.value,)
    assert profile.data_role.display.value == DataRole.DIMENSION.value


def test_data_role_retracts_when_its_input_projection_is_cleared(db) -> None:
    """A rebuildable projection must be able to go BACK to nothing: `build_graph` wipes the graph
    on every upload, and a re-project with no surviving table_role evidence must leave `data_role`
    NULL rather than stranding a stale classification."""
    _seed_graph(db)
    _seed_evidence(db, "table_role", "reference")
    _project(db)
    assert _table_row(db, "data_role") == (DataRole.REFERENCE.value,)

    db.execute("UPDATE field_evidence SET lifecycle = 'rejected' WHERE logical_ref = %s "
               "AND field_name = 'table_role'", (_TABLE_REF,))
    _seed_evidence(db, "business_context", "still described")   # something to re-resolve
    _project(db)
    # table_role has no active evidence left, so nothing re-resolves it; the graph rebuild is what
    # clears the display. Prove the derivation follows the input rather than persisting alone.
    db.execute("UPDATE graph_node SET table_role = NULL WHERE catalog_source = %s "
               "AND object_ref = %s", (_SRC, _TABLE_OBJECT_REF))
    _seed_evidence(db, "event_or_snapshot", "snapshot")
    _project(db)
    assert _table_row(db, "data_role") == (None,)


# ── the search-doc slots: insert-time / rebuild parity ──────────────────────────────────────────


def test_search_doc_expression_and_parameter_builder_agree_on_slot_count() -> None:
    """The import-time assertion made executable: a slot added to the expression but not to the
    builder mis-binds every INSERT in the module, silently shifting definition into the name
    weight. Pinned so the failure is a test, not a corrupted index."""
    assert _SEARCH_DOC.count("%s") == _SEARCH_DOC_SLOTS
    assert len(_search_doc_params("table", "t", None, None, None, None, None, None)) == (
        _SEARCH_DOC_SLOTS)
    assert len(_search_doc_params("column", "t", "c", "d", None, None, None, None)) == (
        _SEARCH_DOC_SLOTS)


def test_table_definition_and_business_context_reach_the_table_search_document(db) -> None:
    _seal()
    _seed_graph(db)
    _seed_evidence(db, "definition", "Crosswalk of legacy party identifiers.")
    _seed_evidence(db, "business_context",
                   "Owned by financial crime operations for sanctions screening.")
    _project(db)

    # A word that appears ONLY in the table definition finds the TABLE node.
    assert [h.object_ref for h in _hits(db, "crosswalk") if h.kind == "table"] == [
        _TABLE_OBJECT_REF]
    # A word that appears ONLY in the business context does too — for a technical catalog this is
    # the only prose a table has at all.
    assert [h.object_ref for h in _hits(db, "sanctions") if h.kind == "table"] == [
        _TABLE_OBJECT_REF]


def test_table_prose_is_not_copied_into_column_documents(db) -> None:
    """The plan is explicit: join/derive, do not copy the same prose into every column record. A
    column matching on its table's business context would flood a search with 126 identical hits
    and make the table-grain field look column-grain."""
    _seal()
    _seed_graph(db)
    _seed_evidence(db, "business_context", "Owned by sanctions screening operations.")
    _project(db)

    assert {h.kind for h in _hits(db, "sanctions")} == {"table"}


def test_a_rebuilt_document_is_identical_to_an_insert_time_one(db) -> None:
    """The parity invariant, checked on the bytes: rebuilding a node that has not changed must
    reproduce exactly the document the INSERT wrote. If these ever diverge, a projection silently
    re-indexes rows under different weights than a fresh upload does."""
    _seed_graph(db)
    before = db.execute(
        "SELECT object_ref, search_doc::text FROM graph_node WHERE catalog_source = %s "
        "ORDER BY object_ref", (_SRC,)).fetchall()
    for ref, _doc in before:
        rebuild_search_doc(db, _SRC, ref)
    after = db.execute(
        "SELECT object_ref, search_doc::text FROM graph_node WHERE catalog_source = %s "
        "ORDER BY object_ref", (_SRC,)).fetchall()
    assert after == before


def test_rebuild_backfills_rows_written_by_an_older_document_shape(db) -> None:
    """Migration 1052 changed what a table document contains. A catalog uploaded BEFORE it keeps
    its old document until something touches the row — so the prose stays unfindable forever
    unless a backfill walks the catalog through the one expression."""
    _seal()
    _seed_graph(db)
    # A pre-1052 world: the prose is on the row (a projection wrote it) but the document was built
    # by the old expression, which had no slot for either field.
    db.execute(
        "UPDATE graph_node SET definition = %s, business_context = %s, "
        "search_doc = setweight(to_tsvector('english', table_name), 'A') "
        "WHERE catalog_source = %s AND object_ref = %s",
        ("Crosswalk of legacy party identifiers.",
         "Owned by financial crime operations for sanctions screening.",
         _SRC, _TABLE_OBJECT_REF))
    assert _hits(db, "sanctions") == []

    assert rebuild_search_docs(db, _SRC) == 3   # one table node + two column nodes

    assert [h.object_ref for h in _hits(db, "sanctions")] == [_TABLE_OBJECT_REF]


def test_rebuild_backfill_is_idempotent_and_a_noop_for_an_unknown_source(db) -> None:
    _seed_graph(db)
    assert rebuild_search_docs(db, _SRC) == 3
    first = db.execute(
        "SELECT object_ref, search_doc::text FROM graph_node WHERE catalog_source = %s "
        "ORDER BY object_ref", (_SRC,)).fetchall()
    assert rebuild_search_docs(db, _SRC) == 3
    assert db.execute(
        "SELECT object_ref, search_doc::text FROM graph_node WHERE catalog_source = %s "
        "ORDER BY object_ref", (_SRC,)).fetchall() == first
    assert rebuild_search_docs(db, "no-such-catalog") == 0


# ── the BACKFILL COMMAND: what makes both surfaces reachable on live data ────────────────────────
#
# `rebuild_search_docs` and the `data_role` projection both had ZERO production callers, so on a
# catalog uploaded before 1052 the new facet and the table-prose matching stayed empty until
# somebody happened to re-upload. These pin the command that reaches them.


def _pre_1052_catalog(db) -> None:
    """A catalog in the shape a live deployment is in after `migrate`: the graph and the field
    EVIDENCE are there (an older upload wrote them), and the 1052 projections are not — because the
    code that writes them runs only during an upload."""
    _seal()
    _seed_graph(db)
    _seed_evidence(db, "table_role", "bridge")
    _seed_evidence(db, "business_context",
                   "Owned by financial crime operations for sanctions screening.",
                   producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED)
    # Deliberately NOT projected: this is the state `resolve_and_project` has never run against.
    assert _table_row(db, "data_role", "business_context") == (None, None)


def test_the_backfill_command_fills_the_data_role_facet_and_the_prose_slots(db) -> None:
    from featuregen.overlay.upload.backfill_projections import backfill_projections

    _pre_1052_catalog(db)
    assert _hits(db, "sanctions") == [], "the prose is unmatchable before the backfill"

    reports = backfill_projections(db, sources=[_SRC])

    assert [r.catalog_source for r in reports] == [_SRC]
    assert reports[0].table_refs_projected == 1
    assert reports[0].table_refs_failed == 0
    assert reports[0].search_docs_rebuilt == 3      # one table node + two column nodes
    assert reports[0].ok
    # The FACET column the search facet reads literally, and the prose it now matches on.
    role, context = _table_row(db, "data_role", "business_context")
    assert role == DataRole.CROSSWALK.value
    assert context is not None
    assert [h.object_ref for h in _hits(db, "sanctions")] == [_TABLE_OBJECT_REF]


def test_the_backfill_command_is_idempotent_in_what_it_projects(db) -> None:
    """Idempotent in the PROJECTION, which is the property an operator running it twice needs. The
    append-only decision log does gain a RESOLVED event per resolved field per run — exactly as
    every re-upload does — so the command is safe to repeat, not free."""
    from featuregen.overlay.upload.backfill_projections import backfill_projections

    _pre_1052_catalog(db)
    backfill_projections(db, sources=[_SRC])
    first = db.execute(
        "SELECT object_ref, data_role, business_context, definition, search_doc::text "
        "FROM graph_node WHERE catalog_source = %s ORDER BY object_ref", (_SRC,)).fetchall()

    second = backfill_projections(db, sources=[_SRC])

    assert second[0].table_refs_failed == 0
    assert db.execute(
        "SELECT object_ref, data_role, business_context, definition, search_doc::text "
        "FROM graph_node WHERE catalog_source = %s ORDER BY object_ref",
        (_SRC,)).fetchall() == first


def test_the_backfill_command_covers_EVERY_catalog_when_no_source_is_named(db) -> None:
    from featuregen.overlay.upload.backfill_projections import backfill_projections

    _pre_1052_catalog(db)
    ingest_upload(db, "other", [CanonicalRow("other", "t2", "c2", "text")], actor=ACTOR, now=_NOW)

    got = {r.catalog_source for r in backfill_projections(db)}
    assert {_SRC, "other"} <= got


def test_a_MISNAMED_source_is_refused_rather_than_reported_as_a_clean_run(db) -> None:
    """"0 rows, exit 0" reads a typo as a completed backfill. An operator who names a source that
    is not there has to be told."""
    import pytest

    from featuregen.overlay.upload.backfill_projections import (
        UnknownCatalogSource,
        backfill_projections,
    )

    _pre_1052_catalog(db)
    with pytest.raises(UnknownCatalogSource, match="no catalog nodes"):
        backfill_projections(db, sources=["ftrr"])


def test_one_tables_fault_leaves_the_rest_of_the_catalog_projected(db, monkeypatch) -> None:
    """A savepoint per ref, and the failure COUNTED — a backfill that swallowed a partial failure
    and reported success would tell an operator the catalog is consistent when it is not."""
    from featuregen.overlay.upload import backfill_projections as backfill

    _pre_1052_catalog(db)

    def _boom(conn, **_kwargs):
        conn.execute("SELECT no_such_column_zz FROM graph_node")

    monkeypatch.setattr(backfill, "resolve_and_project", _boom)
    reports = backfill.backfill_projections(db, sources=[_SRC])
    assert reports[0].table_refs_failed == 1
    assert reports[0].ok is False
    # The transaction survived the fault, so the search-document rebuild still ran.
    assert reports[0].search_docs_rebuilt == 3


# ── the CLI wiring (a thin adapter over the DB-tested functions above) ───────────────────────────


def test_the_parser_accepts_the_backfill_subcommand() -> None:
    import featuregen.__main__ as m

    args = m._build_parser().parse_args(["backfill-projections", "--dsn", "postgresql:///x"])
    assert args.command == "backfill-projections"
    assert args.source is None
    args = m._build_parser().parse_args(
        ["backfill-projections", "--dsn", "postgresql:///x", "--source", "ftr"])
    assert args.source == "ftr"


def test_main_routes_the_backfill_subcommand(monkeypatch) -> None:
    import featuregen.__main__ as m

    seen: dict = {}
    monkeypatch.setattr(m, "_run_backfill_projections",
                        lambda dsn, source: seen.update(dsn=dsn, source=source) or 0)
    assert m.main(["backfill-projections", "--dsn", "postgresql:///x", "--source", "ftr"]) == 0
    assert seen == {"dsn": "postgresql:///x", "source": "ftr"}


def test_the_command_exits_NONZERO_on_a_partial_failure(monkeypatch) -> None:
    """The exit code is the only thing a deploy script reads. A partial backfill that exits 0 is a
    silent inconsistency in the surfaces Gate A is being asked to sign off."""
    import featuregen.__main__ as m
    from featuregen.overlay.upload.backfill_projections import SourceBackfillReportV1

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def commit(self): pass
        def rollback(self): pass

    monkeypatch.setattr(m.psycopg, "connect", lambda _dsn: _FakeConn())
    monkeypatch.setattr(
        "featuregen.overlay.upload.backfill_projections.backfill_projections",
        lambda _conn, sources=None: (
            SourceBackfillReportV1(catalog_source="ftr", table_refs_projected=2,
                                   table_refs_failed=1, search_docs_rebuilt=9),))
    assert m.main(["backfill-projections", "--dsn", "postgresql:///x"]) == 1

    monkeypatch.setattr(
        "featuregen.overlay.upload.backfill_projections.backfill_projections",
        lambda _conn, sources=None: (
            SourceBackfillReportV1(catalog_source="ftr", table_refs_projected=3,
                                   search_docs_rebuilt=9),))
    assert m.main(["backfill-projections", "--dsn", "postgresql:///x"]) == 0
