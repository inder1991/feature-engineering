"""Delivery E / Task E2 — semantic-binding governance read model + review bridge (DB level).

The owner-or-admin sibling of ``join_governance`` / ``table_fact_governance`` for the governed
``entity_assignment`` / ``currency_binding`` facts D2 proposes + E3 projects. These tests exercise
the domain layer directly (the routes are covered in ``tests/featuregen/api/…``):

* ``list_semantic_binding_proposals`` — lists BOTH pending AND VERIFIED bindings, ONE view per
  fact_key, with the candidate evidence + candidate-set / ingestion-run provenance and the
  server-sanctioned ``available_actions`` (confirm/reject vs reverify/withdraw/correct).
* ``load_semantic_binding_confirmation_context`` — the fact_type-VALIDATED confirm/reject bridge,
  proven by driving a REAL ``confirm_fact`` with its own ``target_event_id`` (CAS).
* owner-or-admin authority (E1 ``resolve_authority``) + four-eyes (proposer ≠ confirmer) + the
  ``enter_fact`` self-confirm block.
* reverify / withdraw / correct on a VERIFIED binding — reusing the sanctioned expiry/reverify
  transition + the REAL overlay commands (never hand-writing fact state); withdraw → demote,
  correct → a NEW proposal requiring a DIFFERENT confirmer.
"""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb
from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity
from tests.featuregen.overlay._helpers import seed_verified_via_command

from featuregen.contracts import Command
from featuregen.overlay.commands import confirm_fact, enter_fact, propose_fact
from featuregen.overlay.identity import CatalogObjectRef, fact_key, proposal_fingerprint
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.semantic_binding_governance import (
    SemanticBindingGovernanceNotFound,
    correct_binding,
    list_semantic_binding_proposals,
    load_semantic_binding_confirmation_context,
    request_reverify,
    withdraw_binding,
)
from featuregen.overlay.upload.semantic_bindings.projection import verified_currency_binding

SOURCE = "fixture"
ENTITY = "customer"  # a stable member of the closed known_entities() vocabulary

ALICE = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
EVE = mint_test_identity(subject="user:eve", role_claims=("data_owner",))
ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
ADMIN2 = mint_test_identity(subject="user:admin2", role_claims=("platform-admin",))
SVC = mint_test_service_identity(subject="service:overlay", role_claims=("overlay",),
                                 attestation="sig")


# ── ref / value builders ─────────────────────────────────────────────────────────────────────────

def _measure_col(source=SOURCE) -> CatalogObjectRef:
    return CatalogObjectRef(source, "column", "sales", "trades", "notional")


def _entity_col(source=SOURCE) -> CatalogObjectRef:
    return CatalogObjectRef(source, "column", "sales", "party", "cust_id")


def _ccy_ref(column="ccy", source=SOURCE) -> dict:
    return {"catalog_source": source, "object_kind": "column", "schema": "sales",
            "table": "trades", "column": column}


def _cb_value(column="ccy") -> dict:
    return {"currency_column": _ccy_ref(column=column)}


def _ea_value(entity=ENTITY) -> dict:
    return {"entity_id": entity}


def _confirm_via_context(conn, key, actor, *, idem="k"):
    """Drive a REAL confirm_fact with the E2 context bridge's own args — the load-bearing proof that
    ``target_event_id`` is the exact CAS target the command accepts (owner-or-admin confirm)."""
    ctx = load_semantic_binding_confirmation_context(conn, key)
    return confirm_fact(conn, Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
         "target_event_id": ctx["target_event_id"]}, actor, idem))


# ── (1) list: pending + VERIFIED with actions + provenance ───────────────────────────────────────

def _seed_currency_draft(conn, source="src"):
    ref = CatalogObjectRef(source, "column", "public", "trades", "notional")
    value = {"currency_column": {"catalog_source": source, "object_kind": "column",
                                 "schema": "public", "table": "trades", "column": "ccy"}}
    res = propose_fact(conn, Command(
        "propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "currency_binding", "proposed_value": value},
        SVC, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    return ref, fact_key(ref, "currency_binding"), res.produced_event_ids[0]


def _link_candidate(conn, *, fk, source, proposed_event_id, reason_codes, evidence):
    """Persist a minimal D1 candidate set + candidate + proposal link (raw SQL — the WORM triggers
    block UPDATE/DELETE, never INSERT) so the read model's provenance join has a row to surface."""
    conn.execute(
        "INSERT INTO semantic_binding_candidate_set (candidate_set_id, catalog_source, "
        "table_graph_ref, ingestion_run_id, attempt_no, metadata_input_fingerprint, task_version, "
        "prompt_version, schema_version, config_version, completion_status, content_hash) "
        "VALUES ('cset-1', %s, 'public.trades', 'run-42', 1, 'fp-1', 'v1', 'n/a', 'sv1', 'cv1', "
        "'complete', 'ch1') ON CONFLICT DO NOTHING", (source,))
    conn.execute(
        "INSERT INTO semantic_binding_candidate (candidate_id, candidate_set_id, catalog_source, "
        "subject_graph_ref, subject_logical_ref, binding_kind, target_graph_ref, "
        "target_logical_ref, "
        "proposed_value, disposition, reason_codes, evidence_json, input_hash, model_version, "
        "prompt_version, schema_version, config_version) "
        "VALUES ('cand-1', 'cset-1', %s, 'public.trades.notional', %s, 'currency_binding', "
        "'public.trades.ccy', %s, NULL, 'strong', %s, %s, 'ih1', 'deterministic', 'n/a', 'sv1', "
        "'cv1') ON CONFLICT DO NOTHING",
        (source, f"{source}::public.trades.notional", f"{source}::public.trades.ccy",
         Jsonb(reason_codes), Jsonb(evidence)))
    conn.execute(
        "INSERT INTO semantic_binding_candidate_proposal "
        "(candidate_id, fact_key, proposed_event_id) "
        "VALUES ('cand-1', %s, %s) ON CONFLICT DO NOTHING", (fk, proposed_event_id))


def test_list_pending_and_verified_with_actions_and_provenance(overlay_conn):
    conn = overlay_conn
    # A DRAFT currency binding (service-proposed) + a VERIFIED one, both under source 'src'.
    _dref, dkey, dev = _seed_currency_draft(conn, source="src")
    _link_candidate(conn, fk=dkey, source="src", proposed_event_id=dev,
                    reason_codes=["over_bound"],
                    evidence={"signals": ["name_match"], "subject_concept": "amount"})
    vref = CatalogObjectRef("src", "column", "public", "settlements", "amt")
    vvalue = {"currency_column": {"catalog_source": "src", "object_kind": "column",
                                  "schema": "public", "table": "settlements", "column": "ccy"}}
    vres = propose_fact(conn, Command("propose_fact", "overlay_fact", None,
        {"ref": vref, "fact_type": "currency_binding", "proposed_value": vvalue},
        SVC, proposal_fingerprint(vvalue)))
    assert vres.accepted, vres.denied_reason
    vkey = fact_key(vref, "currency_binding")
    conf = confirm_fact(conn, Command("confirm_fact", "overlay_fact", None,
        {"ref": vref, "fact_type": "currency_binding", "use_case": None,
         "target_event_id": vres.produced_event_ids[0]}, ADMIN, "vc"))
    assert conf.accepted, conf.denied_reason

    views = list_semantic_binding_proposals(conn, "src")
    by_key = {v["fact_key"]: v for v in views}
    assert set(by_key) == {dkey, vkey}
    # pending -> confirm/reject; VERIFIED -> reverify/withdraw/correct (the asset-UI edit key)
    assert by_key[dkey]["status"] == "PROPOSED"
    assert by_key[dkey]["available_actions"] == ["confirm", "reject"]
    assert by_key[vkey]["status"] == "VERIFIED"
    assert by_key[vkey]["available_actions"] == ["reverify", "withdraw", "correct"]
    # provenance / evidence / reason codes surfaced from the linked D1 candidate
    d = by_key[dkey]
    assert d["binding_kind"] == "currency_binding"
    assert d["subject"] == {"schema": "public", "table": "trades", "column": "notional"}
    assert d["target"] == {"schema": "public", "table": "trades", "column": "ccy"}
    assert d["reason_codes"] == ["over_bound"]
    assert d["evidence"] == {"signals": ["name_match"], "subject_concept": "amount"}
    assert d["ingestion_run_id"] == "run-42" and d["candidate_set_id"] == "cset-1"
    assert d["target_event_id"] is not None  # the CAS target


def test_list_is_read_scoped_to_the_source(overlay_conn):
    _seed_currency_draft(overlay_conn, source="src")
    assert list_semantic_binding_proposals(overlay_conn, "some-other-source") == []


# ── (2) context bridge is fact_type-VALIDATED ────────────────────────────────────────────────────

def test_context_bridge_rejects_a_non_semantic_fact(overlay_conn):
    # A grain fact_key is not a semantic binding — the bridge raises (routes 404 pre-dispatch).
    from featuregen.overlay.upload.upload_catalog import table_ref
    ref = table_ref("src", "t")
    value = {"columns": ["cif_id"], "is_unique": True}
    res = propose_fact(overlay_conn, Command("propose_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "proposed_value": value},
        SVC, proposal_fingerprint(value)))
    assert res.accepted, res.denied_reason
    with pytest.raises(SemanticBindingGovernanceNotFound):
        load_semantic_binding_confirmation_context(overlay_conn, fact_key(ref, "grain"))
    with pytest.raises(SemanticBindingGovernanceNotFound):
        load_semantic_binding_confirmation_context(overlay_conn, "no-such-fact-key")


# ── (3) confirm DRAFT -> VERIFIED via the context bridge (CAS target proven) + projects edge ──────

def test_confirm_via_context_reaches_verified_and_projects_edge(db, catalog):
    catalog.set_owner(_measure_col(), "user:alice")
    draft = propose_fact(db, Command("propose_fact", "overlay_fact", None,
        {"ref": _measure_col(), "fact_type": "currency_binding", "proposed_value": _cb_value()},
        SVC, proposal_fingerprint(_cb_value())))
    assert draft.accepted, draft.denied_reason
    key = fact_key(_measure_col(), "currency_binding")
    res = _confirm_via_context(db, key, ALICE)  # owner confirms
    assert res.accepted, res.denied_reason
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    # E3 projected the operational currency edge (status='VERIFIED' 2nd gate passes)
    assert verified_currency_binding(db, key) is not None


def test_stale_target_event_id_is_denied(db, catalog):
    catalog.set_owner(_measure_col(), "user:alice")
    propose_fact(db, Command("propose_fact", "overlay_fact", None,
        {"ref": _measure_col(), "fact_type": "currency_binding", "proposed_value": _cb_value()},
        SVC, proposal_fingerprint(_cb_value())))
    key = fact_key(_measure_col(), "currency_binding")
    res = confirm_fact(db, Command("confirm_fact", "overlay_fact", None,
        {"ref": _measure_col(), "fact_type": "currency_binding", "use_case": None,
         "target_event_id": "stale-not-the-head"}, ALICE, "k"))
    assert res.accepted is False and "has been superseded" in res.denied_reason
    assert fold_overlay_state(load_fact(db, key)).status == "DRAFT"  # unchanged


# ── (4) four-eyes: service-proposed confirmed by a human works; self-propose+approve REFUSED ──────

def test_service_proposed_confirmed_by_human(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    propose_fact(db, Command("propose_fact", "overlay_fact", None,
        {"ref": _entity_col(), "fact_type": "entity_assignment", "proposed_value": _ea_value()},
        SVC, proposal_fingerprint(_ea_value())))
    key = fact_key(_entity_col(), "entity_assignment")
    assert _confirm_via_context(db, key, ALICE).accepted  # proposer(service) != confirmer(alice)


def test_human_proposer_cannot_confirm_own_binding(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    propose_fact(db, Command("propose_fact", "overlay_fact", None,
        {"ref": _entity_col(), "fact_type": "entity_assignment", "proposed_value": _ea_value()},
        ALICE, proposal_fingerprint(_ea_value())))   # alice PROPOSES
    key = fact_key(_entity_col(), "entity_assignment")
    res = _confirm_via_context(db, key, ALICE)         # alice cannot also confirm
    assert res.accepted is False and "four-eyes" in res.denied_reason


def test_enter_fact_self_confirm_blocked(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    res = enter_fact(db, Command("enter_fact", "overlay_fact", None,
        {"ref": _entity_col(), "fact_type": "entity_assignment", "proposed_value": _ea_value()},
        ALICE, "e"))
    assert res.accepted is False and "two-party propose/confirm" in res.denied_reason


# ── (5) authz: owner OR admin can confirm; a non-owner non-admin is refused (audited deny) ────────

def _denial_rows(conn):
    return [r[0] for r in conn.execute(
        "SELECT reason FROM security_audit WHERE event_type = 'COMMAND_DENIED' ORDER BY seq"
    ).fetchall()]


def test_owner_confirms(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    propose_fact(db, Command("propose_fact", "overlay_fact", None,
        {"ref": _entity_col(), "fact_type": "entity_assignment", "proposed_value": _ea_value()},
        SVC, proposal_fingerprint(_ea_value())))
    key = fact_key(_entity_col(), "entity_assignment")
    assert _confirm_via_context(db, key, ALICE).accepted


def test_admin_confirms_even_when_owner_is_known(db, catalog):
    catalog.set_owner(_measure_col(), "user:alice")
    propose_fact(db, Command("propose_fact", "overlay_fact", None,
        {"ref": _measure_col(), "fact_type": "currency_binding", "proposed_value": _cb_value()},
        SVC, proposal_fingerprint(_cb_value())))
    key = fact_key(_measure_col(), "currency_binding")
    assert _confirm_via_context(db, key, ADMIN).accepted   # owner-or-admin (E1 admin_confirmable)


def test_non_owner_non_admin_refused_with_audited_deny(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    propose_fact(db, Command("propose_fact", "overlay_fact", None,
        {"ref": _entity_col(), "fact_type": "entity_assignment", "proposed_value": _ea_value()},
        SVC, proposal_fingerprint(_ea_value())))
    key = fact_key(_entity_col(), "entity_assignment")
    res = _confirm_via_context(db, key, EVE)   # eve is a data_owner but NOT the owner nor an admin
    assert res.accepted is False and "not the resolved authority" in res.denied_reason
    assert "actor is not the resolved authority for this fact" in _denial_rows(db)  # tamper-evident


# ── (6) reverify / withdraw / correct on a VERIFIED binding ───────────────────────────────────────

def test_withdraw_demotes_the_verified_binding(db):
    key, _ = seed_verified_via_command(db, ref=_measure_col(), fact_type="currency_binding",
                                       value=_cb_value(), owner="user:alice")
    assert verified_currency_binding(db, key) is not None
    res = withdraw_binding(db, fact_key=key, actor=ADMIN, category="no_longer_valid", note="retire")
    assert res["accepted"] and res["governance_status"] == "REJECTED"
    assert res["operational_projection"] == "demoted"
    assert fold_overlay_state(load_fact(db, key)).status == "REJECTED"
    assert verified_currency_binding(db, key) is None   # 2nd gate hides the demoted edge


def test_reverify_reopens_the_cycle_and_demotes(db):
    key, _ = seed_verified_via_command(db, ref=_measure_col(), fact_type="currency_binding",
                                       value=_cb_value(), owner="user:alice")
    res = request_reverify(db, fact_key=key, actor=ADMIN)
    assert res["accepted"] and res["governance_status"] == "REVERIFY"
    assert fold_overlay_state(load_fact(db, key)).status == "REVERIFY"
    assert verified_currency_binding(db, key) is None   # demoted until re-confirmed
    # a re-confirm re-affirms the value -> VERIFIED again (proposer is the service, admin confirms)
    assert _confirm_via_context(db, key, ADMIN, idem="reaffirm").accepted
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"


def test_correct_opens_a_new_proposal_requiring_a_distinct_confirmer(db):
    key, _ = seed_verified_via_command(db, ref=_measure_col(), fact_type="currency_binding",
                                       value=_cb_value(), owner="user:alice")
    new_value = _cb_value(column="settle_ccy")   # same source/schema/table target — passes the gate
    res = correct_binding(db, fact_key=key, actor=ADMIN, value=new_value, note="fix ccy")
    assert res["accepted"] and res["governance_status"] == "PROPOSED"
    assert res["requires_distinct_confirmer"] is True
    assert res["operational_projection"] == "demoted"
    # the NEW proposal is proposed by ADMIN -> ADMIN cannot also confirm (four-eyes)
    self_confirm = _confirm_via_context(db, key, ADMIN, idem="self")
    assert self_confirm.accepted is False and "four-eyes" in self_confirm.denied_reason
    # a DISTINCT authorized human confirms -> VERIFIED with the CORRECTED value
    assert _confirm_via_context(db, key, ADMIN2, idem="other").accepted
    state = fold_overlay_state(load_fact(db, key))
    assert state.status == "VERIFIED" and state.value == new_value


def test_correct_rejects_a_bad_value_without_touching_the_binding(db):
    key, _ = seed_verified_via_command(db, ref=_measure_col(), fact_type="currency_binding",
                                       value=_cb_value(), owner="user:alice")
    bad = {"currency_column": {"catalog_source": "fixture", "object_kind": "column",
                               "schema": "sales", "table": "OTHER", "column": "ccy"}}  # cross-table
    res = correct_binding(db, fact_key=key, actor=ADMIN, value=bad)
    assert res["accepted"] is False and "corrected value rejected" in res["denied_reason"]
    # atomic: the binding is untouched (still VERIFIED with the original value)
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    assert verified_currency_binding(db, key) is not None
