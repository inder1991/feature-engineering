from types import SimpleNamespace

import pytest

from featuregen.contracts import ConcurrencyError
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.intake.mcv import (
    calculation_method_available,
    minimum_contract_validated,
    not_prohibited_intent,
    open_fields_empty,
    run_minimum_contract_validation,
)

# actor_is_request_owner + confirmer_is_requester_human are owned by P2 (intake.state), consumed here
# (R4) — never redefined in mcv.
from featuregen.intake.state import actor_is_request_owner, confirmer_is_requester_human

_CLEAR = {"outcome": "CLEAR", "catalog_version": "bdc-1"}


def _draft(**over):
    body = {
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date"},
            "calculation_method": "rolling_count",
            "windows": [{"name": "lookback", "value": "90d"}],
            "filters": [{"concept": "declined card authorization",
                         "predicate": "card_authorizations.auth_result = 'D'"}],
        },
        "field_scores": {
            "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
            "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
            "filters": {"ambiguity": 0.10, "confidence": 0.90, "source": "catalog"},
        },
        "open_fields": [],
    }
    body.update(over)
    return body


def _ledger(fields=("entity_grain",)):
    return {"request_id": "req_1",
            "assumptions": [{"field": f, "value": "v", "rationale": "r", "source": "default"} for f in fields]}


def test_definition_contract_passes_all_six_checks():
    res = minimum_contract_validated(
        _draft(), _ledger(), _CLEAR, mode="definition", candidate_count=0,
        confirmed_fields={"filters"},
    )
    assert res.passed is True, res.failures


def test_open_fields_nonempty_fails():
    res = minimum_contract_validated(
        _draft(open_fields=["filters.declined_status_encoding"]), _ledger(), _CLEAR,
        mode="definition", candidate_count=0,
    )
    assert res.passed is False
    assert "open_fields_nonempty" in res.failures


def test_unresolved_grain_fails():
    d = _draft()
    d["feature_semantics"]["entity_grain"] = ["UNKNOWN"]
    res = minimum_contract_validated(d, _ledger(), _CLEAR, mode="definition", candidate_count=0)
    assert "grain_unresolved" in res.failures


def test_prohibited_class_blocks_mcv():
    res = minimum_contract_validated(
        _draft(), _ledger(), {"outcome": "PROHIBITED_DATA_CLASS", "catalog_version": "bdc-1", "matched_class": "race"},
        mode="definition", candidate_count=0, confirmed_fields={"filters"},
    )
    assert res.passed is False
    assert any(f.startswith("blocked:") for f in res.failures)


def test_unavailable_classification_fails_closed():
    res = minimum_contract_validated(_draft(), _ledger(), None, mode="definition", candidate_count=0,
                                     confirmed_fields={"filters"})
    assert "classification_unavailable" in res.failures
    res2 = minimum_contract_validated(_draft(), _ledger(), {"outcome": "CLEAR"}, mode="definition",
                                      candidate_count=0, confirmed_fields={"filters"})
    assert "classification_unavailable" in res2.failures  # no resolvable version


def test_hypothesis_requires_a_candidate_set():
    d = _draft()
    d["feature_semantics"]["calculation_method"] = "UNKNOWN"
    assert calculation_method_available(d, mode="hypothesis", candidate_count=3) is True
    assert calculation_method_available(d, mode="hypothesis", candidate_count=0) is False
    res = minimum_contract_validated(d, _ledger(), _CLEAR, mode="hypothesis", candidate_count=0,
                                     confirmed_fields={"filters"})
    assert "calculation_method_unavailable" in res.failures


def test_high_ambiguity_field_without_account_fails_check_3():
    d = _draft()
    d["field_scores"]["filters"] = {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"}
    # filters neither in the ledger nor human-confirmed → check 3 fails
    res = minimum_contract_validated(d, _ledger(("entity_grain",)), _CLEAR, mode="definition",
                                     candidate_count=0, confirmed_fields=set())
    assert any(f.startswith("high_ambiguity_unaccounted") for f in res.failures)


def test_platform_supplied_field_needs_a_ledger_entry_check_6():
    # entity_grain has source=default (platform-supplied) but NO ledger entry → check 6 fails
    res = minimum_contract_validated(_draft(), _ledger(fields=()), _CLEAR, mode="definition",
                                     candidate_count=0, confirmed_fields={"filters"})
    assert any(f.startswith("unaccounted:") for f in res.failures)


def test_owner_and_confirmer_guards():
    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    other = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))
    svc = build_service_identity(subject="service:intake-agent", role_claims=("intake-agent",), attestation="s")
    # The ONE owner predicate is state-based (R4): actor_is_request_owner(state, actor); `state.requester`
    # is set by the P2 fold to the INTENT_SUBMITTED actor.subject. A folded state exposes `.requester`.
    raj_state = SimpleNamespace(requester="user:raj")
    svc_state = SimpleNamespace(requester="service:intake-agent")
    assert actor_is_request_owner(raj_state, owner) is True
    assert actor_is_request_owner(raj_state, other) is False
    assert confirmer_is_requester_human(raj_state, owner) is True
    assert confirmer_is_requester_human(raj_state, other) is False  # a different data scientist can't confirm
    assert confirmer_is_requester_human(svc_state, svc) is False    # a service can never confirm
    assert not_prohibited_intent(_CLEAR) is True
    assert not_prohibited_intent({"outcome": "OUT_OF_SCOPE", "catalog_version": "bdc-1"}) is False
    assert open_fields_empty(_draft()) is True


# ── DB-backed run_minimum_contract_validation ────────────────────────────────────────────────
# The brief's Step-1 test used P4 helpers (`freeze_draft`, `commands.read_contract_body`) that were
# never built, and passed `state.classification` (the fold surfaces that only as the rejection STRING,
# not the mapping). The real event-sourcing seams carry the bodies inline on DRAFT_CONTRACT_PRODUCED
# (`draft_body`/`assumption_ledger_body`, commands.py:474-475) and the classification mapping on
# INTENT_SUBMITTED (`classification` = R9 `as_mapping()`, commands.py:654). These tests exercise
# run_minimum_contract_validation against those real seams — see the module docstring in mcv.py.

def _open_stream(conn, run_id, owner, *, draft=None, ledger=None, classification=_CLEAR,
                 intake_mode="definition"):
    from featuregen.intake.store import append_feature_contract_event

    append_feature_contract_event(
        conn, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_mcv", "run_id": run_id, "intake_mode": intake_mode,
                 "raw_input_ref": "blob_x", "raw_input_classification": "clean",
                 "classification": classification},
        actor=owner, expected_version=0,
    )
    body = draft if draft is not None else _draft()
    append_feature_contract_event(
        conn, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
        payload={"draft_doc_id": "doc_draft", "assumption_ledger_ref": "doc_ledger",
                 "open_fields": list(body.get("open_fields", [])), "intake_mode": intake_mode,
                 "draft_body": body,
                 "assumption_ledger_body": ledger if ledger is not None else _ledger(("entity_grain", "filters"))},
        actor=owner,
    )


def test_run_minimum_contract_validation_folds_and_appends_the_event(db, agent):
    """R5 DB-backed MCV: fold the feature_contract status (P2 R3), read the current draft/ledger/
    classification off the stream, run the pure checklist, append MINIMUM_CONTRACT_VALIDATED on a pass;
    return a CommandResult whose `.accepted` is the boundary P7's open_gate1_task reads."""
    from featuregen.intake.store import load_feature_contract

    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    run_id = "run_mcv"
    _open_stream(db, run_id, owner)

    res = run_minimum_contract_validation(db, run_id, actor=agent)
    assert res.accepted is True, res.denied_reason
    assert "MINIMUM_CONTRACT_VALIDATED" in [e.type for e in load_feature_contract(db, run_id)]


def test_run_minimum_contract_validation_denies_a_failing_contract(db, agent):
    """A failing pure checklist → denied, NO event appended (stays in the Refinement Loop)."""
    from featuregen.intake.store import load_feature_contract

    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    run_id = "run_mcv_fail"
    _open_stream(db, run_id, owner, draft=_draft(open_fields=["filters.declined_status_encoding"]))

    res = run_minimum_contract_validation(db, run_id, actor=agent)
    assert res.accepted is False
    assert res.denied_reason.startswith("mcv_failed")
    assert "MINIMUM_CONTRACT_VALIDATED" not in [e.type for e in load_feature_contract(db, run_id)]


def test_run_minimum_contract_validation_is_idempotent(db, agent):
    """A second run over an already-validated contract accepts without re-appending (no-regression)."""
    from featuregen.intake.store import load_feature_contract

    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    run_id = "run_mcv_idem"
    _open_stream(db, run_id, owner)

    assert run_minimum_contract_validation(db, run_id, actor=agent).accepted is True
    assert run_minimum_contract_validation(db, run_id, actor=agent).accepted is True
    mcv_events = [e for e in load_feature_contract(db, run_id) if e.type == "MINIMUM_CONTRACT_VALIDATED"]
    assert len(mcv_events) == 1


def test_run_minimum_contract_validation_x4_stale_denies(db, agent, monkeypatch):
    """X4: the append is CAS-pinned to the folded head; a concurrent transition (ConcurrencyError)
    fails closed as `stale` rather than committing MCV on top of a raced head."""
    import featuregen.intake.mcv as mcv

    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    run_id = "run_mcv_stale"
    _open_stream(db, run_id, owner)

    def _boom(*a, **k):
        raise ConcurrencyError("head advanced")

    monkeypatch.setattr(mcv, "append_feature_contract_event", _boom)
    res = run_minimum_contract_validation(db, run_id, actor=agent)
    assert res.accepted is False
    assert res.denied_reason == "stale"


def test_hypothesis_mode_uses_candidate_docs(db, agent):
    """Hypothesis mode: the candidate count comes from the folded candidate_doc_ids, not a method."""
    from featuregen.intake.store import append_feature_contract_event, load_feature_contract

    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    run_id = "run_mcv_hyp"
    d = _draft()
    d["feature_semantics"].pop("calculation_method")  # hypothesis: no single method — a candidate set
    append_feature_contract_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_h", "run_id": run_id, "intake_mode": "hypothesis",
                 "raw_input_ref": "blob_x", "raw_input_classification": "clean", "classification": _CLEAR},
        actor=owner, expected_version=0,
    )
    append_feature_contract_event(
        db, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
        payload={"draft_doc_id": "doc_draft", "assumption_ledger_ref": "doc_ledger",
                 "candidate_doc_ids": ["cand_a", "cand_b", "cand_c"], "open_fields": [],
                 "intake_mode": "hypothesis", "draft_body": d,
                 "assumption_ledger_body": _ledger(("entity_grain", "filters"))},
        actor=owner,
    )
    res = run_minimum_contract_validation(db, run_id, actor=agent)
    assert res.accepted is True, res.denied_reason
    assert "MINIMUM_CONTRACT_VALIDATED" in [e.type for e in load_feature_contract(db, run_id)]


@pytest.mark.parametrize("bad", [None, {}])
def test_run_denies_when_no_draft_body_on_stream(db, agent, bad):
    """Defensive: a stream with no DRAFT_CONTRACT_PRODUCED body cannot pass the checklist."""
    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    run_id = f"run_mcv_nobody_{id(bad)}"
    from featuregen.intake.store import append_feature_contract_event

    append_feature_contract_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_nb", "run_id": run_id, "intake_mode": "definition",
                 "raw_input_ref": "blob_x", "raw_input_classification": "clean", "classification": _CLEAR},
        actor=owner, expected_version=0,
    )
    res = run_minimum_contract_validation(db, run_id, actor=agent)
    assert res.accepted is False
