"""The exact identity of the method that authored a member — derived from the run, never from now.

▲ The case worth reading first is `test_THE_IDENTITY_COMES_FROM_THE_RUN_not_from_what_is_deployed`.
Everything else guards a way of getting that wrong.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.authoring_provenance import (
    LLM_AUTHORED,
    REVIEWED_RECIPE_BLUEPRINT,
)
from featuregen.materialize.method_identity import (
    METHOD_IDENTITY_VERSION,
    MethodIdentityUndecidable,
    derive_method_identity,
)

RUN = "far-mi"


def _run(conn, run_id=RUN, *, schema=3, grammar=1) -> str:
    conn.execute(
        "INSERT INTO formula_authoring_run (authoring_run_id, intent_hash, versions, actor) "
        "VALUES (%s, 'ih', %s::jsonb, '{\"subject\": \"user:sam\"}'::jsonb) "
        "ON CONFLICT DO NOTHING",
        (run_id, '{"formula_schema": %d, "operation_grammar": %d, "disposition": 2, "critic": 1}'
         % (schema, grammar)))
    return run_id


def _dispatch(conn, run_id, role, *, ref, provider="anthropic", model="claude-opus-4-8",
              contract="c-1", prompt="p-1", schema="s-1") -> None:
    conn.execute(
        "INSERT INTO llm_dispatch (dispatch_ref, logical_call_ref, attempt_no, stage, task, "
        "input_hash, redacted_input, provider, model, authoring_run_id, call_role, "
        "provider_contract_hash, prompt_content_hash, schema_content_hash) "
        "VALUES (%s, %s, 1, 'formula', 'author', 'ih', '{}'::jsonb, %s, %s, %s, %s, %s, %s, %s)",
        (ref, f"lcr-{ref}", provider, model, run_id, role, contract, prompt, schema))


def _bypass(conn, run_id, *, revision="recipe:posted_debit_amount", expectation="bp-hash") -> None:
    conn.execute(
        "INSERT INTO formula_authoring_trace_event (authoring_run_id, seq, idempotency_key, "
        "stage, kind, payload) VALUES (%s, 1, %s, 'REVIEW_BYPASSED', 'review_bypassed', %s::jsonb)",
        (run_id, f"{run_id}:review-bypassed",
         '{"blueprint_revision": "%s", "expectation_hash": "%s"}' % (revision, expectation)))


def _llm(conn, run_id=RUN, **over):
    _run(conn, run_id)
    _dispatch(conn, run_id, "formula.author", ref=f"d-a-{run_id}", **over)
    _dispatch(conn, run_id, "formula.critic", ref=f"d-c-{run_id}", **over)
    return run_id


# ══ THE RULE ═══════════════════════════════════════════════════════════════════════════════════
def test_THE_IDENTITY_COMES_FROM_THE_RUN_not_from_what_is_deployed(db):
    """▲ The shortcut this module exists to refuse: reading the CURRENT generation settings at
    sealing time would record today's configuration against a run authored under a different one —
    1099's backfill error moved earlier. Everything here is read from the run's own dispatches."""
    _llm(db, model="claude-opus-4-8", contract="contract-of-the-run")

    identity = derive_method_identity(db, authoring_run_id=RUN, authoring_method=LLM_AUTHORED)

    assert identity.payload["model"] == "claude-opus-4-8"
    assert identity.payload["author_contract_hash"] == "contract-of-the-run"
    assert identity.payload["identity_version"] == METHOD_IDENTITY_VERSION


def test_THE_VERSION_AXIS_COMES_FROM_THE_RUN_TOO(db):
    """A certificate covers a grammar and a schema. Reading the deployed constants would describe
    the platform now rather than the run then."""
    _llm(db)

    identity = derive_method_identity(db, authoring_run_id=RUN, authoring_method=LLM_AUTHORED)

    assert identity.payload["formula_schema_version"] == 3
    assert identity.payload["operation_grammar_version"] == 1


def test_A_DIFFERENT_MODEL_IS_A_DIFFERENT_IDENTITY(db):
    """▲ The property the whole table exists for. Two drafts authored under different models are not
    interchangeable, and a certificate issued for one must not match the other."""
    _llm(db, "far-a", model="claude-opus-4-8")
    _llm(db, "far-b", model="claude-haiku-4-5")

    first = derive_method_identity(db, authoring_run_id="far-a", authoring_method=LLM_AUTHORED)
    second = derive_method_identity(db, authoring_run_id="far-b", authoring_method=LLM_AUTHORED)

    assert first.method_identity_hash != second.method_identity_hash


def test_A_DIFFERENT_CONTRACT_IS_A_DIFFERENT_IDENTITY(db):
    """The prompt and schema live inside the provider contract, so a prompt edit moves this hash —
    which is correct: the method changed, whatever the model says."""
    _llm(db, "far-a", contract="c-1")
    _llm(db, "far-b", contract="c-2")

    assert (derive_method_identity(db, authoring_run_id="far-a", authoring_method=LLM_AUTHORED)
            .method_identity_hash
            != derive_method_identity(db, authoring_run_id="far-b", authoring_method=LLM_AUTHORED)
            .method_identity_hash)


# ══ REFUSALS — never a default ═════════════════════════════════════════════════════════════════
def test_A_RUN_WITH_NO_CRITIC_DISPATCHES_IS_REFUSED(db):
    """▲ Not hypothetical: on the live cluster every authoring run has author dispatches and NO
    critic ones, because all seven drafts died before the critic. An identity built from one role
    would describe half the method a certificate has to cover."""
    _run(db)
    _dispatch(db, RUN, "formula.author", ref="d-a-only")

    with pytest.raises(MethodIdentityUndecidable, match="no formula.critic dispatches"):
        derive_method_identity(db, authoring_run_id=RUN, authoring_method=LLM_AUTHORED)


def test_A_RUN_THAT_USED_TWO_MODELS_IS_REFUSED(db):
    """One run, one method identity. Picking either would mint an identity a certificate could match
    while half the work was done by something else."""
    _run(db)
    _dispatch(db, RUN, "formula.author", ref="d-a", model="claude-opus-4-8")
    _dispatch(db, RUN, "formula.critic", ref="d-c", model="claude-haiku-4-5")

    with pytest.raises(MethodIdentityUndecidable, match="2 different values for model"):
        derive_method_identity(db, authoring_run_id=RUN, authoring_method=LLM_AUTHORED)


def test_A_RUN_WITH_NO_VERSION_VECTOR_IS_REFUSED(db):
    """An identity that omits the grammar would match a certificate issued for a different one."""
    db.execute(
        "INSERT INTO formula_authoring_run (authoring_run_id, intent_hash, versions, actor) "
        "VALUES ('far-noversions', 'ih', '{}'::jsonb, '{\"subject\": \"user:sam\"}'::jsonb)")
    _dispatch(db, "far-noversions", "formula.author", ref="d-a-nv")
    _dispatch(db, "far-noversions", "formula.critic", ref="d-c-nv")

    with pytest.raises(MethodIdentityUndecidable, match="formula_schema, operation_grammar"):
        derive_method_identity(
            db, authoring_run_id="far-noversions", authoring_method=LLM_AUTHORED)


def test_AN_UNKNOWN_METHOD_IS_REFUSED(db):
    _llm(db)
    with pytest.raises(MethodIdentityUndecidable, match="unknown authoring method"):
        derive_method_identity(db, authoring_run_id=RUN, authoring_method="VIBES")


# ══ THE REVIEWED-BLUEPRINT HALF ════════════════════════════════════════════════════════════════
def test_A_BYPASS_NAMES_THE_BLUEPRINT_IT_STOOD_ON(db):
    _run(db)
    _bypass(db, RUN)

    identity = derive_method_identity(
        db, authoring_run_id=RUN, authoring_method=REVIEWED_RECIPE_BLUEPRINT)

    assert identity.payload["blueprint_revision"] == "recipe:posted_debit_amount"
    assert identity.payload["expectation_hash"] == "bp-hash"
    assert identity.payload["method"] == REVIEWED_RECIPE_BLUEPRINT


def test_A_DIFFERENT_BLUEPRINT_REVISION_IS_A_DIFFERENT_IDENTITY(db):
    _run(db, "far-x")
    _run(db, "far-y")
    _bypass(db, "far-x", revision="recipe:a")
    _bypass(db, "far-y", revision="recipe:b")

    assert (derive_method_identity(db, authoring_run_id="far-x",
                                   authoring_method=REVIEWED_RECIPE_BLUEPRINT).method_identity_hash
            != derive_method_identity(db, authoring_run_id="far-y",
                                      authoring_method=REVIEWED_RECIPE_BLUEPRINT)
            .method_identity_hash)


def test_A_REVIEWED_RUN_WITH_NO_BYPASS_PAYLOAD_IS_REFUSED(db):
    _run(db)
    with pytest.raises(MethodIdentityUndecidable, match="no REVIEW_BYPASSED payload"):
        derive_method_identity(
            db, authoring_run_id=RUN, authoring_method=REVIEWED_RECIPE_BLUEPRINT)


def test_EXPECTATION_GENERATION_IS_DELIBERATELY_ABSENT(db):
    """▲ Parent §10 lists it and it is load-bearing — two of the three reviewed recipes are Formula
    V1, so "a reviewed expectation exists" does not identify a V3-producible method.

    It is NOT here, because the trace proves only the blueprint revision and the expectation hash.
    Deriving the generation from today's registry would assert a fact about the run from a source
    that moves independently of it — the exact thing this module refuses. It joins when 1104 records
    it as RESOLVED AT AUTHORING TIME, and METHOD_IDENTITY_VERSION bumps with it.
    """
    _run(db)
    _bypass(db, RUN)

    identity = derive_method_identity(
        db, authoring_run_id=RUN, authoring_method=REVIEWED_RECIPE_BLUEPRINT)

    assert "expectation_generation" not in identity.payload
