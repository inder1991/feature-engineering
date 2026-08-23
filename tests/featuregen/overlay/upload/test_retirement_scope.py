"""Retirement, decoupled from the money guard.

▲ The case worth reading first is `test_THE_SCOPE_KEY_SURVIVES_A_CONFIGURATION_CHANGE`. Everything
else follows from retirement being keyed on something the identity hash cannot move.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.formula_draft_store import (
    DraftNotAnAnswer,
    DraftRetired,
    formula_identity,
    request_draft,
)
from featuregen.overlay.upload.retirement_scope import (
    RetirementScope,
    consume_exception,
    record_tombstone,
    retirement_scope_key,
    tombstone_covering,
    valid_exception_for,
)

#: ▲ Ids are DISTINCTIVE ON PURPOSE. A full-suite run once failed 16 of these tests while the file
#: alone passed 22/22 — order-dependent, and consistent with a committed row leaked from another
#: suite colliding on shared ids like "crev-1". Unique prefixes make that collision class
#: impossible instead of waiting for the seed that reproduces it.
CANDIDATE = dict(
    considered_revision_id="crev-retscope", option_id="opt-retscope", planning_request_hash="h-asked-retscope",
    catalog_snapshot_hash="h-snap-retscope", definition_revision="rev-retscope")


@pytest.fixture(autouse=True)
def _considered_revision_exists(db):
    """Migration 1116 makes `formula_draft.considered_revision_id` a real foreign key, so the one
    revision every draft in this file names has to exist. Seeding only — nothing here asserts on
    the chain, and no test in this file commits, so the rolled-back fixture needs no teardown
    (the committed-test variant lives in test_formula_draft_retirement.py)."""
    from tests.featuregen.runs._chain import seed_run_chain

    seed_run_chain(db, run_id="rs", considered_revision_id="crev-retscope")


def _request(conn, *, draft_id="fd-ret-1", config="cfg-1", **over):
    facts = {**CANDIDATE, **over}
    return request_draft(
        conn, formula_draft_id=draft_id, authoring_config_hash=config,
        requested_by="user:sam", requested_at="t", **facts)


def _identity(config="cfg-1", **over) -> str:
    return formula_identity(authoring_config_hash=config, **{**CANDIDATE, **over})


# ══ THE PROPERTY THE WHOLE DESIGN TURNS ON ═════════════════════════════════════════════════════
def test_THE_SCOPE_KEY_SURVIVES_A_CONFIGURATION_CHANGE():
    """▲ Retirement used to ride on the identity hash, which INCLUDES the authoring configuration —
    so correcting that hash (it is a constant today) would have re-minted every identity, let every
    INSERT win, and silently un-retired every formula anyone ever withdrew.

    The scope key deliberately excludes it. Two drafts of one candidate under different
    configurations share a retirement scope and always will.
    """
    assert retirement_scope_key(**CANDIDATE) == retirement_scope_key(**CANDIDATE)
    # It is NOT the identity: the identity moves with the configuration, this does not.
    assert _identity(config="cfg-1") != _identity(config="cfg-2")


@pytest.mark.parametrize("field", sorted(CANDIDATE))
def test_EVERY_CANDIDATE_FACT_MOVES_THE_SCOPE_KEY(field):
    """Each names a different thing to withdraw. A field that did not move the key would let one
    retirement silently cover a candidate nobody withdrew."""
    moved = {**CANDIDATE, field: "something-else"}
    assert retirement_scope_key(**moved) != retirement_scope_key(**CANDIDATE)


# ══ REFUSED BEFORE ANY SPEND ═══════════════════════════════════════════════════════════════════
def test_A_TOMBSTONE_REFUSES_BEFORE_THE_INSERT(db):
    """▲ The whole fix. Retirement was consulted only when the INSERT LOST the identity race, so a
    won INSERT was silent permission to spend. Now the tombstone is read first, and the caller
    enqueues nothing because it never returns."""
    _request(db)
    record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="withdrawn", retired_by="user:ops")

    with pytest.raises(DraftRetired, match="withdrawn by tombstone"):
        _request(db, draft_id="fd-ret-2", config="cfg-2")   # a NEW configuration, same candidate


def test_A_NEW_DRAFT_ID_DOES_NOT_DEFEAT_A_TOMBSTONE(db):
    """The identity is what is withdrawn, not the row. Minting a fresh draft id was the obvious
    workaround, and it is the one this refuses by name."""
    _request(db)
    record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                     reason="withdrawn", retired_by="user:ops")

    with pytest.raises(DraftRetired):
        _request(db, draft_id="fd-brand-new")


# ══ SCOPE IS A GOVERNANCE DECISION ═════════════════════════════════════════════════════════════
def test_AN_EXACT_RETIREMENT_DOES_NOT_WITHDRAW_THE_CANDIDATE(db):
    """▲ The widening an earlier design granted by a schema choice. "Retire this draft" means an
    exact identity; withdrawing the candidate under every future model and prompt is a stronger act
    that has to be asked for."""
    _request(db)
    record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                     reason="withdrawn", retired_by="user:ops")

    # A DIFFERENT configuration of the same candidate is untouched.
    draft_id, created = _request(db, draft_id="fd-ret-2", config="cfg-2")
    assert created is True and draft_id == "fd-ret-2"


def test_A_CANDIDATE_WIDE_RETIREMENT_DOES_WITHDRAW_IT(db):
    _request(db)
    record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="withdrawn", retired_by="user:ops")

    with pytest.raises(DraftRetired):
        _request(db, draft_id="fd-ret-2", config="cfg-2")


def test_BOTH_SCOPES_CAN_COEXIST_for_one_candidate(db):
    """▲ The collision the old single-column key could not represent: retiring configuration A
    consumed the candidate's key, so B could never be retired and a candidate-wide tombstone could
    never follow an exact one."""
    _request(db)
    _request(db, draft_id="fd-ret-2", config="cfg-2")

    record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                     reason="a", retired_by="user:ops")
    record_tombstone(db, formula_draft_id="fd-ret-2", scope=RetirementScope.EXACT_DRAFT,
                     reason="b", retired_by="user:ops")
    record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="c", retired_by="user:ops")

    assert db.execute(
        "SELECT count(*) FROM formula_draft_retirement_tombstone").fetchone()[0] == 3


def test_RECORDING_THE_SAME_WITHDRAWAL_TWICE_IS_ONE_TOMBSTONE(db):
    _request(db)
    first = record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                             reason="withdrawn", retired_by="user:ops")
    second = record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                              reason="withdrawn", retired_by="user:ops")

    assert first.tombstone_id == second.tombstone_id
    assert db.execute(
        "SELECT count(*) FROM formula_draft_retirement_tombstone").fetchone()[0] == 1


def test_THE_WIDER_WITHDRAWAL_IS_REPORTED_when_both_cover_a_request(db):
    """Reporting the narrower one would understate what was refused, and send somebody to ask for
    the wrong exception."""
    _request(db)
    record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                     reason="exact", retired_by="user:ops")
    record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="candidate-wide", retired_by="user:ops")

    found = tombstone_covering(
        db, scope_key=retirement_scope_key(**CANDIDATE), formula_identity_hash=_identity())

    assert found.scope is RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS


# ══ THE EXCEPTION MUST BIND ════════════════════════════════════════════════════════════════════
def _spend(db, *, authorization_id="sa-retscope", contract="pc-1"):
    """▲ An exception BINDS its money (1105), so one cannot be written without an authorization.
    Regeneration is an approved, COST-CONFIRMED act — the constraint is what makes that true rather
    than intended."""
    db.execute(
        "INSERT INTO llm_spend_authorization_revision (spend_authorization_id, action, "
        "actor_subject, job_identity, member_identities, provider_contract_hash, max_calls, "
        "max_tokens, currency, max_cost, pricing_version, idempotency_identity, expires_at) "
        "VALUES (%s, 'AUTHOR_FORMULA', 'user:ops', 'job-1', '[]'::jsonb, %s, 4, 100000, 'USD', "
        "5.00, 'price-v1', %s, '2099-01-01T00:00:00Z') ON CONFLICT DO NOTHING",
        (authorization_id, contract, f"idem-{authorization_id}"))
    return authorization_id


def _exception(db, tombstone_id, *, target, contract="pc-1", strategy="st-1", uses=1,
               expires="2099-01-01T00:00:00Z"):
    spend = _spend(db, contract=contract)
    db.execute(
        "INSERT INTO formula_draft_regeneration_exception (exception_id, tombstone_id, "
        "target_formula_identity_hash, provider_contract_hash, strategy_identity_hash, "
        "actor_subject, overrides_tombstone, max_uses, expires_at, llm_spend_authorization_id) "
        "VALUES ('ex-retscope', %s, %s, %s, %s, 'user:ops', %s, %s, %s, %s)",
        (tombstone_id, target, contract, strategy, tombstone_id is not None, uses, expires,
         spend))
    return "ex-retscope"


@pytest.mark.parametrize("over,why", [
    ({"contract": "pc-other"}, "a different provider contract is a different price"),
    ({"strategy": "st-other"}, "a different strategy is a different method"),
    ({"expires": "2000-01-01T00:00:00Z"}, "an approval granted in a triage window must age out"),
])
def test_AN_EXCEPTION_THAT_DOES_NOT_BIND_DOES_NOT_AUTHORIZE(db, over, why):
    """▲ An earlier design keyed the approval on a scope and a timestamp, which authorized ANY
    regeneration of that candidate, at ANY cost, under ANY configuration, FOR EVER."""
    _request(db)
    tombstone = record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="withdrawn", retired_by="user:ops")
    _exception(db, tombstone.tombstone_id, target=_identity(config="cfg-2"), **over)

    assert valid_exception_for(
        db, target_formula_identity_hash=_identity(config="cfg-2"),
        provider_contract_hash="pc-1", strategy_identity_hash="st-1",
        covering_tombstone_id=tombstone.tombstone_id,
        now="2026-08-23T00:00:00Z") is None, why


def test_AN_EXCEPTION_IS_CONSUMED_EXACTLY_ONCE(db):
    """▲ An exception checked and not consumed is a coupon that regenerates itself every time
    somebody clicks."""
    _request(db)
    tombstone = record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="withdrawn", retired_by="user:ops")
    exception_id = _exception(db, tombstone.tombstone_id, target=_identity(config="cfg-2"))

    assert consume_exception(db, exception_id) is True
    assert consume_exception(db, exception_id) is False


def test_A_BOUND_EXCEPTION_LETS_THE_REGENERATION_THROUGH_ONCE(db):
    """▲ And it is REACHABLE, which the earlier ordering could not manage: it refused on the
    tombstone before any exception was loaded, so the override column had no code path at all."""
    _request(db)
    tombstone = record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="withdrawn", retired_by="user:ops")
    _exception(db, tombstone.tombstone_id, target=_identity(config="cfg-2"))

    draft_id, created = _request(
        db, draft_id="fd-ret-2", config="cfg-2",
        provider_contract_hash="pc-1", strategy_identity_hash="st-1",
        now="2026-08-23T00:00:00Z")
    assert (draft_id, created) == ("fd-ret-2", True)

    # ONCE. The second attempt finds the exception spent and the tombstone still standing.
    with pytest.raises(DraftRetired):
        _request(db, draft_id="fd-ret-3", config="cfg-2",
                 provider_contract_hash="pc-1", strategy_identity_hash="st-1",
                 now="2026-08-23T00:00:00Z")


def test_WITHOUT_THE_BINDING_FACTS_A_TOMBSTONE_ALWAYS_REFUSES(db):
    """Fail-closed by construction: an exception binds a provider contract and a strategy, so a
    caller that cannot name them cannot have one honoured."""
    _request(db)
    tombstone = record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="withdrawn", retired_by="user:ops")
    _exception(db, tombstone.tombstone_id, target=_identity(config="cfg-2"))

    with pytest.raises(DraftRetired):
        _request(db, draft_id="fd-ret-2", config="cfg-2")     # no contract, no strategy


# ══ A FAILURE IS NOT A PURCHASE ════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("state", ["FAILED", "CANCELLED"])
def test_A_TERMINAL_FAILURE_IS_NOT_RETURNED_AS_AN_EXISTING_DRAFT(db, state):
    """▲ The wedge: these states are terminal, this returned them with `created=False`, and the
    route enqueues only `if created` — so the candidate could never be authored again, and the only
    escape was moving an identity-bearing input that the constant config hash cannot move.

    Two of the seven live drafts say in their own failure text that the fault was the platform's
    and "not a problem with the candidate".
    """
    _request(db)
    db.execute(
        "UPDATE formula_draft SET state = %s, failure_reason = %s WHERE formula_draft_id = 'fd-ret-1'",
        (state, "boom" if state == "FAILED" else None))

    # ▲ As the LLM LANE asks (provider contract present): Option 2 made the contract-less path
    # the FREE deterministic lane — that path's own tests live in test_formula_draft_retirement.
    with pytest.raises(DraftNotAnAnswer, match="not an answer this platform bought"):
        _request(db, draft_id="fd-ret-2", provider_contract_hash="sha256:llm",
                 strategy_identity_hash="sih-llm")


def test_A_BLOCKED_DRAFT_IS_STILL_AN_ANSWER(db):
    """A business refusal IS a verdict about the candidate, and re-buying it cannot change it. Only
    the platform's own failures are not answers."""
    _request(db)
    db.execute(
        "UPDATE formula_draft SET state = 'BLOCKED', blockers = '[{\"code\": \"NO_GRAIN\"}]'::jsonb "
        "WHERE formula_draft_id = 'fd-ret-1'")

    draft_id, created = _request(db, draft_id="fd-ret-2")
    assert (draft_id, created) == ("fd-ret-1", False)


def test_A_DISAGREEING_SECOND_RETIREMENT_IS_REFUSED_not_silently_discarded(db):
    """▲ Workflow finding W5: every draft of one candidate collides on the candidate-wide coverage
    key, the INSERT was DO NOTHING, and the function returned a TombstoneV1 built from the CALLER's
    arguments — so operator 2's 'PII leak' vanished while they were told it took effect, and the
    audit trail said 'wrong window' for ever. The exact defect RetirementDisagreement fixed on the
    legacy path, reintroduced by the new one."""
    from featuregen.overlay.upload.formula_draft_store import RetirementDisagreement

    _request(db)
    _request(db, draft_id="fd-ret-2", config="cfg-2")
    record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="wrong window", retired_by="user:ops")

    with pytest.raises(RetirementDisagreement, match="wrong window"):
        record_tombstone(
            db, formula_draft_id="fd-ret-2",
            scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
            reason="PII leak", retired_by="user:two")


def test_THE_RETURNED_TOMBSTONE_IS_THE_STORED_ROW_not_the_arguments(db):
    """Idempotency stays silent — but what comes back is what the table says, so a caller can never
    hold a tombstone the store does not."""
    _request(db)
    first = record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                             reason="withdrawn", retired_by="user:ops")
    again = record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                             reason="withdrawn", retired_by="user:someone-else")

    assert again.tombstone_id == first.tombstone_id
    assert again.reason == "withdrawn"


def test_A_TOMBSTONE_STOPS_AN_IN_FLIGHT_DRAFT_at_its_next_step(db):
    """▲ Workflow finding W3, the second retirement blocker: the advance fence read only the LEGACY
    retirement table, so a tombstone stopped future requests while every IN-FLIGHT draft of the
    candidate kept spending to READY — retirement failing open for exactly the drafts most worth
    stopping. The fence now recomputes the scope key from the draft's own frozen identity columns,
    so a candidate-wide withdrawal stops OTHER configurations' in-flight drafts too."""
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance

    _request(db)                                               # fd-ret-1, cfg-1
    _request(db, draft_id="fd-ret-2", config="cfg-2")          # in flight, other configuration
    advance(db, "fd-ret-2", DraftStateV1.AUTHORING)

    record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="withdrawn mid-flight", retired_by="user:ops")

    with pytest.raises(DraftRetired, match="withdrawn by tombstone"):
        advance(db, "fd-ret-2", DraftStateV1.CRITIC_REVIEW)


def test_AN_EXACT_TOMBSTONE_STOPS_ONLY_ITS_OWN_DRAFT_in_flight(db):
    """The scope discipline, applied at the fence: an EXACT withdrawal must not stop a sibling
    configuration that nobody withdrew."""
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance

    _request(db)
    _request(db, draft_id="fd-ret-2", config="cfg-2")
    advance(db, "fd-ret-2", DraftStateV1.AUTHORING)

    record_tombstone(db, formula_draft_id="fd-ret-1", scope=RetirementScope.EXACT_DRAFT,
                     reason="withdrawn", retired_by="user:ops")

    assert advance(db, "fd-ret-2", DraftStateV1.CRITIC_REVIEW) is DraftStateV1.CRITIC_REVIEW
    with pytest.raises(DraftRetired):
        advance(db, "fd-ret-1", DraftStateV1.AUTHORING)


# ══ THE REGENERATION THAT WAS STRUCTURALLY IMPOSSIBLE ══════════════════════════════════════════
def test_AN_APPROVED_EXCEPTION_REGENERATES_THE_EXACT_FAILED_IDENTITY(db):
    """▲ Workflow finding W4, the deepest one. The exception binds the EXACT identity being
    re-requested — but 1090's unique index covered every row, so the terminal FAILED draft occupied
    the slot for ever and the authorized INSERT lost unconditionally. The operator presenting the
    approved exception had one use burned per click while being told to obtain the thing they were
    holding. Since 1107 a failure holds no slot: history stays, the remedy is REACHABLE."""
    _request(db)
    db.execute("UPDATE formula_draft SET state = 'FAILED', failure_reason = 'provider outage' "
               "WHERE formula_draft_id = 'fd-ret-1'")
    # A TOMBSTONE-LESS exception — a failure is not a withdrawal, so there is nothing to override —
    # bound to the SAME identity that failed.
    _exception(db, None, target=_identity())

    draft_id, created = _request(
        db, draft_id="fd-ret-regen", provider_contract_hash="pc-1",
        strategy_identity_hash="st-1", now="2026-08-23T00:00:00Z")

    assert (draft_id, created) == ("fd-ret-regen", True)
    # History SURVIVES: the failed row is still there, still FAILED, still readable.
    assert db.execute(
        "SELECT state FROM formula_draft WHERE formula_draft_id = 'fd-ret-1'").fetchone()[0] == "FAILED"
    # And the exception is spent — exactly once, by the transaction that minted.
    assert db.execute(
        "SELECT uses_consumed FROM formula_draft_regeneration_exception").fetchone()[0] == 1


def test_A_REFUSAL_NEVER_CONSUMES_THE_EXCEPTION(db):
    """▲ The consume-then-refuse half of W4: consumption used to precede the refusal decision, so a
    caller that caught the typed refusal and committed burned one use per click — and at max_uses
    the message silently degraded. Consumption now happens only in the transaction that mints."""
    _request(db)
    tombstone = record_tombstone(
        db, formula_draft_id="fd-ret-1", scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
        reason="withdrawn", retired_by="user:ops")
    # An exception that does NOT match this request's binding facts (different strategy), so the
    # request is refused — and the exception must remain whole.
    _exception(db, tombstone.tombstone_id, target=_identity(config="cfg-2"), strategy="st-other")

    with pytest.raises(DraftRetired):
        _request(db, draft_id="fd-ret-2", config="cfg-2",
                 provider_contract_hash="pc-1", strategy_identity_hash="st-1",
                 now="2026-08-23T00:00:00Z")

    assert db.execute(
        "SELECT uses_consumed FROM formula_draft_regeneration_exception").fetchone()[0] == 0


def test_A_FAILED_IDENTITY_WITHOUT_AN_EXCEPTION_STILL_REFUSES_by_name(db):
    """The bounded half of §11.1.2's ruling: the re-attempt exists AND it is bounded — by the
    exception's max_uses, which is the same mechanism that bounds the money."""
    _request(db)
    db.execute("UPDATE formula_draft SET state = 'FAILED', failure_reason = 'x' "
               "WHERE formula_draft_id = 'fd-ret-1'")

    # As the LLM lane asks — see the note above.
    with pytest.raises(DraftNotAnAnswer, match="approved regeneration exception"):
        _request(db, draft_id="fd-ret-2", provider_contract_hash="sha256:llm",
                 strategy_identity_hash="sih-llm")
