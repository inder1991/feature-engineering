"""§11.3 — the override is SERVER-VERIFIED evidence, or it is nothing.

The case worth reading first is the trio of refusals: an override naming a refusal that did not
happen — wrong draft, wrong candidate, wrong state, wrong code — is refused BY NAME, because
without that verification "Try AI formula" is a client-chosen method with extra steps.

Unique ids per test (the test_retirement_scope lesson).
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.overlay.upload.method_override import (
    OverrideRefusalUnverified,
    current_method_override,
    request_method_override,
)

_FUTURE = "2026-12-31T00:00:00Z"


def _spend(conn, tag: str) -> str:
    from featuregen.overlay.upload.llm_spend import authorize_spend

    return authorize_spend(
        conn, action="AUTHOR_FORMULA", actor_subject="user:sam", job_identity=f"job-{tag}",
        member_identities=[f"sel-{tag}"], provider_contract_hash="sha256:contract",
        max_calls=5, max_tokens=50_000, currency="USD", max_cost="5.00",
        pricing_version="p@1", expires_at=_FUTURE)


def _draft(conn, tag: str, *, state: str = "BLOCKED",
           blockers: list | None = None) -> str:
    considered = f"crev-{tag}"
    seed_run_chain(conn, run_id=f"mo-{tag}", considered_revision_id=considered)
    draft_id = f"fd-{tag}"
    if blockers is None:
        blockers = [{"code": "REVIEWED_BLUEPRINT_NOT_EXECUTABLE", "reason": "operand vanished"}]
    conn.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, "
        "definition_revision, formula_identity_hash, state, blockers, formula_content_hash, "
        "formula_json, requested_by, requested_at) "
        "VALUES (%s,%s,'opt-a','h','h','h','r',%s,%s,%s::jsonb,%s,%s::jsonb,'user:sam',"
        "'2026-08-23T00:00:00Z')",
        (draft_id, considered, f"ident-{draft_id}", state,
         json.dumps(blockers if state == "BLOCKED" else []),
         "sha256:f" if state == "READY" else None,
         '{"formula_schema_version": 3}' if state == "READY" else None))
    return draft_id


def _request(conn, tag: str, draft_id: str, **overrides):
    return request_method_override(
        conn,
        considered_revision_id=overrides.get("considered", f"crev-{tag}"),
        option_id=overrides.get("option_id", "opt-a"),
        refused_formula_draft_id=draft_id,
        actor_subject="user:sam",
        reason=overrides.get("reason", "the blueprint's operand was renamed upstream"),
        llm_spend_authorization_id=overrides.get("spend", _spend(conn, tag)),
        expires_at=overrides.get("expires_at", _FUTURE))


# ══ the verification IS the feature ═════════════════════════════════════════════════════════════
def test_A_VERIFIED_REFUSAL_RECORDS_AN_OVERRIDE_the_resolver_then_consumes(db):
    draft = _draft(db, "ok")
    override_id, created = _request(db, "ok", draft)

    assert created is True
    assert current_method_override(
        db, considered_revision_id="crev-ok", option_id="opt-a") == override_id

    # And the RESOLVER consumes it as evidence: LLM route, override named as a warning.
    from featuregen.overlay.upload.formula_strategy import (
        FormulaStrategy,
        FormulaStrategyFactsV1,
        resolve_formula_strategy,
    )

    decision = resolve_formula_strategy(FormulaStrategyFactsV1(
        candidate_origin="recipe", computation_kind="deterministic_formula",
        method_override_revision_id=override_id))
    assert decision.strategy is FormulaStrategy.LLM_AUTHORED
    assert "METHOD_OVERRIDDEN_TO_LLM" in decision.warnings


def test_an_identical_ask_is_ONE_override(db):
    draft = _draft(db, "idem")
    spend = _spend(db, "idem")
    first, created_first = _request(db, "idem", draft, spend=spend)
    second, created_second = _request(db, "idem", draft, spend=spend)
    assert first == second
    assert (created_first, created_second) == (True, False)


def test_a_refusal_that_did_not_happen_is_REFUSED_wrong_state(db):
    draft = _draft(db, "ready", state="READY")
    with pytest.raises(OverrideRefusalUnverified, match="not BLOCKED"):
        _request(db, "ready", draft)


def test_a_refusal_that_did_not_happen_is_REFUSED_wrong_code(db):
    draft = _draft(db, "code", blockers=[{"code": "SNAPSHOT_MISSING_REFS", "reason": "x"}])
    with pytest.raises(OverrideRefusalUnverified, match="deterministic instantiation"):
        _request(db, "code", draft)


def test_a_refusal_about_ANOTHER_candidate_authorizes_nothing_here(db):
    draft = _draft(db, "other")
    with pytest.raises(OverrideRefusalUnverified, match="another"):
        _request(db, "other", draft, option_id="opt-b")


def test_an_absent_draft_is_a_refusal_not_a_crash(db):
    seed_run_chain(db, run_id="mo-none", considered_revision_id="crev-none")
    with pytest.raises(OverrideRefusalUnverified, match="does not exist"):
        request_method_override(
            db, considered_revision_id="crev-none", option_id="opt-a",
            refused_formula_draft_id="fd-none", actor_subject="user:sam", reason="r",
            llm_spend_authorization_id="spend-none", expires_at=_FUTURE)


def test_no_live_ceiling_no_override(db):
    """§11.2: overriding to the LLM authorizes BUYING an answer."""
    draft = _draft(db, "nospend")
    with pytest.raises(OverrideRefusalUnverified, match="absent"):
        _request(db, "nospend", draft, spend="spend-that-does-not-exist")


# ══ expiry — a refusal AGES ═════════════════════════════════════════════════════════════════════
def test_an_EXPIRED_override_is_not_current(db):
    """The blueprint may have been fixed; the correct answer then is deterministic — which an
    unexpiring override would quietly override. Compared IN POSTGRES (the W1 lesson)."""
    draft = _draft(db, "exp")
    near = db.execute("SELECT (clock_timestamp() + interval '1 second')::text").fetchone()[0]
    override_id, _ = _request(db, "exp", draft, expires_at=near)
    assert current_method_override(
        db, considered_revision_id="crev-exp", option_id="opt-a") == override_id

    db.execute("SELECT pg_sleep(1.2)")
    assert current_method_override(
        db, considered_revision_id="crev-exp", option_id="opt-a") is None


def test_the_ASSEMBLER_resolves_the_override_itself(db, monkeypatch):
    """The grain_refs lesson: an input fact the assembler cannot see is one every caller has to
    remember to thread. Proved at the seam: `assemble_strategy_facts` with no override id folds
    the candidate's current one into the facts."""
    from featuregen.overlay.upload import formula_strategy_facts as fsf

    draft = _draft(db, "asm")
    override_id, _ = _request(db, "asm", draft)

    class _Idea:
        source_definition_id = ""
        generation_source = "user_defined"
        computation_kind = "deterministic_formula"
        definition = "d"

    assembled = fsf.assemble_strategy_facts(
        db, considered_revision_id="crev-asm", option_id="opt-a", idea=_Idea(),
        catalog_snapshot_hash="sha256:snap")
    assert assembled.facts.method_override_revision_id == override_id


def test_the_record_is_append_only_by_trigger(db):
    draft = _draft(db, "trig")
    override_id, _ = _request(db, "trig", draft)
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM formula_method_override_revision WHERE override_id = %s",
                   (override_id,))
