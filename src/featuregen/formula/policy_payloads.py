"""What a policy realization POINTS AT — the executable content a renderer needs.

**The gap.** A realization records `executable_content_hash` and `cas_pointer`: an address, and a
promise that content lives there. Nothing stored the content. So a policy could be decided, governed,
versioned and referenced, and still not be renderable — `eligible_status_policy_hash = abc123` does
not tell anyone to emit ``WHERE transaction_status IN ('POSTED','SETTLED')``. A hash NAMES a
decision; it is not the decision.

**Content-addressed, so the address is the integrity check.** A payload's id is the hash of its own
canonical bytes. A realization pointing at ``abc123`` either finds bytes that hash to ``abc123`` or
finds nothing — never different bytes wearing the right name.

**No defaults, anywhere.** Every declared policy resolves to content or causes a named refusal. A
defaulted policy is a wrong number wearing a governed costume: the artifact claims a decision was
applied, and the decision was invented at render time by whoever wrote the fallback. That is why
:func:`resolve_executable_policy` refuses rather than returning an empty payload, and why each
payload validates its own required fields rather than tolerating absence.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from featuregen.canonical import jcs_sha256
from featuregen.contracts.db import DbConn

__all__ = [
    "CurrencyConversionPayloadV1",
    "DirectionPayloadV1",
    "EligibleStatusPayloadV1",
    "MissingRateBehaviourV1",
    "PolicyKindV1",
    "PolicyReadBasisV1",
    "PolicyPayloadUnavailable",
    "QuoteConventionV1",
    "ReversalPayloadV1",
    "SurvivorRuleV1",
    "load_payload",
    "payload_content_hash",
    "record_payload",
    "resolve_executable_policy",
]

PAYLOAD_VERSION = 1


class PolicyKindV1(StrEnum):
    """The closed set of shapes. Each has different required fields, and a reader that cannot tell
    which kind it holds cannot know which fields to demand."""

    ELIGIBLE_STATUS = "eligible_status"
    DIRECTION = "direction"
    REVERSAL = "reversal"
    CURRENCY_CONVERSION = "currency_conversion"


class PolicyReadBasisV1(StrEnum):
    """WHEN this policy's columns are read — the fact that decides whether the policy leaks.

    **Required, with no default, because it cannot be inferred from anything else stored.** A status
    column that is UPDATED IN PLACE reads as it is now: a transaction posted last March and reversed
    yesterday reads REVERSED today, so a model trained on "was this eligible in March" is trained on
    an answer March could not have known. An append-only ledger where the status travels with the
    row has the opposite property. The two look identical in the catalog — same column, same type,
    same name — and only whoever governs the source knows which it is.

    Defaulting this would be worse than leaving it out. The leakage gate refuses
    ``LATEST_AVAILABLE`` policy reads, so a default of ``EVENT_TIME`` would make every policy pass
    the gate by construction, and a leakage gate that cannot refuse is a gate that reports safety
    it never checked.

    **One basis per policy, not per column.** A policy whose columns are read on two different bases
    is two decisions wearing one name, and it should be two policies — otherwise which basis a given
    column got would be a fact nobody recorded.

    Deliberately a SECOND enum, mirroring ``materialize.boundary_v2.KnowledgeTimeBasisV2`` member
    for member. ``formula`` must not import ``materialize`` — that is the layer direction of the
    whole package — and a string here that the compiler mapped by hand would drift the first time
    either side gained a member. A test asserts the two sets are equal, so drift is a failing test
    rather than a silent divergence.
    """

    AS_OF_CUTOFF = "as_of_cutoff"
    EVENT_TIME = "event_time"
    LATEST_AVAILABLE = "latest_available"


class QuoteConventionV1(StrEnum):
    """Which way round a rate is quoted.

    Getting this wrong does not fail — it produces a number that is the reciprocal of the right one,
    which for an FX-converted balance is off by a factor of ten or more and still looks like money.
    So it is declared, never inferred from the column name.
    """

    #: rate is UNITS OF QUOTE per one unit of BASE — multiply the base amount by it.
    BASE_TO_QUOTE = "base_to_quote"
    #: rate is units of BASE per one unit of QUOTE — divide, or invert before multiplying.
    QUOTE_TO_BASE = "quote_to_base"


class MissingRateBehaviourV1(StrEnum):
    """What happens to a row whose as-of rate does not exist.

    There is no sensible default. Dropping silently changes the population the feature is computed
    over; zero fabricates a conversion; NULL propagates honestly but changes nullability. Each is
    defensible and they are not interchangeable, so the policy says which.
    """

    REFUSE = "refuse"          # the whole build refuses — the safest, and the loudest
    NULL_RESULT = "null_result"
    DROP_ROW = "drop_row"


class SurvivorRuleV1(StrEnum):
    """Which row survives when a reversal links to an original."""

    DROP_BOTH = "drop_both"          # the pair nets to nothing and neither is counted
    KEEP_LATEST = "keep_latest"
    KEEP_ORIGINAL = "keep_original"


class PolicyPayloadUnavailable(Exception):
    """A declared policy has no executable content, named rather than defaulted.

    Carries the realization and the hash so an operator can go and look, because "policy content
    missing" without saying WHICH is not actionable.
    """


@dataclass(frozen=True, slots=True)
class EligibleStatusPayloadV1:
    """Which status values count, and the column they are read from.

    Both halves are required. Values without a column cannot be applied; a column without values is
    a filter that admits everything, which is the same as no policy at all while still claiming one.
    """

    status_column_ref: str
    eligible_values: tuple[str, ...]
    read_basis: PolicyReadBasisV1

    kind = PolicyKindV1.ELIGIBLE_STATUS

    def __post_init__(self) -> None:
        _require(self.status_column_ref, "status_column_ref")
        if not self.eligible_values:
            raise ValueError(
                "an eligible-status policy with no values admits every row: that is not a policy, "
                "and recording it as one would let a feature claim a governed filter it does not "
                "have")
        if len(set(self.eligible_values)) != len(self.eligible_values):
            raise ValueError(f"duplicate status values: {list(self.eligible_values)!r}")

    def to_json(self) -> dict:
        # SORTED, because a set of eligible values has no order and two orderings of the same set
        # must not be two different policies.
        return {"status_column_ref": self.status_column_ref,
                "eligible_values": sorted(self.eligible_values),
                "read_basis": self.read_basis.value}


@dataclass(frozen=True, slots=True)
class DirectionPayloadV1:
    """How debit and credit are told apart.

    Modelled as the column plus the values meaning each direction, rather than as a sign convention,
    because banks spell this differently per source: `DR`/`CR`, `D`/`C`, `+`/`-`, or a separate
    boolean. A convention would have to guess which; the values do not.
    """

    direction_column_ref: str
    debit_values: tuple[str, ...]
    credit_values: tuple[str, ...]
    read_basis: PolicyReadBasisV1

    kind = PolicyKindV1.DIRECTION

    def __post_init__(self) -> None:
        _require(self.direction_column_ref, "direction_column_ref")
        if not self.debit_values or not self.credit_values:
            raise ValueError(
                "a direction policy needs values for BOTH directions: one side alone cannot say "
                "what the other side is, and treating 'not debit' as credit silently classifies "
                "every unrecognised value")
        overlap = sorted(set(self.debit_values) & set(self.credit_values))
        if overlap:
            raise ValueError(
                f"{overlap!r} is listed as both debit and credit: a row cannot be both, and a "
                f"policy that says it is would make the classification depend on evaluation order")

    def to_json(self) -> dict:
        return {"direction_column_ref": self.direction_column_ref,
                "debit_values": sorted(self.debit_values),
                "credit_values": sorted(self.credit_values),
                "read_basis": self.read_basis.value}


@dataclass(frozen=True, slots=True)
class ReversalPayloadV1:
    """How a reversal is linked to its original, and which row survives.

    Both are needed and neither implies the other: knowing that a reversal points at an original
    does not say whether the pair should net to nothing or whether the later row wins.
    """

    link_column_ref: str
    survivor_rule: SurvivorRuleV1
    read_basis: PolicyReadBasisV1

    kind = PolicyKindV1.REVERSAL

    def __post_init__(self) -> None:
        _require(self.link_column_ref, "link_column_ref")

    def to_json(self) -> dict:
        return {"link_column_ref": self.link_column_ref,
                "survivor_rule": self.survivor_rule.value,
                "read_basis": self.read_basis.value}


@dataclass(frozen=True, slots=True)
class CurrencyConversionPayloadV1:
    """The rate relation and everything needed to apply it.

    **The realization OWNS this** — per the FX ownership ruling. The operator graph carries only the
    RESOLVED binding of what this declares, and never chooses a rate table itself. Two places naming
    the same table is how they come to disagree, so there is one place and this is it.

    Every field is required because every one of them, wrong, produces a plausible number:

    * a wrong ``rate_table_ref`` converts at somebody else's rates;
    * wrong ``rate_key_refs`` join the wrong currency pair;
    * a wrong ``as_of_column_ref`` applies today's rate to a year-old row;
    * a wrong ``quote_convention`` returns the reciprocal;
    * an unstated ``missing_rate_behaviour`` silently drops or zeroes rows;
    * a wrong ``read_basis`` joins today's rate to a year-old row and calls it as-of.
    """

    rate_table_ref: str
    rate_column_ref: str
    as_of_column_ref: str
    rate_key_refs: tuple[str, ...]
    quote_convention: QuoteConventionV1
    missing_rate_behaviour: MissingRateBehaviourV1
    read_basis: PolicyReadBasisV1

    kind = PolicyKindV1.CURRENCY_CONVERSION

    def __post_init__(self) -> None:
        for value, name in ((self.rate_table_ref, "rate_table_ref"),
                            (self.rate_column_ref, "rate_column_ref"),
                            (self.as_of_column_ref, "as_of_column_ref")):
            _require(value, name)
        if not self.rate_key_refs:
            raise ValueError(
                "a currency conversion with no key refs cannot say WHICH pair's rate to join: "
                "without them the as-of join matches every rate row in range, which amplifies the "
                "amount by however many currencies the table happens to carry")

    def to_json(self) -> dict:
        return {"rate_table_ref": self.rate_table_ref,
                "rate_column_ref": self.rate_column_ref,
                "as_of_column_ref": self.as_of_column_ref,
                # ORDERED: the key order is the join order, and reordering it changes the plan.
                "rate_key_refs": list(self.rate_key_refs),
                "quote_convention": self.quote_convention.value,
                "missing_rate_behaviour": self.missing_rate_behaviour.value,
                "read_basis": self.read_basis.value}


PolicyPayloadV1 = (EligibleStatusPayloadV1 | DirectionPayloadV1 | ReversalPayloadV1
                   | CurrencyConversionPayloadV1)

_BY_KIND: Mapping[str, type] = {
    PolicyKindV1.ELIGIBLE_STATUS.value: EligibleStatusPayloadV1,
    PolicyKindV1.DIRECTION.value: DirectionPayloadV1,
    PolicyKindV1.REVERSAL.value: ReversalPayloadV1,
    PolicyKindV1.CURRENCY_CONVERSION.value: CurrencyConversionPayloadV1,
}


def payload_content_hash(payload: PolicyPayloadV1) -> str:
    """The payload's address: the hash of its own canonical bytes, kind included.

    The kind is inside the hash because the same field values under two kinds are two different
    decisions, and an address that could not tell them apart would let one be served for the other.
    """
    return jcs_sha256({"kind": payload.kind.value,
                       "version": PAYLOAD_VERSION,
                       "payload": payload.to_json()})


def record_payload(conn: DbConn, payload: PolicyPayloadV1, *, recorded_by: str) -> str:
    """Store executable content and return its address. Idempotent on the content.

    Recording the same decision twice is one payload — the second call finds the first, because two
    rows with identical bytes would be two names for one thing and every reader would have to know
    which name to use.
    """
    _require(recorded_by, "recorded_by")
    content = payload_content_hash(payload)
    conn.execute(
        "INSERT INTO executable_policy_payload (content_hash, policy_kind, payload_version, "
        "payload_json, recorded_by) VALUES (%s, %s, %s, %s::jsonb, %s) "
        "ON CONFLICT (content_hash) DO NOTHING",
        (content, payload.kind.value, PAYLOAD_VERSION,
         json.dumps(payload.to_json()), recorded_by))
    return content


def load_payload(conn: DbConn, content_hash: str) -> PolicyPayloadV1 | None:
    """The content at an address, or ``None``.

    ``None`` rather than a default: an address with nothing behind it is a policy nobody supplied,
    and returning an empty payload would let a feature render as though a decision had been made.
    """
    row = conn.execute(
        "SELECT policy_kind, payload_json FROM executable_policy_payload WHERE content_hash = %s",
        (content_hash,)).fetchone()
    if row is None:
        return None
    return _from_json(row[0], row[1] if isinstance(row[1], dict) else {})


def resolve_executable_policy(
    conn: DbConn, *, realization_revision_id: str,
) -> PolicyPayloadV1:
    """The executable content one realization points at, or a NAMED refusal.

    This is the function the compiler calls, and it is deliberately the only way to get from a
    governed decision to renderable content — so there is exactly one place where "the policy is
    missing" can be answered, and it answers by refusing.

    Raises:
        PolicyPayloadUnavailable: the realization does not exist, or its content hash addresses
            nothing. Both name the realization and the hash, because an operator told only that
            "policy content is missing" has nowhere to go.
    """
    row = conn.execute(
        "SELECT executable_content_hash, policy_kind, policy_ref FROM policy_realization_revision "
        "WHERE revision_id = %s", (realization_revision_id,)).fetchone()
    if row is None:
        raise PolicyPayloadUnavailable(
            f"policy realization {realization_revision_id!r} does not exist, so there is no policy "
            f"to resolve — a feature declaring it is declaring something nobody decided")

    content_hash, kind, policy_ref = row
    payload = load_payload(conn, content_hash)
    if payload is None:
        raise PolicyPayloadUnavailable(
            f"policy realization {realization_revision_id!r} ({kind} {policy_ref!r}) points at "
            f"content {content_hash!r}, and nothing is stored there. The decision was recorded and "
            f"its content was not: the feature cannot be rendered, and rendering it with a default "
            f"would apply a policy nobody wrote")
    return payload


def _from_json(kind: str, data: Mapping[str, object]) -> PolicyPayloadV1:
    """Rebuild a typed payload, refusing a kind this build does not know.

    An unknown kind is a statement about US — a payload written by a newer build — and treating it
    as any known shape would apply the wrong policy while claiming the right one.
    """
    shape = _BY_KIND.get(kind)
    if shape is None:
        raise PolicyPayloadUnavailable(
            f"policy kind {kind!r} is not one this build knows ({sorted(_BY_KIND)}): a payload "
            f"whose shape we cannot read must not be interpreted as one we can")
    if shape is EligibleStatusPayloadV1:
        return EligibleStatusPayloadV1(
            status_column_ref=str(data["status_column_ref"]),
            eligible_values=tuple(data.get("eligible_values") or ()),
            read_basis=_basis(data))
    if shape is DirectionPayloadV1:
        return DirectionPayloadV1(
            direction_column_ref=str(data["direction_column_ref"]),
            debit_values=tuple(data.get("debit_values") or ()),
            credit_values=tuple(data.get("credit_values") or ()),
            read_basis=_basis(data))
    if shape is ReversalPayloadV1:
        return ReversalPayloadV1(
            link_column_ref=str(data["link_column_ref"]),
            survivor_rule=SurvivorRuleV1(str(data["survivor_rule"])),
            read_basis=_basis(data))
    return CurrencyConversionPayloadV1(
        rate_table_ref=str(data["rate_table_ref"]),
        rate_column_ref=str(data["rate_column_ref"]),
        as_of_column_ref=str(data["as_of_column_ref"]),
        rate_key_refs=tuple(data.get("rate_key_refs") or ()),
        quote_convention=QuoteConventionV1(str(data["quote_convention"])),
        missing_rate_behaviour=MissingRateBehaviourV1(str(data["missing_rate_behaviour"])),
        read_basis=_basis(data))


def _basis(data: Mapping[str, object]) -> PolicyReadBasisV1:
    """The stored read basis, REFUSED rather than defaulted when absent.

    A payload written before this field existed cannot be read as though it had one: whichever
    basis were assumed, the leakage gate would then decide on an assumption rather than on a
    recorded fact. Refusing sends an operator to re-record the decision, which is the only thing
    that actually establishes it.
    """
    raw = data.get("read_basis")
    if raw is None or not str(raw).strip():
        raise PolicyPayloadUnavailable(
            "this payload records no read_basis, so WHEN its columns are read is unknown. It "
            "cannot be defaulted: the leakage gate refuses post-cutoff policy reads, so assuming "
            "a basis would make this policy pass a check nobody performed. Re-record the decision "
            "with its basis stated")
    try:
        return PolicyReadBasisV1(str(raw))
    except ValueError:
        raise PolicyPayloadUnavailable(
            f"read_basis {raw!r} is not one this build knows "
            f"({[b.value for b in PolicyReadBasisV1]})") from None


def _require(value: str, name: str) -> None:
    if not (value or "").strip():
        raise ValueError(
            f"{name} is required: a policy missing it is one the renderer cannot apply, and a blank "
            f"here would become a silent default at render time")
