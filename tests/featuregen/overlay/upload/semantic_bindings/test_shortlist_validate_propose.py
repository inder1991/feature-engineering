"""D2 — the deterministic shortlist + validate + store + propose→E1 DRAFT facts.

Pure unit tests for ``shortlist`` / ``validate`` (no DB, no LLM, no sample-shape); real-DB tests for
``store`` (D1 immutable set + CAS current projection) and ``propose`` (E1 DRAFT fact command + the
proposal LINK). The ``conn`` fixture is the migrated PG connection (writes roll back on teardown);
``catalog`` registers a StubCatalog so ``propose_fact`` resolves authority.
"""
from __future__ import annotations

import pytest
from tests.featuregen._helpers import mint_test_service_identity

from featuregen.overlay.identity import CatalogObjectRef, display_object_ref, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.column_view import ColumnMetadataView, TableMetadataView
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.semantic_bindings import (
    BINDING_KINDS,
    DISPOSITIONS,
    PassCIdentifier,
    SemanticBindingCandidate,
    candidate_id_for,
    propose,
    shortlist,
    store_shortlist,
    to_fact_command,
    validate,
    validate_candidates,
)
from featuregen.overlay.upload.semantic_bindings import store_projection as d1
from featuregen.overlay.upload.semantic_bindings.types import (
    RC_AMBIGUOUS_TARGET,
    RC_ENTITY_NOT_KNOWN,
    RC_OVER_BOUND,
    RC_SUBJECT_NOT_IN_ROSTER,
    RC_TARGET_NOT_IN_ROSTER,
    ColumnRef,
    Evidence,
)
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

SVC = mint_test_service_identity(subject="service:overlay", role_claims=("overlay",),
                                 attestation="sig")
_SRC = "src"
_SCHEMA = "public"
_TABLE = "txn"


# --- builders ------------------------------------------------------------------------------------
def _col(column: str, *, concept: str | None = None, term_type: str = "",
         semantic_type: str | None = None, source: str = _SRC, schema: str = _SCHEMA,
         table: str = _TABLE) -> ColumnMetadataView:
    return ColumnMetadataView(
        source=source, schema=schema, table=table, column=column,
        logical_ref=normalize_ref(source, schema, table, column),
        operational_type="text", declared_type="", term_name="", business_definition="",
        domain="", term_type=term_type, process_path="", synonyms=(), bian_path="", fibo_path="",
        semantic_type=semantic_type, logical_representation=None, concept=concept,
        drafted_definition=None, classified_domain=None, sidecar_attached=False)


def _view(columns, *, source: str = _SRC, schema: str = _SCHEMA,
          table: str = _TABLE) -> TableMetadataView:
    return TableMetadataView(
        source=source, schema=schema, table=table,
        logical_ref=normalize_ref(source, schema, table), table_definition=None, term_name=None,
        columns=tuple(columns))


def _pc_key(source: str, schema: str, table: str, column: str) -> str:
    return normalize_ref(source, schema, table, column)


# ==================================================================================================
# 1) shortlist determinism
# ==================================================================================================
def test_shortlist_is_deterministic() -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code"),
                  _col("cust_id")])
    pass_c = {_pc_key(_SRC, _SCHEMA, _TABLE, "cust_id"):
              PassCIdentifier(join_key_eligible=True, entity="customer")}
    a = shortlist(view, None, pass_c)
    b = shortlist(view, None, pass_c)
    assert a == b                                    # identical tuple (order + content)
    assert isinstance(a, tuple) and len(a) == 2
    # candidate content is fully hashable/immutable
    assert len(set(a)) == 2


# ==================================================================================================
# 2) roster constraint — targets ONLY from the server roster; a fabricated FQN never appears
# ==================================================================================================
def test_every_emitted_ref_is_a_roster_column() -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    roster = {c.logical_ref for c in view.columns}
    for cand in shortlist(view):
        assert cand.subject.logical_ref in roster
        if cand.target is not None:
            assert cand.target.logical_ref in roster


def test_validate_rejects_a_fabricated_off_roster_target() -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    ghost = ColumnRef(_SRC, _SCHEMA, _TABLE, "ghost_ccy",
                      normalize_ref(_SRC, _SCHEMA, _TABLE, "ghost_ccy"))
    fabricated = SemanticBindingCandidate(
        binding_kind="currency_binding",
        subject=ColumnRef.from_view(view.columns[0]), target=ghost, disposition="strong",
        input_hash="ih", evidence=Evidence())
    assert validate(fabricated, view).reason_code == RC_TARGET_NOT_IN_ROSTER


def test_validate_rejects_off_roster_subject() -> None:
    view = _view([_col("ccy", concept="currency_code")])
    off = ColumnRef(_SRC, _SCHEMA, "other", "amt", normalize_ref(_SRC, _SCHEMA, "other", "amt"))
    cand = SemanticBindingCandidate(
        binding_kind="currency_binding", subject=off,
        target=ColumnRef.from_view(view.columns[0]), disposition="strong", input_hash="ih")
    assert validate(cand, view).reason_code == RC_SUBJECT_NOT_IN_ROSTER


# ==================================================================================================
# 3) currency rules — unambiguous -> strong; two equally-plausible -> weak; NO sample-shape
# ==================================================================================================
def test_unambiguous_currency_is_strong() -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    assert len(cands) == 1
    (c,) = cands
    assert c.binding_kind == "currency_binding" and c.disposition == "strong"
    assert c.subject.column == "amt" and c.target.column == "ccy"


def test_ambiguous_currency_targets_are_weak() -> None:
    view = _view([_col("amt", concept="monetary_flow"),
                  _col("ccy", concept="currency_code"),
                  _col("settle_ccy", concept="base_currency")])
    cands = shortlist(view)
    assert len(cands) == 2
    assert all(c.disposition == "weak" for c in cands)     # ambiguity is preserved, never guessed
    assert {c.target.column for c in cands} == {"ccy", "settle_ccy"}


def test_currency_detected_from_structural_name_not_samples() -> None:
    # No concept anywhere — only structural names. A currency is NEVER inferred from sample values
    # (there are none on the view); an amount with NO currency column yields NO candidate.
    view = _view([_col("amount"), _col("txn_ccy")])
    cands = shortlist(view)
    assert len(cands) == 1 and cands[0].disposition == "strong"
    assert cands[0].subject.column == "amount" and cands[0].target.column == "txn_ccy"
    lonely = _view([_col("amount")])
    assert shortlist(lonely) == ()                        # no currency in the roster -> no candidate


# ==================================================================================================
# 4) entity rules — eligible + known -> candidate; unknown -> rejected; non-identifier -> nothing
# ==================================================================================================
def test_identifier_with_known_entity_is_a_candidate() -> None:
    view = _view([_col("cust_id")])
    pc = {_pc_key(_SRC, _SCHEMA, _TABLE, "cust_id"):
          PassCIdentifier(join_key_eligible=True, entity="customer")}
    (c,) = shortlist(view, None, pc)
    assert c.binding_kind == "entity_assignment" and c.disposition == "strong"
    assert c.entity_id == "customer" and c.target is None


def test_identifier_with_unknown_entity_is_rejected_not_dropped() -> None:
    view = _view([_col("cust_id")])
    pc = {_pc_key(_SRC, _SCHEMA, _TABLE, "cust_id"):
          PassCIdentifier(join_key_eligible=True, entity="not_a_real_entity")}
    (c,) = shortlist(view, None, pc)                       # emitted, NOT silently dropped
    assert c.disposition == "rejected" and RC_ENTITY_NOT_KNOWN in c.reason_codes


def test_non_identifier_column_is_not_an_entity_candidate() -> None:
    view = _view([_col("cust_id")])
    pc = {_pc_key(_SRC, _SCHEMA, _TABLE, "cust_id"):
          PassCIdentifier(join_key_eligible=False, entity="customer")}
    assert shortlist(view, None, pc) == ()
    assert shortlist(view, None, None) == ()               # no Pass C metadata -> no entity candidate


# ==================================================================================================
# 5) term_type=measure excludes; an open term_type alone does not classify
# ==================================================================================================
def test_measure_term_type_excludes_entity_candidacy() -> None:
    view = _view([_col("amt_id", term_type="measure")])
    pc = {_pc_key(_SRC, _SCHEMA, _TABLE, "amt_id"):
          PassCIdentifier(join_key_eligible=True, entity="customer")}
    assert shortlist(view, None, pc) == ()                 # rule 6: a measure is never an entity key


def test_open_term_type_alone_classifies_nothing() -> None:
    # An open-vocab term_type with no concept / currency / amount signal yields NOTHING by itself.
    view = _view([_col("some_col", term_type="regulatory term")])
    assert shortlist(view) == ()
    assert shortlist(view, None, None) == ()


# ==================================================================================================
# 6) validate — role + ambiguity + bound each carry a durable reason code
# ==================================================================================================
def test_validate_flags_a_strong_claim_over_ambiguous_targets() -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code"),
                  _col("settle_ccy", concept="base_currency")])
    # a hand-built STRONG candidate while two currency targets exist -> ambiguity reason.
    cand = SemanticBindingCandidate(
        binding_kind="currency_binding", subject=ColumnRef.from_view(view.columns[0]),
        target=ColumnRef.from_view(view.columns[1]), disposition="strong", input_hash="ih")
    assert validate(cand, view).reason_code == RC_AMBIGUOUS_TARGET


def test_validate_candidates_enforces_the_bound() -> None:
    cols = [_col(f"amt{i}", concept="monetary_flow") for i in range(3)] + \
           [_col("ccy", concept="currency_code")]
    view = _view(cols)
    cands = shortlist(view)                                # 3 strong currency candidates
    assert len(cands) == 3 and all(c.disposition == "strong" for c in cands)
    bounded = validate_candidates(cands, view, cap=2)
    kept = [c for c in bounded if c.disposition != "rejected"]
    over = [c for c in bounded if RC_OVER_BOUND in c.reason_codes]
    assert len(bounded) == 3 and len(kept) == 2 and len(over) == 1   # nothing dropped


# ==================================================================================================
# 7) propose — strong -> correct E1 DRAFT command; link only after success; NEVER verifies
# ==================================================================================================
def _entity_view():
    return _view([_col("cust_id")])


def _entity_candidate():
    view = _entity_view()
    pc = {_pc_key(_SRC, _SCHEMA, _TABLE, "cust_id"):
          PassCIdentifier(join_key_eligible=True, entity="customer")}
    (cand,) = shortlist(view, None, pc)
    return view, pc, cand


def test_to_fact_command_maps_currency_and_entity() -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    (cur,) = shortlist(view)
    cmd = to_fact_command(cur, actor=SVC, idempotency_key="k")
    assert cmd.action == "propose_fact" and cmd.args["fact_type"] == "currency_binding"
    assert cmd.args["proposed_value"]["currency_column"]["column"] == "ccy"
    assert cmd.args["proposed_value"]["currency_column"]["table"] == _TABLE

    _view2, _pc, ent = _entity_candidate()
    cmd2 = to_fact_command(ent, actor=SVC, idempotency_key="k")
    assert cmd2.args["fact_type"] == "entity_assignment"
    assert cmd2.args["proposed_value"] == {"entity_id": "customer"}


def test_propose_creates_a_draft_and_links_never_verified(conn, catalog) -> None:
    view, pc, cand = _entity_candidate()
    catalog.set_owner(CatalogObjectRef(_SRC, "column", _SCHEMA, _TABLE, "cust_id"), "user:alice")
    stored = store_shortlist(conn, table_view=view, candidates=[cand], catalog_source=_SRC,
                             ingestion_run_id="run_1", attempt_no=1, pass_c=pc)
    cid = candidate_id_for(cand, candidate_set_id=stored.persist.candidate_set_id)

    outcome = propose(conn, cand, candidate_id=cid, actor=SVC, idempotency_key="p1")
    assert outcome.accepted is True and outcome.linked is True

    # the fact is a DRAFT — NEVER verified by this path.
    fk = fact_key(CatalogObjectRef(_SRC, "column", _SCHEMA, _TABLE, "cust_id"), "entity_assignment")
    assert outcome.fact_key == fk
    assert fold_overlay_state(load_fact(conn, fk)).status == "DRAFT"
    # the proposal LINK exists, keyed to the candidate row (written AFTER propose_fact succeeded).
    row = conn.execute(
        "SELECT fact_key, proposed_event_id FROM semantic_binding_candidate_proposal "
        "WHERE candidate_id = %s", (cid,)).fetchone()
    assert row is not None and row[0] == fk and row[1] == outcome.proposed_event_id


def test_propose_refuses_a_non_strong_candidate(conn) -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code"),
                  _col("settle_ccy", concept="base_currency")])
    weak = shortlist(view)[0]
    assert weak.disposition == "weak"
    called = {"n": 0}

    def _never(_c, _cmd):        # a fake propose_fact that must never run for a weak candidate
        called["n"] += 1
        raise AssertionError("propose_fact must not be called for a weak candidate")

    out = propose(conn, weak, candidate_id="cid", actor=SVC, idempotency_key="p",
                  propose_fact=_never)
    assert out.accepted is False and out.linked is False and called["n"] == 0


def test_propose_does_not_link_when_propose_fact_denies(conn, catalog) -> None:
    _view2, pc, cand = _entity_candidate()
    stored = store_shortlist(conn, table_view=_view2, candidates=[cand], catalog_source=_SRC,
                             ingestion_run_id="run_1", attempt_no=1, pass_c=pc)
    cid = candidate_id_for(cand, candidate_set_id=stored.persist.candidate_set_id)
    from featuregen.contracts import CommandResult

    def _deny(_c, _cmd):
        return CommandResult(accepted=False, aggregate_id="fk_x", denied_reason="nope")

    out = propose(conn, cand, candidate_id=cid, actor=SVC, idempotency_key="p", propose_fact=_deny)
    assert out.accepted is False and out.linked is False
    assert conn.execute("SELECT 1 FROM semantic_binding_candidate_proposal WHERE candidate_id = %s",
                        (cid,)).fetchone() is None       # a denied proposal never orphans a link


# ==================================================================================================
# 8) store — persists via D1 (immutable set + CAS current); re-shortlist is idempotent
# ==================================================================================================
def test_store_persists_and_projects_current(conn) -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    res = store_shortlist(conn, table_view=view, candidates=cands, catalog_source=_SRC,
                          ingestion_run_id="run_1", attempt_no=1)
    assert res.persist.inserted is True
    assert res.projection is not None and res.projection.status == "current"
    cur = conn.execute(
        "SELECT candidate_set_id, status FROM current_semantic_binding_candidate_set "
        "WHERE catalog_source = %s AND table_graph_ref = %s",
        (_SRC, f"{_SCHEMA}.{_TABLE}")).fetchone()
    assert cur == (res.persist.candidate_set_id, "current")
    # the candidate landed in the immutable D1 store.
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate WHERE candidate_set_id = %s",
                        (res.persist.candidate_set_id,)).fetchone()[0] == len(cands)


def test_restore_unchanged_inputs_is_idempotent(conn) -> None:
    view = _view([_col("amt", concept="monetary_flow"), _col("ccy", concept="currency_code")])
    cands = shortlist(view)
    first = store_shortlist(conn, table_view=view, candidates=cands, catalog_source=_SRC,
                            ingestion_run_id="run_1", attempt_no=1)
    second = store_shortlist(conn, table_view=view, candidates=cands, catalog_source=_SRC,
                             ingestion_run_id="run_1", attempt_no=1)
    assert first.fingerprint == second.fingerprint
    assert second.persist.inserted is False                        # D1 replay — no new set
    assert second.persist.candidate_set_id == first.persist.candidate_set_id
    assert second.projection.status == "current"
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate_set").fetchone()[0] == 1


def test_store_persists_rejected_candidates_too(conn) -> None:
    view = _view([_col("cust_id")])
    pc = {_pc_key(_SRC, _SCHEMA, _TABLE, "cust_id"):
          PassCIdentifier(join_key_eligible=True, entity="not_a_real_entity")}
    (rej,) = shortlist(view, None, pc)
    assert rej.disposition == "rejected"
    res = store_shortlist(conn, table_view=view, candidates=[rej], catalog_source=_SRC,
                          ingestion_run_id="run_1", attempt_no=1, pass_c=pc)
    disp, codes = conn.execute(
        "SELECT disposition, reason_codes FROM semantic_binding_candidate WHERE candidate_set_id = %s",
        (res.persist.candidate_set_id,)).fetchone()
    assert disp == "rejected" and RC_ENTITY_NOT_KNOWN in codes    # durable, never dropped


# ==================================================================================================
# registry parity + probe guards
# ==================================================================================================
def test_registries_match_d1_and_probe_entity_is_known() -> None:
    assert BINDING_KINDS == d1.BINDING_KINDS
    assert DISPOSITIONS == d1.DISPOSITIONS
    assert "customer" in known_entities()
    assert display_object_ref(CatalogObjectRef(_SRC, "column", _SCHEMA, _TABLE, "cust_id")) \
        == "public.txn.cust_id"


@pytest.mark.parametrize("kind", sorted(BINDING_KINDS))
def test_binding_kinds_are_the_e1_governed_fact_types(kind) -> None:
    from featuregen.overlay import facts
    assert kind in facts.DATA_FACT_TYPES
