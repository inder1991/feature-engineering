"""C-A3c — the MEASURE-fact reader: ``unit`` and ``currency``, read through the verified-decision
seam, with an unreadable fact REFUSING rather than reading as absent.

**Why this exists.** ``authoring_v2`` reads every operand fact through
:func:`~featuregen.overlay.upload.operational_facts.read_operational_value`, and ``field_policies``
classifies unit/currency as measure ANNOTATIONS — load-bearing only when source-ATTESTED or
human-CONFIRMED. ``read_operational_value`` answers ``not_operational`` for precisely those (its
``_GOVERNED_DECISION_FIELD`` set holds only ``additivity`` and ``logical_representation``),
``_fact_text`` turns that into ``""``, and ``not_operational`` is **not** in
``_C1_HARD_FAIL_STATUSES`` (``fork``/``hash_mismatch``/``projection_unavailable``).

The consequence is not "the value is missing". It is that a per-row-currency monetary operand
arrives at ``resolve_output_v2`` looking **non-monetary**, with nothing recorded — so BR-6's
``CURRENCY_CONVERSION_UNDECLARED`` tooth (``output_authority_v2``: ``if facts.unit == "monetary"
and facts.currency == "per_row"``) cannot fire at all, and a mixed-currency population is summed
across currencies in silence. Both V2 fact paths fail this way: the frozen snapshot reader hardcodes
``status = "not_operational"  # hint by policy — never governed``.

:func:`~featuregen.overlay.upload.operational_facts.read_verified_decision_value` is the seam that
answers honestly — it re-resolves the value from the exact active evidence the latest decision names
and requires both evidence and value hashes to match before ``resolved``. Its only field gate is
``policy_for(field).operational_rule``, which unit and currency both carry
(``influence_max=OPERATIONAL``, ``operational_rule=_SOURCE_OR_HUMAN``).

**What this refuses, and what it deliberately does not.** An ABSENT decision is not an error: most
columns carry no unit and are legitimately non-monetary, and refusing there would break every
existing feature. An UNREADABLE one is an error — a conflict, a fork, a hash mismatch, a retired
decision or an unavailable projection each mean the platform *cannot say* what the column is, and
answering ``""`` there is exactly the silent degradation this module exists to end.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.contracts.db import DbConn
from featuregen.overlay.upload.operational_facts import (
    OperationalValue,
    read_verified_decision_value,
)

__all__ = [
    "MEASURE_FIELDS",
    "MeasureFact",
    "MeasureFacts",
    "MeasureFactsUnreadable",
    "MeasureReadDisposition",
    "disposition_of",
    "read_measure_facts",
]

#: The two fields this reader owns. ``logical_type`` and grain stay with their existing readers —
#: they are governed DECISION fields, already answered correctly by ``read_operational_value``.
MEASURE_FIELDS: tuple[str, ...] = ("unit", "currency")


class MeasureReadDisposition(StrEnum):
    """What one status MEANS for a measure field — the whole judgement of this module."""

    #: The decision exists and its evidence verified. Use the value.
    RESOLVED = "resolved"
    #: Nobody has decided this column's unit/currency. Legitimate: most columns are not measures.
    ABSENT = "absent"
    #: A decision exists but the platform cannot serve it. Refuse — never read as ABSENT.
    UNREADABLE = "unreadable"


#: Statuses that mean "no one has said" — the only non-``resolved`` case that is not a refusal.
#: ``retired`` is deliberately NOT here: a withdrawn decision is a column whose meaning USED to be
#: declared, and treating that as "never a measure" is the same silent downgrade as ``""``.
_ABSENT_STATUSES: frozenset[str] = frozenset({"no_decision", "no_value"})


def disposition_of(status: str) -> MeasureReadDisposition:
    """Map an :class:`OperationalValue` status onto this module's three-way judgement.

    Total over the C1 vocabulary (``conflict``, ``fork``, ``hash_mismatch``, ``no_decision``,
    ``no_value``, ``not_operational``, ``projection_unavailable``, ``resolved``, ``retired``) and
    over anything added later: an unrecognised status is UNREADABLE, because a status this module
    has never seen is the one case where guessing is least defensible.
    """
    if status == "resolved":
        return MeasureReadDisposition.RESOLVED
    if status in _ABSENT_STATUSES:
        return MeasureReadDisposition.ABSENT
    return MeasureReadDisposition.UNREADABLE


@dataclass(frozen=True, slots=True)
class MeasureFact:
    """One measure fact and the provenance an occurrence must pin (C-A3c).

    ``value`` is ``""`` only for :attr:`MeasureReadDisposition.ABSENT` — an unreadable fact never
    reaches this type, because :func:`read_measure_facts` refuses first.
    """

    field: str
    value: str
    disposition: MeasureReadDisposition
    producer: str | None
    strength: str | None
    decision_event_id: str | None
    selected_evidence_ids: tuple[str, ...]
    policy_version: str
    resolver_version: str | None


@dataclass(frozen=True, slots=True)
class MeasureFacts:
    """Both measure facts for one operand, each carrying its own provenance."""

    logical_ref: str
    unit: MeasureFact
    currency: MeasureFact

    @property
    def is_monetary_per_row(self) -> bool:
        """The exact predicate ``output_authority_v2`` tests, answered from VERIFIED reads.

        Named here so the FX-conversion requirement is decided in one place rather than by two
        string comparisons that could drift apart.
        """
        return self.unit.value == "monetary" and self.currency.value == "per_row"


@dataclass(frozen=True, slots=True)
class MeasureFactsUnreadable:
    """A typed refusal: a measure decision exists but could not be served.

    Carries the field and the raw status so the caller can name BOTH in its refusal — "currency is
    ``hash_mismatch``" is actionable; "currency is empty" is what this module exists to stop.
    """

    logical_ref: str
    field: str
    status: str
    reason: str | None

    def detail(self) -> str:
        because = f": {self.reason}" if self.reason else ""
        return (f"{self.logical_ref} carries a {self.field} decision that cannot be served "
                f"(status {self.status!r}){because} — refusing rather than reading it as a "
                f"non-measure, which would sum across currencies in silence")


def read_measure_facts(
    conn: DbConn, logical_ref: str
) -> MeasureFacts | MeasureFactsUnreadable:
    """Read ``unit`` and ``currency`` for one operand, or refuse.

    Returns :class:`MeasureFactsUnreadable` on the FIRST unreadable field, in
    :data:`MEASURE_FIELDS` order, so the refusal is deterministic for a given catalog state rather
    than depending on which read happened to be attempted first.
    """
    facts: dict[str, MeasureFact] = {}
    for field in MEASURE_FIELDS:
        value = read_verified_decision_value(conn, logical_ref, field)
        disposition = disposition_of(value.status)
        if disposition is MeasureReadDisposition.UNREADABLE:
            return MeasureFactsUnreadable(
                logical_ref=logical_ref,
                field=field,
                status=value.status,
                reason=_reason_of(value),
            )
        facts[field] = _fact(field, value, disposition)
    return MeasureFacts(logical_ref=logical_ref, unit=facts["unit"],
                        currency=facts["currency"])


def _fact(
    field: str, value: OperationalValue, disposition: MeasureReadDisposition
) -> MeasureFact:
    return MeasureFact(
        field=field,
        value="" if value.value is None else str(value.value),
        disposition=disposition,
        producer=None if value.producer is None else str(value.producer),
        strength=None if value.strength is None else str(value.strength),
        decision_event_id=value.decision_event_id,
        selected_evidence_ids=tuple(value.selected_evidence_ids),
        policy_version=value.policy_version,
        resolver_version=value.resolver_version,
    )


def _reason_of(value: OperationalValue) -> str | None:
    """The conflict detail, when the read carried one — read defensively.

    ``OperationalValue`` exposes ``conflict_status``; a reader that assumed a ``reason`` attribute
    would break the moment the shape moved, and this module must not be the thing that fails when
    it is the thing reporting a failure.
    """
    return getattr(value, "conflict_status", None)
