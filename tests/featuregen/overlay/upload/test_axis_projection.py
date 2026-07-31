"""Task 3 (ingestion-richness): the per-column display axes, projected with provenance.

`project_display_axes` fills ONLY what is NULL/blank, per catalog, from what the platform already
knows — enforcement (`visible_requires`) and the concept registry — and never touches enforcement
or a human/uploader/governed decision:

* `sensitivity_display` — a NEW display-only column. It is deliberately NOT `graph_node.sensitivity`:
  that column is the read-scope TAG (0993 CHECK = read_scope.SENSITIVITY_ROLES keys) and an INPUT to
  the GENERATED enforcement column `visible_requires` (1032), so writing display labels there would
  either violate the CHECK ('confidential') or CHANGE enforcement ('restricted' onto an untagged
  column). The display axis must be able to say 'confidential' and must change nothing.
* `entity` — identifier concepts' `entity_link`, display/planning context only; the governed
  `entity_assignment` path (entity_fact_key / entity_status) is never touched.
* `additivity` — the concept registry's default; an uploaded/human value is never overwritten.
* `party_role` — Task 1's deterministic token normalizer; NULL on ambiguity, never a guess.
"""
from __future__ import annotations

from featuregen.overlay.upload.axis_projection import (
    AxisProjectionReport,
    project_display_axes,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

_SRC = "axsrc"


def _node(db, ref: str, *, source: str = _SRC, column: str | None = None, table: str = "t",
          concept: str | None = None, sensitivity: str | None = None,
          effective_restriction: str | None = None, additivity: str | None = None,
          entity: str | None = None, entity_fact_key: str | None = None,
          entity_status: str | None = None, party_role: str | None = None,
          sensitivity_display: str | None = None, data_type: str | None = "text",
          declared_type: str | None = None) -> str:
    column = column or ref.rsplit(".", 1)[-1]
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name,"
        " data_type, is_grain, is_as_of, concept, sensitivity, effective_restriction, additivity,"
        " entity, entity_fact_key, entity_status, party_role, sensitivity_display, declared_type)"
        " VALUES (%s, %s, 'column', %s, %s, %s, false, false, %s, %s, %s, %s, %s, %s, %s, %s,"
        " %s, %s)",
        (source, ref, table, column, data_type, concept, sensitivity, effective_restriction,
         additivity, entity, entity_fact_key, entity_status, party_role, sensitivity_display,
         declared_type))
    return ref


def _col(db, ref: str, field: str, *, source: str = _SRC):
    return db.execute(
        f"SELECT {field} FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (source, ref)).fetchone()[0]


def _enforcement_snapshot(db, *, source: str = _SRC) -> list[tuple]:
    """(object_ref, raw tag, visible_requires) for every node — the two columns the projection
    must NEVER change (the generated `visible_requires` derives from them)."""
    return db.execute(
        "SELECT object_ref, sensitivity, visible_requires FROM graph_node"
        " WHERE catalog_source = %s ORDER BY object_ref", (source,)).fetchall()


# ── sensitivity display: enforcement first, then the concept default, else honest NULL ────────────


def test_sensitivity_display_projected_from_enforcement(db):
    restricted = _node(db, "public.t.a", effective_restriction="restricted")
    confidential = _node(db, "public.t.b", effective_restriction="confidential")
    pii_tagged = _node(db, "public.t.c", sensitivity="pii")
    # concept default: a pii-class concept on a column with NO enforcement ({}).
    concept_pii = _node(db, "public.t.d", concept="party_name")
    concept_proxy = _node(db, "public.t.e", concept="country_code")
    unknown = _node(db, "public.t.f", concept="monetary_stock")   # public-class -> honest NULL

    before = _enforcement_snapshot(db)
    report = project_display_axes(db, _SRC)

    assert _col(db, restricted, "sensitivity_display") == "restricted"
    assert _col(db, confidential, "sensitivity_display") == "confidential"
    # {pii} is the tag axis; its display level per the registry mapping is 'restricted'.
    assert _col(db, pii_tagged, "sensitivity_display") == "restricted"
    assert _col(db, concept_pii, "sensitivity_display") == "restricted"
    assert _col(db, concept_proxy, "sensitivity_display") == "confidential"
    assert _col(db, unknown, "sensitivity_display") is None       # unknown is honest
    assert sorted(report.sensitivity_set) == sorted(
        [restricted, confidential, pii_tagged, concept_pii, concept_proxy])
    # enforcement (visible_requires) and the raw tag are NEVER written by this projection.
    assert _enforcement_snapshot(db) == before


def test_enforcement_wins_over_the_concept_default(db):
    """A proxy-class concept under a `restricted` floor displays the ENFORCED level, not the
    (weaker) concept default — display must never understate what reads are actually gated on."""
    ref = _node(db, "public.t.geo", concept="country_code", effective_restriction="restricted")
    project_display_axes(db, _SRC)
    assert _col(db, ref, "sensitivity_display") == "restricted"


def test_an_existing_sensitivity_display_is_never_overwritten(db):
    ref = _node(db, "public.t.kept", concept="country_code", sensitivity_display="restricted")
    report = project_display_axes(db, _SRC)
    assert _col(db, ref, "sensitivity_display") == "restricted"
    assert report.sensitivity_set == ()


# ── entity: identifier concepts' entity_link, never over a governed/human/declared value ─────────


def test_entity_projected_from_concept_link_with_provenance(db):
    filled = _node(db, "public.t.cust_no", concept="customer_id")
    declared = _node(db, "public.t.acct", concept="account_id", entity="account")
    governed = _node(db, "public.t.gov", concept="customer_id",
                     entity_fact_key="fct_x", entity_status="VERIFIED", entity="customer")
    fact_no_entity = _node(db, "public.t.gov2", concept="customer_id", entity_fact_key="fct_y")
    non_identifier = _node(db, "public.t.amt", concept="monetary_flow")

    report = project_display_axes(db, _SRC)

    assert _col(db, filled, "entity") == "customer"
    assert report.entity_set == (filled,)                      # provenance: exactly what was set
    assert _col(db, declared, "entity") == "account"           # declared value untouched
    assert _col(db, governed, "entity") == "customer"          # governed value untouched
    # a column under a governed entity fact is SKIPPED even when its display entity is blank —
    # the governed path owns it; the abstention is recorded, never silent.
    assert _col(db, fact_no_entity, "entity") is None
    assert ("public.t.gov2", "entity", "entity_fact_present") in [
        (s.object_ref, s.axis, s.reason) for s in report.skipped]
    assert _col(db, non_identifier, "entity") is None          # no entity_link -> nothing to say


def test_entity_projection_rebuilds_the_search_doc_to_the_fresh_insert_doc(db):
    """Invariant #20: `entity` feeds `_SEARCH_DOC`'s domain slot, so a projected node's rebuilt doc
    must equal the doc of a node INSERTED with that entity — one expression, two paths."""
    row = CanonicalRow("axs", "t", "cust_id", "text")
    build_graph(db, "axs", [row], concepts={content_hash(row): "customer_id"})
    control = CanonicalRow("axc", "t", "cust_id", "text", entity="customer")
    build_graph(db, "axc", [control], concepts={content_hash(control): "customer_id"})

    report = project_display_axes(db, "axs")

    assert "public.t.cust_id" in report.entity_set
    projected = db.execute(
        "SELECT search_doc::text FROM graph_node WHERE catalog_source = 'axs'"
        " AND object_ref = 'public.t.cust_id'").fetchone()[0]
    fresh = db.execute(
        "SELECT search_doc::text FROM graph_node WHERE catalog_source = 'axc'"
        " AND object_ref = 'public.t.cust_id'").fetchone()[0]
    assert projected == fresh


# ── additivity: the concept registry default, never over an uploaded/human value ─────────────────


def test_additivity_defaults_from_concept(db):
    flow = _node(db, "public.t.flow_amt", concept="monetary_flow")           # additive
    stock = _node(db, "public.t.bal", concept="monetary_stock")              # semi_additive
    declared = _node(db, "public.t.decl", concept="monetary_flow", additivity="non_additive")
    na = _node(db, "public.t.code", concept="country_code")                  # registry 'n/a'

    report = project_display_axes(db, _SRC)

    assert _col(db, flow, "additivity") == "additive"
    assert _col(db, stock, "additivity") == "semi_additive"
    assert _col(db, declared, "additivity") == "non_additive"   # uploaded value never overwritten
    assert _col(db, na, "additivity") is None                   # 'n/a' is not a default
    assert sorted(report.additivity_set) == sorted([flow, stock])


# ── party_role: deterministic, advisory, honest abstention ───────────────────────────────────────


def test_party_role_projected_from_column_name_tokens(db):
    cases = {
        "sender_bic": "sender",
        "receiver_bic": "receiver",
        "third_reimb_inst_code": "reimbursement",
        "counter_party_cif_id": "counterparty",
        "cust_swift_cd": "subject",
    }
    refs = {c: _node(db, f"public.t.{c}") for c in cases}
    ambiguous = _node(db, "public.t.tran_amt")
    kept = _node(db, "public.t.sender_ref", party_role="counterparty")

    report = project_display_axes(db, _SRC)

    for c, role in cases.items():
        assert _col(db, refs[c], "party_role") == role, c
    assert _col(db, ambiguous, "party_role") is None            # ambiguity abstains, never guesses
    assert _col(db, kept, "party_role") == "counterparty"       # existing value never overwritten
    assert sorted(report.party_role_set) == sorted(refs.values())


def test_party_role_is_never_consumed_by_join_candidacy_or_execution(db):
    """The advisory contract (plan 3b): `party_role` explains and names; it must never gate. No
    join-candidacy / planner / execution module may even MENTION it — an import-gate style source
    scan, so a future predicate cannot slip in silently."""
    import pathlib

    import featuregen

    src_root = pathlib.Path(featuregen.__file__).parent
    allowed = {
        "overlay/upload/party_vocab.py",       # the vocabulary itself
        "overlay/upload/axis_projection.py",   # the display projection (this task)
        "overlay/upload/ingest.py",            # wiring + the summary dossier payload
        "overlay/upload/enrich.py",            # the summary payload assembler
        "overlay/upload/enrich_llm.py",        # egress allowlist entry (structural token)
        "overlay/upload/asset_detail.py",      # the column dossier (Task 3C read surface)
        "overlay/upload/column_view.py",       # pure dossier assembler
    }
    offenders = sorted(
        str(p.relative_to(src_root))
        for p in src_root.rglob("*.py")
        if "party_role" in p.read_text(encoding="utf-8")
        and str(p.relative_to(src_root)) not in allowed)
    assert offenders == [], f"party_role leaked into non-display modules: {offenders}"


# ── idempotent, catalog-scoped ───────────────────────────────────────────────────────────────────


def test_projection_is_idempotent_and_scoped(db):
    mine = _node(db, "public.t.cust_no", concept="customer_id",
                 effective_restriction="restricted")
    other = _node(db, "public.t.cust_no", source="othersrc", concept="customer_id",
                  effective_restriction="restricted")

    first = project_display_axes(db, _SRC)
    assert first.entity_set == (mine,) and first.sensitivity_set == (mine,)

    rows_before = db.execute(
        "SELECT object_ref, sensitivity_display, entity, additivity, party_role FROM graph_node"
        " ORDER BY catalog_source, object_ref").fetchall()
    second = project_display_axes(db, _SRC)
    assert (second.sensitivity_set, second.entity_set, second.additivity_set,
            second.party_role_set) == ((), (), (), ())          # nothing left to fill
    assert db.execute(
        "SELECT object_ref, sensitivity_display, entity, additivity, party_role FROM graph_node"
        " ORDER BY catalog_source, object_ref").fetchall() == rows_before

    # scoping: 'othersrc' was NEVER touched.
    assert _col(db, other, "entity", source="othersrc") is None
    assert _col(db, other, "sensitivity_display", source="othersrc") is None


# ── declared-type gap honesty (Step 5) ───────────────────────────────────────────────────────────


def test_type_unknown_columns_are_listed_in_the_report(db):
    gap = _node(db, "public.t.cust_buy_rate", data_type="unknown")
    _node(db, "public.t.declared", data_type="unknown", declared_type="numeric(18,6)")
    _node(db, "public.t.attested", data_type="numeric")

    report = project_display_axes(db, _SRC)

    assert report.type_unknown == (gap,)


def test_ingest_tail_runs_the_projection_and_records_the_stage(db):
    """Step 4: `ingest_upload` runs the projection at the TAIL (after the governed re-projections,
    so governed values are already back on the fresh graph and the fill-only-NULL guards see them)
    and records stage `axis_projection` with the report counts."""
    from datetime import UTC, datetime, timedelta

    from featuregen.contracts.envelopes import IdentityEnvelope
    from featuregen.overlay.config import OverlayConfig, register_overlay_config
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.stage_report import StageRecorder

    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))
    actor = IdentityEnvelope(subject="u", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))
    rec = StageRecorder()
    rows = [CanonicalRow("axing", "txn", "sender_bic", "text"),
            CanonicalRow("axing", "txn", "amt", "numeric", additivity="non_additive")]
    res = ingest_upload(db, "axing", rows, actor=actor,
                        now=datetime(2026, 7, 31, tzinfo=UTC), stage_recorder=rec)
    assert res.status == "ingested"
    stage = next(r for r in rec.reports if r.stage == "axis_projection")
    assert stage.state == "succeeded"
    assert stage.detail["party_role_set"] == 1                  # sender_bic -> sender
    assert stage.started_at is not None
    assert _col(db, "public.txn.sender_bic", "party_role", source="axing") == "sender"
    assert _col(db, "public.txn.amt", "additivity", source="axing") == "non_additive"
    # the stage sits at the ingest TAIL: after every re-projection, before quarantine.
    order = [r.stage for r in rec.reports]
    assert order.index("axis_projection") > order.index("semantic_binding_projection")
    assert order.index("axis_projection") > order.index("join_drift")
    assert order.index("axis_projection") < order.index("quarantine")


def test_the_search_sensitivity_facet_is_untouched_by_the_projection(db):
    """Verify-only (plan Task 3 files note): the search `sensitivity` facet reads the RAW tag
    column — a HARD read-scope input — which this projection never writes, so facet behavior is
    byte-identical across a run. (The display axis lives in `sensitivity_display`; surfacing it
    on the search/dossier read side is Task 3C's work, gated on the frontend facet contract.)"""
    from featuregen.overlay.upload.search import _COLUMN_FACETS

    assert _COLUMN_FACETS["sensitivity"] == "sensitivity"       # the facet's source column
    tagged = _node(db, "public.t.tagged", sensitivity="pii")
    floored = _node(db, "public.t.floored", effective_restriction="confidential")
    project_display_axes(db, _SRC)
    assert _col(db, tagged, "sensitivity") == "pii"             # raw tag byte-identical
    assert _col(db, floored, "sensitivity") is None


def test_report_shape_and_stage_detail_are_bounded_counts(db):
    # a token-free column name: `cust_*` would legitimately earn party_role='subject'.
    _node(db, "public.t.client_no", concept="customer_id", effective_restriction="restricted")
    report = project_display_axes(db, _SRC)
    assert isinstance(report, AxisProjectionReport)
    detail = report.stage_detail()
    assert detail["sensitivity_set"] == 1
    assert detail["entity_set"] == 1
    assert detail["additivity_set"] == 0
    assert detail["party_role_set"] == 0
    assert "skipped" not in detail            # zero skips -> no noise key
    assert "type_unknown" not in detail
