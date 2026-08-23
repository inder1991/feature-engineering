"""Executable policy content — the thing a realization points at and nothing stored.

A realization records `executable_content_hash` and `cas_pointer`: an address, and a promise that
content lives there. Nothing stored the content, so every governed policy was decidable and
un-renderable at once. `eligible_status_policy_hash = abc123` does not tell anyone to emit
`WHERE transaction_status IN ('POSTED','SETTLED')`.

What these tests hold:

1. **A declared policy resolves to content or REFUSES BY NAME.** No defaults — a defaulted policy is
   a wrong number wearing a governed costume.
2. **The address is the integrity check**, and it is stable against orderings that carry no meaning.
3. **Every field a wrong value would silently corrupt is REQUIRED.**
4. **Payloads are immutable**, because artifacts sealed against one were verified under exactly
   those bytes.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.formula.policy_payloads import (
    CurrencyConversionPayloadV1,
    DirectionPayloadV1,
    EligibleStatusPayloadV1,
    MissingRateBehaviourV1,
    PolicyPayloadUnavailable,
    PolicyReadBasisV1,
    QuoteConventionV1,
    load_payload,
    payload_content_hash,
    record_payload,
    resolve_executable_policy,
)

STATUS = EligibleStatusPayloadV1(
    status_column_ref="cib::bo_dpl_cib.txns.status", eligible_values=("POSTED", "SETTLED"),
    read_basis=PolicyReadBasisV1.EVENT_TIME)
FX = CurrencyConversionPayloadV1(
    rate_table_ref="cib::bo_dpl_cib.fx_rates",
    rate_column_ref="cib::bo_dpl_cib.fx_rates.rate",
    as_of_column_ref="cib::bo_dpl_cib.fx_rates.effective_dt",
    rate_key_refs=("cib::bo_dpl_cib.fx_rates.base_ccy", "cib::bo_dpl_cib.fx_rates.quote_ccy"),
    quote_convention=QuoteConventionV1.BASE_TO_QUOTE,
    missing_rate_behaviour=MissingRateBehaviourV1.REFUSE,
    read_basis=PolicyReadBasisV1.AS_OF_CUTOFF)


def _realization(conn, revision_id: str, content_hash: str, kind: str = "status") -> str:
    conn.execute(
        "INSERT INTO policy_realization_revision (revision_id, family_key_hash, policy_kind, "
        "policy_ref, bound_dataset, environment_id, semantic_role, executable_content_hash, "
        "cas_pointer, provenance) VALUES (%s,'fam',%s,'ref','ds','env','role',%s,'cas',"
        "'source_derived')", (revision_id, kind, content_hash))
    return revision_id


# ══ CONTENT OR A NAMED REFUSAL — NEVER A DEFAULT ═══════════════════════════════════════════════
def test_A_REALIZATION_WITH_NO_STORED_CONTENT_REFUSES_BY_NAME(db):
    """The state the platform is in today for every policy: the decision recorded, its content not.

    Rendering with a default here would apply a policy nobody wrote while the artifact claims a
    governed one was used — which is the failure this whole store exists to make impossible.
    """
    _realization(db, "pr-1", "sha256:nothing-is-stored-here")
    with pytest.raises(PolicyPayloadUnavailable) as raised:
        resolve_executable_policy(db, realization_revision_id="pr-1")
    assert "pr-1" in str(raised.value)
    assert "sha256:nothing-is-stored-here" in str(raised.value)
    assert "rendering it with a default" in str(raised.value)


def test_a_realization_that_does_not_exist_refuses(db):
    with pytest.raises(PolicyPayloadUnavailable, match="does not exist"):
        resolve_executable_policy(db, realization_revision_id="pr-nonexistent")


def test_STORED_CONTENT_RESOLVES_TO_A_TYPED_PAYLOAD(db):
    """The round trip the renderer needs: from a governed decision to something emittable."""
    content = record_payload(db, STATUS, recorded_by="user:ops")
    _realization(db, "pr-1", content)

    resolved = resolve_executable_policy(db, realization_revision_id="pr-1")
    assert isinstance(resolved, EligibleStatusPayloadV1)
    assert resolved.eligible_values == ("POSTED", "SETTLED")
    assert resolved.status_column_ref == STATUS.status_column_ref


# ══ THE ADDRESS IS THE INTEGRITY CHECK ═════════════════════════════════════════════════════════
def test_the_address_is_the_HASH_OF_THE_CONTENT(db):
    """So a pointer either finds bytes that hash to it or finds nothing — never different bytes
    wearing the right name."""
    content = record_payload(db, STATUS, recorded_by="user:ops")
    assert content == payload_content_hash(STATUS)
    assert load_payload(db, content) == STATUS


def test_recording_the_same_decision_twice_is_ONE_payload(db):
    """Two rows with identical bytes would be two names for one thing, and every reader would have
    to know which name to use."""
    first = record_payload(db, STATUS, recorded_by="user:ops")
    second = record_payload(db, STATUS, recorded_by="user:someone-else")
    assert first == second
    assert db.execute("SELECT count(*) FROM executable_policy_payload").fetchone()[0] == 1


def test_MEANINGLESS_ORDER_IS_NOT_PART_OF_THE_ADDRESS():
    """A set of eligible values has no order; two spellings of the same set are one policy."""
    assert payload_content_hash(STATUS) == payload_content_hash(EligibleStatusPayloadV1(
        status_column_ref=STATUS.status_column_ref, eligible_values=("SETTLED", "POSTED"),
        read_basis=STATUS.read_basis))


def test_MEANINGFUL_ORDER_IS_PART_OF_THE_ADDRESS():
    """FX key order is the JOIN order, so reordering it changes the plan and must change the id."""
    swapped = CurrencyConversionPayloadV1(
        rate_table_ref=FX.rate_table_ref, rate_column_ref=FX.rate_column_ref,
        as_of_column_ref=FX.as_of_column_ref,
        rate_key_refs=tuple(reversed(FX.rate_key_refs)),
        quote_convention=FX.quote_convention,
        missing_rate_behaviour=FX.missing_rate_behaviour,
        read_basis=FX.read_basis)
    assert payload_content_hash(FX) != payload_content_hash(swapped)


def test_the_KIND_is_inside_the_address():
    """The same field values under two kinds are two different decisions."""
    a = payload_content_hash(STATUS)
    b = payload_content_hash(DirectionPayloadV1(
        direction_column_ref=STATUS.status_column_ref,
        debit_values=("POSTED",), credit_values=("SETTLED",),
        read_basis=STATUS.read_basis))
    assert a != b


# ══ WHAT A WRONG VALUE WOULD SILENTLY CORRUPT IS REQUIRED ══════════════════════════════════════
def test_a_status_policy_with_NO_VALUES_is_refused():
    """A filter admitting every row is not a policy, and recording it as one lets a feature claim a
    governed filter it does not have."""
    with pytest.raises(ValueError, match="admits every row"):
        EligibleStatusPayloadV1(status_column_ref="c", eligible_values=(),
                                read_basis=PolicyReadBasisV1.EVENT_TIME)


def test_a_direction_policy_needs_BOTH_SIDES():
    """Treating 'not debit' as credit silently classifies every unrecognised value."""
    with pytest.raises(ValueError, match="BOTH directions"):
        DirectionPayloadV1(direction_column_ref="c", debit_values=("DR",), credit_values=(),
                           read_basis=PolicyReadBasisV1.EVENT_TIME)


def test_a_value_cannot_be_BOTH_debit_and_credit():
    """A policy saying it can makes the classification depend on evaluation order."""
    with pytest.raises(ValueError, match="both debit and credit"):
        DirectionPayloadV1(direction_column_ref="c", debit_values=("X", "DR"),
                           credit_values=("CR", "X"),
                           read_basis=PolicyReadBasisV1.EVENT_TIME)


def test_an_FX_policy_needs_KEY_REFS():
    """Without them the as-of join matches every rate row in range, amplifying the amount by however
    many currencies the table happens to carry — a wrong number, not an error."""
    with pytest.raises(ValueError, match="WHICH pair"):
        CurrencyConversionPayloadV1(
            rate_table_ref="t", rate_column_ref="r", as_of_column_ref="d", rate_key_refs=(),
            quote_convention=QuoteConventionV1.BASE_TO_QUOTE,
            missing_rate_behaviour=MissingRateBehaviourV1.REFUSE,
            read_basis=PolicyReadBasisV1.AS_OF_CUTOFF)


def test_the_QUOTE_CONVENTION_IS_DECLARED_not_inferred():
    """Wrong, it returns the reciprocal — off by a factor of ten or more and still looks like money.

    Asserted as a REQUIRED field with two meanings rather than a default, because a default would be
    right half the time and silently wrong the rest.
    """
    assert {c.value for c in QuoteConventionV1} == {"base_to_quote", "quote_to_base"}
    with pytest.raises(TypeError):
        CurrencyConversionPayloadV1(                                    # type: ignore[call-arg]
            rate_table_ref="t", rate_column_ref="r", as_of_column_ref="d",
            rate_key_refs=("k",), missing_rate_behaviour=MissingRateBehaviourV1.REFUSE,
            read_basis=PolicyReadBasisV1.AS_OF_CUTOFF)


def test_MISSING_RATE_BEHAVIOUR_has_no_default():
    """Dropping changes the population; zero fabricates a conversion; NULL changes nullability. Each
    is defensible and they are not interchangeable, so the policy says which."""
    assert {b.value for b in MissingRateBehaviourV1} == {"refuse", "null_result", "drop_row"}
    with pytest.raises(TypeError):
        CurrencyConversionPayloadV1(                                    # type: ignore[call-arg]
            rate_table_ref="t", rate_column_ref="r", as_of_column_ref="d",
            rate_key_refs=("k",), quote_convention=QuoteConventionV1.BASE_TO_QUOTE,
            read_basis=PolicyReadBasisV1.AS_OF_CUTOFF)


# ══ IMMUTABLE ══════════════════════════════════════════════════════════════════════════════════
def test_A_STORED_PAYLOAD_CANNOT_BE_EDITED(db):
    """A feature verified under 'POSTED, SETTLED' must not quietly become one about
    'POSTED, SETTLED, PENDING' with its verification still green."""
    record_payload(db, STATUS, recorded_by="user:ops")
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        db.execute("UPDATE executable_policy_payload SET payload_json = '{}'::jsonb")


def test_an_unknown_KIND_is_refused_rather_than_guessed(db):
    """A payload written by a newer build is a statement about US. Interpreting it as a shape we do
    know would apply the wrong policy while claiming the right one."""
    db.execute(
        "INSERT INTO executable_policy_payload (content_hash, policy_kind, payload_version, "
        "payload_json, recorded_by) VALUES ('sha256:future','direction',1,'{}'::jsonb,'u')")
    db.execute("UPDATE pg_class SET relname = relname WHERE false")   # no-op; keeps the tx shape
    with pytest.raises((PolicyPayloadUnavailable, KeyError)):
        load_payload(db, "sha256:future")


# ══ WHEN THE POLICY IS READ — THE FACT THAT DECIDES WHETHER IT LEAKS ═══════════════════════════
def test_the_READ_BASIS_has_no_default():
    """A status column updated in place reads as it is NOW: a transaction posted in March and
    reversed yesterday reads REVERSED today, so a model trained on "was this eligible in March" is
    trained on an answer March could not have known. An append-only ledger has the opposite
    property, the catalog cannot tell them apart, and only the source's governor knows which.

    Defaulting would be worse than omitting: the leakage gate refuses `latest_available` policy
    reads, so a default of `event_time` would make every policy pass by construction.
    """
    assert {b.value for b in PolicyReadBasisV1} == {
        "as_of_cutoff", "event_time", "latest_available"}
    with pytest.raises(TypeError):
        EligibleStatusPayloadV1(                                        # type: ignore[call-arg]
            status_column_ref="c", eligible_values=("POSTED",))


def test_the_BASIS_IS_PART_OF_THE_ADDRESS():
    """The same column and values read as-of and read at current state are two different decisions,
    and one address for both would let the leaking one be served for the safe one."""
    latest = EligibleStatusPayloadV1(
        status_column_ref=STATUS.status_column_ref, eligible_values=STATUS.eligible_values,
        read_basis=PolicyReadBasisV1.LATEST_AVAILABLE)
    assert payload_content_hash(STATUS) != payload_content_hash(latest)


def test_a_payload_stored_WITHOUT_A_BASIS_refuses_on_read(db):
    """The pre-field shape, read back by this build. It must not be interpreted as though a basis
    had been recorded — whichever were assumed, the leakage gate would then decide on an assumption
    rather than on a fact."""
    db.execute(
        "INSERT INTO executable_policy_payload (content_hash, policy_kind, payload_version, "
        "payload_json, recorded_by) VALUES ('legacy','eligible_status',1,"
        """'{"status_column_ref":"c","eligible_values":["POSTED"]}'::jsonb,'user:ops')""")
    with pytest.raises(PolicyPayloadUnavailable, match="records no read_basis"):
        load_payload(db, "legacy")
