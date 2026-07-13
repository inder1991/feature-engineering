"""Task 11 — the AUTHORITY PROOF (Phase 3A integration, no new production code).

THE ONE INVARIANT: ``approved_join VERIFIED -> operational graph_edge -> planner traversal`` is the
ONLY way a Pass C join becomes operationally traversable. Everything else — a DRAFT proposal, a
rejected/expired fact, a shared entity tag — fails CLOSED. Every scenario here drives the
PRODUCTION paths end-to-end (``ingest_upload`` with ``OVERLAY_PASS_C=1``, the dual-admin confirm
commands, ``fire_due_overlay_expiries``, ``project_confirmed_joins``); nothing hand-inserts facts.

The nine acceptance criteria map one-to-one onto the ``test_c<N>_...`` tests below, plus a light
flag-off byte-for-byte check (the deep version lives in test_passc_ingest.py).

CRITERION-7 CALLER ANALYSIS (the governed-bypass crux — see test_c7 for the runtime half):

* ``find_join_path`` / ``route_strategies`` / ``_cross_adjacency``'s JOIN layer all filter on
  ``authority='operational' AND (approved_join_fact_key IS NULL OR approved_join_status =
  'VERIFIED')`` — the operational planner is governed-filtered everywhere it reads join edges.
* ``cross_join_via_entity`` (entity.py:79) has ZERO production callers (tests only) — a dormant
  ADVISORY primitive whose docstring requires callers to surface it for human confirmation.
* ``find_cross_catalog_path`` (entity.py:249) is a HYBRID: its join-edge adjacency is
  governed-filtered, but its entity-BRIDGE adjacency (graph_node.entity tags) is NOT — by
  documented design ("Entity-bridge hops are declared/entity-resolved — callers surface them for
  human confirmation"). Its ONLY production caller is ``contract/author.py::_join_path``, and only
  its CROSS-catalog branch; the steps it records are explicitly labelled ``kind="entity"`` on a
  ContractDraft that becomes governing ONLY through ``confirm_contract`` — the human gate — and
  ``contract.join_path`` is descriptive JSONB nothing ever traverses/executes.
* CONCLUSION: no operational feature-construction path consumes the advisory entity bridge without
  a human gate; test_c7 asserts every operational entry point is closed and PINS the advisory
  behaviour (an entity-only path IS returned — asserting None would misdocument the design).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from tests.featuregen.overlay.upload.passc.conftest import (
    _confirm_join,
    _drain,
    _expire_join,
    _reject_join,
)

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.task_read import get_task_proposal
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.entity import cross_join_via_entity, find_cross_catalog_path
from featuregen.overlay.upload.feature_assist import route_strategies
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.ingest import _pass_c_columns, ingest_upload
from featuregen.overlay.upload.join_path import find_join_path
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.passc.namespace import classify_namespace
from featuregen.overlay.upload.passc.projection import (
    list_approved_join_refs,
    project_confirmed_joins,
)
from featuregen.overlay.upload.passc.types import NamespaceCompatibility
from featuregen.overlay.upload.readiness import (
    RelationshipStatus,
    compute_relationship_readiness,
)

_NOW = datetime(2026, 7, 13, tzinfo=UTC)
_CIF_TERM = "Customer Information File Identifier"
# The spec-§7 mixed BIAN leaf: it hosts BOTH account- and customer-namespace identifiers, so a
# shared leaf proves nothing (types.py pins it in DEFAULT_CONFIG.mixed_bian_leaves).
_MIXED_BIAN = "Party Reference/Customer and Counterparty Identification"


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _rec(source: str, table: str, column: str, term: str, *, synonyms=(),
         bian: str = "Customer Management/Customer Reference") -> GlossaryRecord:
    return GlossaryRecord(
        logical_ref=normalize_ref(source, "public", table, column),
        term_name=term, definition=f"The {term}.", domain="Customer",
        synonyms=tuple(synonyms), bian_path=bian, fibo_path="")


def _cif_rows(source: str) -> list[CanonicalRow]:
    """Criterion-1 shape: two tables share the cif_id concept; customer.cif_id is a confirmed
    grain -> a strong, N:1 grain-inferred candidate (transactions.cif_id -> customer.cif_id)."""
    return [CanonicalRow(source, "customer", "cif_id", "integer", is_grain=True),
            CanonicalRow(source, "transactions", "cif_id", "integer")]


def _cif_glossary(source: str) -> GlossaryUpload:
    return GlossaryUpload(rows=[], records=[
        _rec(source, "customer", "cif_id", _CIF_TERM, synonyms=("CIF",)),
        _rec(source, "transactions", "cif_id", _CIF_TERM, synonyms=("CIF",))])


def _ingest(conn, source: str, rows, glossary=None):
    res = ingest_upload(conn, source, rows, actor=_actor(), now=_NOW, glossary=glossary)
    assert res.status == "ingested", res
    _drain(conn)   # the Pass C propose runs late in ingest — catch the read models up to head
    return res


def _sole_join_ref(conn, source: str):
    """Exactly ONE approved_join ref proposed for `source`, rebuilt from the production read model."""
    refs = list_approved_join_refs(conn, source)
    assert len(refs) == 1, f"expected exactly one approved_join for {source!r}, got {refs}"
    return refs[0]


def _ledger(conn, source: str):
    return conn.execute(
        "SELECT from_ref, to_ref, bucket, namespace_compatibility, fact_key, evidence_json"
        " FROM pass_c_candidate_evidence WHERE catalog_source=%s ORDER BY from_ref, to_ref",
        (source,)).fetchall()


def _open_task_ids(conn, key: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT task_id FROM human_tasks WHERE fact_key=%s AND status='open' ORDER BY task_id",
        (key,)).fetchall()]


def _join_edges(conn, source: str):
    return conn.execute(
        "SELECT from_ref, to_ref, authority, approved_join_fact_key, approved_join_status"
        " FROM graph_edge WHERE catalog_source=%s AND kind='joins' ORDER BY from_ref",
        (source,)).fetchall()


# ── Criterion 1: strong cif candidate -> a governed approved_join DRAFT ──────────────────────────


def test_c1_strong_cif_candidate_is_proposed_as_draft(passc_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    _ingest(passc_conn, "bank", _cif_rows("bank"), _cif_glossary("bank"))

    rows = _ledger(passc_conn, "bank")
    assert len(rows) == 1, rows
    from_ref, to_ref, bucket, namespace, fk, evidence = rows[0]
    assert {from_ref, to_ref} == {"public.customer.cif_id", "public.transactions.cif_id"}
    assert bucket == "strong" and namespace == "compatible"
    assert fk is not None                                   # proposed -> fact_key stamped back
    assert evidence["proposed_cardinality"] == "N:1"
    assert evidence["cardinality_status"] == "inferred_from_confirmed_grain"

    events = load_fact(passc_conn, fk)
    assert [e.type for e in events].count("OVERLAY_FACT_PROPOSED") == 1
    assert fold_overlay_state(events).status == "DRAFT"     # never VERIFIED without the humans
    assert len(_open_task_ids(passc_conn, fk)) == 2         # the dual side-labelled gate tasks


# ── Criterion 2: FORACID <-> CIF_ID under the mixed BIAN leaf is NEVER proposed ──────────────────


def test_c2_foracid_cif_under_mixed_bian_leaf_is_never_proposed(passc_conn, monkeypatch):
    """FORACID (term "Customer Account Number") and CIF_ID (term "Customer Information File
    Identifier") normalize to DIFFERENT identifier concepts, and their shared BIAN leaf is the
    configured MIXED one — so the pair classifies AMBIGUOUS and never reaches the ledger, let
    alone a proposal. A cif<->cif control pair in the SAME upload DOES propose, proving Pass C
    ran and excluded the FORACID pair specifically (the negative is not vacuous)."""
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    rows = [CanonicalRow("cbs", "accounts", "foracid", "text"),
            CanonicalRow("cbs", "customers", "cif_id", "integer", is_grain=True),
            CanonicalRow("cbs", "transactions", "cif_id", "integer")]
    glossary = GlossaryUpload(rows=[], records=[
        _rec("cbs", "accounts", "foracid", "Customer Account Number",
             synonyms=("FORACID",), bian=_MIXED_BIAN),
        _rec("cbs", "customers", "cif_id", _CIF_TERM, synonyms=("CIF",), bian=_MIXED_BIAN),
        _rec("cbs", "transactions", "cif_id", _CIF_TERM, synonyms=("CIF",), bian=_MIXED_BIAN)])
    _ingest(passc_conn, "cbs", rows, glossary)

    # Control proposed; NO ledger row / fact / task involves foracid in any pairing.
    led = _ledger(passc_conn, "cbs")
    assert len(led) == 1 and led[0][2] == "strong", led
    assert "foracid" not in led[0][0] + led[0][1]
    ref = _sole_join_ref(passc_conn, "cbs")
    assert "foracid" not in {ref.from_ref.column, ref.to_ref.column}

    # WHY (through the production ColMeta assembly): AMBIGUOUS via the mixed leaf — and had the
    # sides carried different confirmed entity tags, INCOMPATIBLE via different_column_entity.
    cols = _pass_c_columns(passc_conn, "cbs", rows, concepts=None, glossary=glossary)
    by_ref = {c.object_ref: c for c in cols}
    foracid, cif = by_ref["public.accounts.foracid"], by_ref["public.customers.cif_id"]
    verdict, reasons = classify_namespace(foracid, cif)
    assert verdict is NamespaceCompatibility.AMBIGUOUS and "mixed_bian_leaf" in reasons
    verdict2, reasons2 = classify_namespace(replace(foracid, column_entity="Account"),
                                            replace(cif, column_entity="Customer"))
    assert verdict2 is NamespaceCompatibility.INCOMPATIBLE
    assert "different_column_entity" in reasons2


# ── Criterion 3: missing-grain / both-grain -> ledger diagnostic only, NO proposal ───────────────


def test_c3_missing_or_both_grain_is_diagnostic_only(passc_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")

    # Neither side a grain -> MANY_TO_MANY_RISK, forced weak, no cardinality, no proposal.
    nog = [CanonicalRow("nograin", "transactions", "cif_id", "integer"),
           CanonicalRow("nograin", "cases", "cif_id", "integer")]
    _ingest(passc_conn, "nograin", nog, GlossaryUpload(rows=[], records=[
        _rec("nograin", "transactions", "cif_id", _CIF_TERM),
        _rec("nograin", "cases", "cif_id", _CIF_TERM)]))
    led = _ledger(passc_conn, "nograin")
    assert len(led) == 1, led
    assert led[0][2] == "weak" and led[0][4] is None        # bucket weak, fact_key NULL
    assert led[0][5]["cardinality_status"] == "many_to_many_risk"
    assert list_approved_join_refs(passc_conn, "nograin") == []

    # BOTH sides grains -> AMBIGUOUS_BOTH_GRAINS (a would-be 1:1 is never auto-proposed).
    both = [CanonicalRow("bothgrain", "customer", "cif_id", "integer", is_grain=True),
            CanonicalRow("bothgrain", "custmast", "cif_id", "integer", is_grain=True)]
    _ingest(passc_conn, "bothgrain", both, GlossaryUpload(rows=[], records=[
        _rec("bothgrain", "customer", "cif_id", _CIF_TERM),
        _rec("bothgrain", "custmast", "cif_id", _CIF_TERM)]))
    led = _ledger(passc_conn, "bothgrain")
    assert len(led) == 1, led
    assert led[0][2] == "weak" and led[0][4] is None
    assert led[0][5]["cardinality_status"] == "ambiguous_both_grains"
    assert list_approved_join_refs(passc_conn, "bothgrain") == []

    # Nothing opened a gate task in either source.
    assert passc_conn.execute("SELECT count(*) FROM human_tasks").fetchone()[0] == 0


# ── Criterion 4: a DRAFT proposal is NOT traversable (fail-closed pre-confirm) ───────────────────


def test_c4_proposed_draft_is_not_traversable(passc_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    _ingest(passc_conn, "bank", _cif_rows("bank"), _cif_glossary("bank"))

    ref = _sole_join_ref(passc_conn, "bank")
    assert fold_overlay_state(
        load_fact(passc_conn, fact_key(ref, "approved_join"))).status == "DRAFT"
    # Non-vacuous: both tables exist in the graph; there is simply NO operational join between them.
    n = passc_conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source='bank'"
                           " AND kind='column'").fetchone()[0]
    assert n == 2
    assert _join_edges(passc_conn, "bank") == []            # nothing declared, nothing projected
    assert find_join_path(passc_conn, "bank", "transactions", "customer") is None


# ── Criterion 5: two-admin VERIFIED -> projected -> traversable + readiness=confirmed ────────────


def test_c5_dual_confirm_verified_projects_and_traverses(passc_conn, monkeypatch,
                                                         human_admin_1, human_admin_2):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    _ingest(passc_conn, "bank", _cif_rows("bank"), _cif_glossary("bank"))
    ref = _sole_join_ref(passc_conn, "bank")
    key = fact_key(ref, "approved_join")

    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)  # dual -> VERIFIED
    project_confirmed_joins(passc_conn, source="bank",
                            pairs=list_approved_join_refs(passc_conn, "bank"))

    edges = _join_edges(passc_conn, "bank")
    assert edges == [("public.transactions.cif_id", "public.customer.cif_id",
                      "operational", key, "VERIFIED")]
    path = find_join_path(passc_conn, "bank", "transactions", "customer")
    assert path is not None and len(path) == 1
    assert path[0].to_ref == "public.customer.cif_id" and path[0].cardinality == "N:1"

    for table in ("transactions", "customer"):
        rows = compute_relationship_readiness(passc_conn, source="bank", subset=table)
        assert len(rows) == 1, (table, rows)
        assert rows[0].status is RelationshipStatus.CONFIRMED


# ── Criterion 6: demotion via the PRODUCTION paths (expiry hook; pre-VERIFIED reject) ────────────


def test_c6_expiry_demotes_the_edge_without_a_reingest(passc_conn, monkeypatch,
                                                       human_admin_1, human_admin_2):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    _ingest(passc_conn, "bank", _cif_rows("bank"), _cif_glossary("bank"))
    ref = _sole_join_ref(passc_conn, "bank")
    key = fact_key(ref, "approved_join")
    _confirm_join(passc_conn, ref, admin1=human_admin_1, admin2=human_admin_2)
    project_confirmed_joins(passc_conn, source="bank",
                            pairs=list_approved_join_refs(passc_conn, "bank"))
    assert find_join_path(passc_conn, "bank", "transactions", "customer") is not None

    # PRODUCTION demotion: fire_due_overlay_expiries -> VERIFIED leaves -> the async hook
    # (demote_join_edges) flips the edge display_only IMMEDIATELY — no re-ingest, no projector run.
    _expire_join(passc_conn, ref)
    edges = _join_edges(passc_conn, "bank")
    assert edges == [("public.transactions.cif_id", "public.customer.cif_id",
                      "display_only", key, "REVERIFY")]     # link KEPT for the audit trail
    assert find_join_path(passc_conn, "bank", "transactions", "customer") is None


def test_c6_pre_verified_reject_never_operationalizes(passc_conn, monkeypatch, human_admin_1):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    _ingest(passc_conn, "rej", _cif_rows("rej"), _cif_glossary("rej"))
    ref = _sole_join_ref(passc_conn, "rej")

    _reject_join(passc_conn, ref, admin=human_admin_1)      # one admin rejects the pending DRAFT
    assert fold_overlay_state(
        load_fact(passc_conn, fact_key(ref, "approved_join"))).status == "REJECTED"
    assert _join_edges(passc_conn, "rej") == []             # never became an edge at all
    # Even a full projector pass over the fact refuses to project a REJECTED join.
    project_confirmed_joins(passc_conn, source="rej",
                            pairs=list_approved_join_refs(passc_conn, "rej"))
    assert _join_edges(passc_conn, "rej") == []
    assert find_join_path(passc_conn, "rej", "transactions", "customer") is None


# ── Criterion 7: a shared entity tag NEVER bypasses the governed join gate ───────────────────────


def test_c7_shared_entity_tag_never_bypasses_the_governed_gate(passc_conn, monkeypatch):
    """The governed-bypass proof (second only to proposed-not-traversable). Two tables share the
    confirmed ``Customer`` entity tag; a strong approved_join DRAFT exists but is NOT VERIFIED.
    Every OPERATIONAL feature-construction entry point must refuse the pair:

    * ``find_join_path`` -> None (no operational VERIFIED-backed edge exists);
    * the ``graph_edge`` substrate itself holds NO ``joins`` row for the source;
    * ``route_strategies`` (the feature planner's router) yields NO join ("aggregation") strategy;
    * ``find_cross_catalog_path``'s governed JOIN layer contributes nothing — any path it returns
      consists ONLY of explicitly-labelled advisory ``kind="entity"`` steps.

    The ADVISORY layer is pinned, not denied: ``cross_join_via_entity`` and the entity-bridge hops
    of ``find_cross_catalog_path`` DO surface the shared-tag bridge — by documented design
    ("callers surface it for human confirmation"). The module docstring carries the caller
    analysis proving no operational path consumes that advisory result without a human gate
    (``cross_join_via_entity``: zero production callers; entity-bridge CrossSteps: consumed only
    by cross-catalog contract authoring into a human-confirmed, never-executed draft field)."""
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    rows = [CanonicalRow("crm", "customers", "customer_id", "integer",
                         is_grain=True, entity="Customer"),
            CanonicalRow("crm", "cases", "customer_id", "integer", entity="Customer")]
    _ingest(passc_conn, "crm", rows, GlossaryUpload(rows=[], records=[
        _rec("crm", "customers", "customer_id", "Customer Identifier"),
        _rec("crm", "cases", "customer_id", "Customer Identifier")]))

    # The shared tag is really there, and the governed proposal is pending — NOT VERIFIED.
    tags = {r[0] for r in passc_conn.execute(
        "SELECT entity FROM graph_node WHERE catalog_source='crm' AND kind='column'").fetchall()}
    assert tags == {"Customer"}
    ref = _sole_join_ref(passc_conn, "crm")
    assert fold_overlay_state(
        load_fact(passc_conn, fact_key(ref, "approved_join"))).status == "DRAFT"

    # 1+2. The operational join-path finder and its substrate are CLOSED.
    assert find_join_path(passc_conn, "crm", "cases", "customers") is None
    assert _join_edges(passc_conn, "crm") == []
    # 3. The feature planner routes NO join strategy over the pair (entity tags may enable the
    #    non-join "distributional" lens; "aggregation" — the join lens — must stay off).
    picks = {name for name, _ in route_strategies(passc_conn, [
        {"object_ref": "public.cases.customer_id", "catalog_source": "crm"},
        {"object_ref": "public.customers.customer_id", "catalog_source": "crm"}])}
    assert "aggregation" not in picks
    # 4. Cross-catalog finder: the governed join layer contributed NOTHING — the only route is the
    #    advisory entity bridge, every step explicitly labelled kind="entity".
    xpath = find_cross_catalog_path(passc_conn, "crm", "cases", "crm", "customers")
    assert xpath is not None and all(step.kind == "entity" for step in xpath)
    # 5. The advisory primitive still surfaces the bridge for HUMAN confirmation (by design).
    bridge = cross_join_via_entity(passc_conn, "crm", "cases", "crm", "customers")
    assert bridge is not None and bridge.entity == "Customer"


# ── Criterion 8: re-ingest of the same glossary dedupes (SKIP_ACTIVE) — one DRAFT, not two ───────


def test_c8_reingest_dedupes_to_a_single_draft(passc_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    _ingest(passc_conn, "bank", _cif_rows("bank"), _cif_glossary("bank"))
    _ingest(passc_conn, "bank", _cif_rows("bank"), _cif_glossary("bank"))   # identical re-ingest

    assert len(_ledger(passc_conn, "bank")) == 1            # clear-then-write: one row, one pair
    ref = _sole_join_ref(passc_conn, "bank")                # ONE proposal in the read model
    key = fact_key(ref, "approved_join")
    events = load_fact(passc_conn, key)
    assert [e.type for e in events].count("OVERLAY_FACT_PROPOSED") == 1     # deduped, not doubled
    assert fold_overlay_state(events).status == "DRAFT"
    assert len(_open_task_ids(passc_conn, key)) == 2        # still exactly the two gate tasks


# ── Criterion 9: the reviewer sees the evidence (score/signals/namespace/grain/explanation) ──────


def test_c9_reviewer_task_surfaces_the_candidate_evidence(passc_conn, monkeypatch, human_admin_1):
    monkeypatch.setenv("OVERLAY_PASS_C", "1")
    _ingest(passc_conn, "bank", _cif_rows("bank"), _cif_glossary("bank"))
    ref = _sole_join_ref(passc_conn, "bank")
    task_ids = _open_task_ids(passc_conn, fact_key(ref, "approved_join"))
    assert len(task_ids) == 2

    proposal = get_task_proposal(passc_conn, task_ids[0], human_admin_1)
    assert proposal["fact_type"] == "approved_join"
    assert proposal["proposed_value"]["cardinality"] == "N:1"

    evidence = proposal["evidence"]
    assert evidence is not None, "the gate task must carry the pre-minted candidate evidence"
    mv = evidence.metric_values                             # asdict(JoinCandidateEvidenceV1)
    assert mv["bucket"] == "strong" and mv["score"] >= 80   # the score
    names = {s["signal_name"] for s in mv["positive_signals"]}          # the positive signals
    assert {"same_identifier_concept", "same_column_name", "one_side_confirmed_grain"} <= names
    assert mv["namespace_compatibility"] == "compatible"    # namespace compatibility
    assert mv["cardinality_status"] == "inferred_from_confirmed_grain"  # grain-inference status
    assert "confirmed grain" in mv["explanation"]           # the human-readable explanation


# ── Flag OFF (light — the deep byte-for-byte suite is test_passc_ingest.py) ──────────────────────


def test_flag_off_same_glossary_yields_no_approved_join_and_declared_stays_operational(
        passc_conn, monkeypatch):
    monkeypatch.delenv("OVERLAY_PASS_C", raising=False)
    monkeypatch.delenv("OVERLAY_GOVERNED_JOINS", raising=False)
    rows = [CanonicalRow("legacy", "customer", "cif_id", "integer", is_grain=True),
            CanonicalRow("legacy", "transactions", "cif_id", "integer",
                         joins_to="customer.cif_id", cardinality="N:1")]
    _ingest(passc_conn, "legacy", rows, _cif_glossary("legacy"))

    assert _ledger(passc_conn, "legacy") == []                          # no candidate ledger rows
    assert list_approved_join_refs(passc_conn, "legacy") == []          # no approved_join facts
    assert passc_conn.execute("SELECT count(*) FROM human_tasks").fetchone()[0] == 0
    assert _join_edges(passc_conn, "legacy") == [                       # declared edge untouched
        ("public.transactions.cif_id", "public.customer.cif_id", "operational", None, None)]
    path = find_join_path(passc_conn, "legacy", "transactions", "customer")
    assert path is not None and len(path) == 1
