"""Task 4 (ingestion-richness remediation) — un-stall the semantic-binding pipeline.

The live defect (kind cluster, 2026-07-31): 126 rows in ``semantic_binding_candidate`` and ZERO in
``semantic_binding_candidate_proposal`` / ``semantic_binding_edge`` with BOTH stage flags on. Root
cause: ``_currency_candidates`` marked a pairing STRONG only when the table had EXACTLY ONE
currency column, so on any real table with several currency-code columns (FTR carries
``tran_crncy`` AND ``actual_tran_crncy``) every measure x currency pairing was WEAK, the proposal
stage received zero proposable candidates, emitted nothing, and recorded a vacuous
``succeeded {proposed: 0, abstained: 0}`` — the silent-zero class the plan bans.

Locked here:

1. LIVE SHAPE — a monetary column with a same-table currency column yields >= 1 currency-binding
   proposal, via NAME-AFFINITY disambiguation (``tran_amt -> tran_crncy``,
   ``actual_tran_amt -> actual_tran_crncy``) and FIXED-CURRENCY detection
   (``counter_party_amt_aed`` -> literal ``AED``), and the stage records
   ``succeeded {proposed: n>0}``.
2. GUARD — candidates > 0 with proposals == 0 and NO recorded per-candidate denial reason forces
   stage state ``failed {reason: unexplained_zero}`` — a silent zero is a stage failure by
   contract.
3. EXPLAINED ZERO — a genuinely ambiguous table (no affinity signal) still proposes nothing, but
   the stage records the per-candidate denial reasons and stays ``succeeded`` (an EXPLAINED zero
   is not a failure; ambiguity is preserved for a reviewer, never guessed).
4. E2E — candidate -> proposal -> confirm via the EXISTING ``semantic_binding_governance``
   surface -> ``semantic_binding_edge`` row -> ``graph_node.currency`` projected; BOTH the AED
   fixed-currency case (literal, no target column) and the column-ref case covered, plus replay
   parity (the registered projection rebuild reproduces the same operational state).
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts import Command
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.facts import CURRENCY_BINDING
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_view import build_table_views
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.semantic_binding_governance import (
    list_semantic_binding_proposals,
    load_semantic_binding_confirmation_context,
)
from featuregen.overlay.upload.semantic_bindings.projection import (
    SemanticBindingProjection,
    project_verified_semantic_binding,
    verified_currency_binding,
)
from featuregen.overlay.upload.semantic_bindings.shortlist import shortlist
from featuregen.overlay.upload.semantic_bindings.types import STRONG, WEAK
from featuregen.overlay.upload.semantic_bindings.validate import validate_candidates
from featuregen.overlay.upload.stage_report import StageRecorder

_NOW = datetime(2026, 7, 31, tzinfo=UTC)
_CANDS = "OVERLAY_SEMANTIC_BINDING_CANDIDATES"
_PROPS = "OVERLAY_SEMANTIC_BINDING_PROPOSALS"
_SOURCE = "ftr"
_TABLE = "ftr_tran"


def _uploader() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _admin() -> IdentityEnvelope:
    return IdentityEnvelope(subject="user:admin", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("platform-admin",))


def _live_shape_rows() -> list[CanonicalRow]:
    """The verified live FTR shape that starved the stage: TWO currency columns (``tran_crncy``,
    ``actual_tran_crncy``) — so the old exactly-one-currency-in-the-table rule made every pairing
    WEAK — plus a fixed-currency measure (``counter_party_amt_aed``)."""
    return [
        CanonicalRow(_SOURCE, _TABLE, "tran_id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, _TABLE, "tran_amt", "numeric"),
        CanonicalRow(_SOURCE, _TABLE, "tran_crncy", "text"),
        CanonicalRow(_SOURCE, _TABLE, "actual_tran_amt", "numeric"),
        CanonicalRow(_SOURCE, _TABLE, "actual_tran_crncy", "text"),
        CanonicalRow(_SOURCE, _TABLE, "counter_party_amt_aed", "numeric"),
    ]


def _stage(rec: StageRecorder, name: str):
    reports = [r for r in rec.reports if r.stage == name]
    assert reports, f"stage {name!r} was never recorded"
    return reports[-1]


def _proposal_links(conn, source: str = _SOURCE) -> int:
    return conn.execute(
        "SELECT count(*) FROM semantic_binding_candidate_proposal p "
        "JOIN semantic_binding_candidate c ON c.candidate_id = p.candidate_id "
        "WHERE c.catalog_source = %s", (source,)).fetchone()[0]


def _measure_ref(column: str) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=_SOURCE, object_kind="column", schema="public",
                            table=_TABLE, column=column)


# ══════════════════════════════ 1. the live shape proposes ══════════════════════════════════════

def test_live_shape_yields_proposals_and_stage_succeeded_nonzero(db, monkeypatch):
    """THE Task-4 boundary test: a monetary column with a same-table currency column yields >= 1
    currency-binding proposal and the stage records ``succeeded`` with ``proposed > 0`` — on the
    exact multi-currency-column shape that produced 126 candidates -> 0 proposals live."""
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")
    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, _live_shape_rows(), actor=_uploader(), now=_NOW,
                        stage_recorder=rec)
    assert res.status == "ingested"
    # 5 candidates: 2 affinity-strong pairs + 2 non-preferred weak pairs + 1 fixed-currency strong.
    assert res.semantic_binding_candidates == 5
    # The three STRONG candidates become DRAFT proposals (the un-stall).
    assert res.semantic_binding_proposed == 3
    assert _proposal_links(db) == 3
    report = _stage(rec, "semantic_binding_proposals")
    assert report.state == "succeeded"
    assert report.detail is not None and report.detail["proposed"] == 3
    # The two weak pairings are recorded as per-candidate denial reasons — never silently dropped.
    assert report.detail["denials"], "non-proposed candidates must carry a recorded reason"

    # Each proposal is a DRAFT governed fact — NEVER auto-verified.
    for column in ("tran_amt", "actual_tran_amt", "counter_party_amt_aed"):
        stream = load_fact(db, fact_key(_measure_ref(column), CURRENCY_BINDING))
        assert stream, f"no DRAFT proposal for {column}"
        assert fold_overlay_state(stream).status == "DRAFT"


def test_affinity_pairs_each_measure_with_its_named_currency(db, monkeypatch):
    """The disambiguation is name-affinity, not guesswork: ``actual_tran_amt`` binds
    ``actual_tran_crncy`` (never ``tran_crncy``) and ``tran_amt`` binds ``tran_crncy``."""
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")
    res = ingest_upload(db, _SOURCE, _live_shape_rows(), actor=_uploader(), now=_NOW)
    assert res.semantic_binding_proposed == 3

    stream = load_fact(db, fact_key(_measure_ref("actual_tran_amt"), CURRENCY_BINDING))
    value = stream[0].payload["proposed_value"]
    assert value["currency_column"]["column"] == "actual_tran_crncy"

    stream = load_fact(db, fact_key(_measure_ref("tran_amt"), CURRENCY_BINDING))
    value = stream[0].payload["proposed_value"]
    assert value["currency_column"]["column"] == "tran_crncy"

    # The fixed-currency measure proposes the LITERAL code, not a column pairing.
    stream = load_fact(db, fact_key(_measure_ref("counter_party_amt_aed"), CURRENCY_BINDING))
    assert stream[0].payload["proposed_value"] == {"currency_code": "AED"}


# ══════════════════════════════ 2. the unexplained-zero guard ═══════════════════════════════════

def test_unexplained_zero_forces_stage_failed(db, monkeypatch):
    """The class-level guard: candidates > 0 with proposals == 0 and NO recorded per-candidate
    denial reason is a CONTRACT VIOLATION — the stage records ``failed {unexplained_zero}``, never
    a vacuous ``succeeded {0}``."""
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")
    import featuregen.overlay.upload.ingest as ingest_mod

    def _silent_zero(conn, proposable, *, actor):
        return 0, 0, {}                       # zero proposals, zero reasons — the banned shape

    monkeypatch.setattr(ingest_mod, "_run_semantic_binding_proposal_stage", _silent_zero)
    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, _live_shape_rows(), actor=_uploader(), now=_NOW,
                        stage_recorder=rec)
    assert res.status == "ingested"                       # never fails the upload itself
    assert res.semantic_binding_candidates > 0
    report = _stage(rec, "semantic_binding_proposals")
    assert report.state == "failed"
    assert report.reason_code == "unexplained_zero"
    assert res.semantic_binding_failed >= 1


def test_all_weak_zero_is_explained_not_failed(db, monkeypatch):
    """A genuinely ambiguous table (two currency columns, NO name affinity) proposes nothing — but
    every candidate carries a recorded denial reason, so the zero is EXPLAINED: the stage stays
    ``succeeded`` with the denial histogram in its detail (ambiguity preserved for a reviewer)."""
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")
    rows = [
        CanonicalRow(_SOURCE, "opaque", "id", "integer", is_grain=True),
        CanonicalRow(_SOURCE, "opaque", "amt", "numeric"),
        CanonicalRow(_SOURCE, "opaque", "ccy", "text"),
        CanonicalRow(_SOURCE, "opaque", "curr", "text"),
    ]
    rec = StageRecorder()
    res = ingest_upload(db, _SOURCE, rows, actor=_uploader(), now=_NOW, stage_recorder=rec)
    assert res.status == "ingested"
    assert res.semantic_binding_candidates == 2           # amt->ccy, amt->curr — both weak
    assert res.semantic_binding_proposed == 0
    report = _stage(rec, "semantic_binding_proposals")
    assert report.state == "succeeded"                    # an EXPLAINED zero is not a failure
    assert report.detail is not None and report.detail["proposed"] == 0
    assert sum(report.detail["denials"].values()) == 2    # every candidate has a recorded reason


# ══════════════════════════════ 3. the pure live-mechanism repro ════════════════════════════════

def _concept_views():
    """The exact LIVE recognition path: the currency columns carry ``currency``-group CONCEPTS
    (Pass A products) — their names match no structural token — mirroring the deployed FTR shape."""
    rows = _live_shape_rows()
    concepts = {
        content_hash(rows[1]): "monetary_flow",
        content_hash(rows[2]): "currency_code",
        content_hash(rows[3]): "monetary_flow",
        content_hash(rows[4]): "local_currency",
        content_hash(rows[5]): "monetary_flow",
    }
    return build_table_views(rows, glossary=None, bindings=None, concepts=concepts,
                             definitions=None, domains=None)


def test_concept_recognized_multi_currency_pairs_by_affinity():
    """The pure D2 regression lock for the live mechanism: concept-recognized currency columns +
    several of them in one table must still yield STRONG affinity pairs and the fixed-currency
    literal — under the OLD rule this exact shape produced zero STRONG candidates."""
    (view,) = _concept_views().values()
    cands = validate_candidates(shortlist(view, None, None), view)
    by_pair = {(c.subject.column, c.target.column if c.target else c.currency_code): c
               for c in cands}
    assert by_pair[("tran_amt", "tran_crncy")].disposition == STRONG
    assert by_pair[("actual_tran_amt", "actual_tran_crncy")].disposition == STRONG
    assert by_pair[("counter_party_amt_aed", "AED")].disposition == STRONG
    # The non-preferred pairings survive as WEAK review artefacts — never silently dropped.
    assert by_pair[("tran_amt", "actual_tran_crncy")].disposition == WEAK
    assert by_pair[("actual_tran_amt", "tran_crncy")].disposition == WEAK
    # The fixed-currency measure is NOT cross-paired with unrelated currency columns (decoy noise).
    assert ("counter_party_amt_aed", "tran_crncy") not in by_pair
    assert ("counter_party_amt_aed", "actual_tran_crncy") not in by_pair


def test_validate_downgrades_forged_strong_on_non_preferred_target():
    """validate() re-derives the SAME affinity preference: a candidate claiming STRONG for the
    non-preferred target is downgraded (ambiguous_target) — shortlist and validate cannot drift."""
    from dataclasses import replace

    (view,) = _concept_views().values()
    cands = shortlist(view, None, None)
    weak = next(c for c in cands
                if c.subject.column == "actual_tran_amt"
                and c.target is not None and c.target.column == "tran_crncy")
    forged = replace(weak, disposition=STRONG)
    (checked,) = validate_candidates([forged], view)
    assert checked.disposition == "rejected"
    assert "ambiguous_target" in checked.reason_codes


# ══════════════════════════════ 4. end-to-end: confirm -> operational ═══════════════════════════

def test_e2e_confirm_projects_edge_and_graph_currency(db, monkeypatch):
    """candidate -> proposal -> confirm via the EXISTING semantic_binding_governance surface ->
    ``semantic_binding_edge`` row -> ``graph_node.currency`` projected. BOTH cases: the AED
    fixed-currency literal and the actual_tran_amt -> actual_tran_crncy column ref."""
    monkeypatch.setenv(_CANDS, "1")
    monkeypatch.setenv(_PROPS, "1")
    res = ingest_upload(db, _SOURCE, _live_shape_rows(), actor=_uploader(), now=_NOW)
    assert res.semantic_binding_proposed == 3

    proposals = list_semantic_binding_proposals(db, _SOURCE)
    by_subject = {p["subject"]["column"]: p for p in proposals}
    aed = by_subject["counter_party_amt_aed"]
    colref = by_subject["actual_tran_amt"]
    assert aed["status"] == "PROPOSED" and aed["value"] == {"currency_code": "AED"}
    assert colref["value"]["currency_column"]["column"] == "actual_tran_crncy"

    for view in (aed, colref):
        ctx = load_semantic_binding_confirmation_context(db, view["fact_key"])
        result = confirm_fact(db, Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
             "target_event_id": ctx["target_event_id"]},
            _admin(), f"confirm:{view['fact_key']}"))
        assert result.accepted, result.denied_reason
        assert project_verified_semantic_binding(
            db, _SOURCE, ctx["ref"], ctx["fact_type"], now=_NOW) == "projected"

    # Column-ref case: a VERIFIED measure -> currency-column edge.
    colref_key = fact_key(_measure_ref("actual_tran_amt"), CURRENCY_BINDING)
    edge = verified_currency_binding(db, colref_key)
    assert edge is not None
    assert edge["from_ref"] == f"public.{_TABLE}.actual_tran_amt"
    assert edge["to_ref"] == f"public.{_TABLE}.actual_tran_crncy"
    assert edge["currency_code"] is None

    # Fixed-currency case: a VERIFIED literal edge (no target column) ...
    aed_key = fact_key(_measure_ref("counter_party_amt_aed"), CURRENCY_BINDING)
    edge = verified_currency_binding(db, aed_key)
    assert edge is not None
    assert edge["to_ref"] is None and edge["currency_code"] == "AED"
    # ... AND graph_node.currency projected onto the measure column, with provenance.
    row = db.execute(
        "SELECT currency, currency_status, currency_fact_key FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s",
        (_SOURCE, f"public.{_TABLE}.counter_party_amt_aed")).fetchone()
    assert row == ("AED", "VERIFIED", aed_key)

    # Replay parity: a from-zero rebuild of the registered projection reproduces the SAME state.
    SemanticBindingProjection().rebuild(db)
    assert verified_currency_binding(db, aed_key)["currency_code"] == "AED"
    assert verified_currency_binding(db, colref_key)["to_ref"] == \
        f"public.{_TABLE}.actual_tran_crncy"
    row = db.execute(
        "SELECT currency, currency_status FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s",
        (_SOURCE, f"public.{_TABLE}.counter_party_amt_aed")).fetchone()
    assert row == ("AED", "VERIFIED")
