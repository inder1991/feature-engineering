"""What can I use this column for? — the product view over the readiness engine.

The engine (:mod:`column_readiness`, :mod:`readiness`) is correct and untouched. Its FRAMING was the
defect: a single red ``BLOCKED`` badge stood for three unrelated situations, and the screen shouted
machine tokens (``no_authority_decision``, ``availability_no_verified_fact``) at a banker.

The sharpest symptom, straight off the screen::

    MISSING  event_time  blocking · authority hint · availability_no_verified_fact

``authority hint`` means a proposal EXISTS. The badge beside it said MISSING. We were calling the
AI's answer missing on the same line where we admitted having it.

**The product rule** (owner's direction): this tool uses AI-proposed fields whether or not a human
has reviewed them. So an unreviewed AI value is USABLE. "Blocked" is not a state this product has.

What the old badge conflated, and what each case actually needs:

======================================  ==========================  ==========================
situation                               state                       what to do
======================================  ==========================  ==========================
something is proposed, nobody confirmed :attr:`USABLE_UNREVIEWED`   confirm it (optional)
only a data check is outstanding        :attr:`NEEDS_DATA_CHECK`    profile the data
nothing proposed by anyone              :attr:`NO_CANDIDATE`        someone must decide
confirmed, nothing outstanding          :attr:`READY`               nothing
C1 projection degraded                  :attr:`UNAVAILABLE`         wait / investigate
======================================  ==========================  ==========================

The discriminator is already present in the engine's output and was simply never read: a
requirement's ``authority`` is ``"governed"``, a ``producer/strength`` pair (``llm/proposed``,
``taxonomy/proposed``, ``source/attested``), ``"hint"``, ``"external_check"``, ``"structural"``, or a
bare C1 status (``no_decision`` / ``no_value`` / ``none``). A slash or ``hint`` means *somebody
proposed something*; a bare status means *nobody did*. That one distinction is the whole reframe.

**Nothing is discarded.** Every raw requirement id stays on :attr:`RoleUsability.outstanding` for the
disclosure an engineer needs — it stops being the primary content, it does not stop existing.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from featuregen.overlay.upload.column_readiness import (
    ColumnCapability,
    ColumnReadiness,
    ColumnRequirement,
)
from featuregen.overlay.upload.readiness import FeatureReadiness


class Usability(StrEnum):
    """Named for what a banker would say, not for the machinery.

    An earlier draft used `usable_unreviewed`, which is a machine enum wearing a product label:
    nobody says "unreviewed", and the `usable_` prefix was arguing with the old design rather than
    naming the thing. Usability needs no word of its own once nothing looks like an error.
    """

    CONFIRMED = "confirmed"
    AI_PROPOSED = "ai_proposed"
    NEEDS_DATA_CHECK = "needs_data_check"
    NOT_SET = "not_set"
    UNAVAILABLE = "unavailable"


#: The states in which the tool will actually use the column for that role. `NEEDS_DATA_CHECK`
#: counts: the metadata is settled and only an external observation is outstanding, so the planner
#: can propose with the check attached rather than being barred.
_USABLE = frozenset({Usability.CONFIRMED, Usability.AI_PROPOSED,
                     Usability.NEEDS_DATA_CHECK})

#: Role -> what a banker calls it. The engine's `as_measure` is an internal name, not a label.
_ROLE_LABEL: dict[str, str] = {
    "as_measure": "Measure",
    "as_entity_key": "Entity key",
    "as_event_time": "Event time",
    "as_grain_key": "Grain key",
    "as_join_key": "Join key",
}

#: A bare C1 status means NO ONE proposed anything — distinct from a proposal nobody has confirmed.
_NO_PROPOSAL_AUTHORITIES = frozenset({
    "no_decision", "no_value", "none", "not_operational", "projection_unavailable",
})


def _has_proposal(req: ColumnRequirement) -> bool:
    """Does SOMEBODY's answer exist for this requirement, confirmed or not?

    ``hint`` is an advisory value; a ``producer/strength`` pair is a real evidence-backed proposal.
    Either way an answer exists, and the old UI reported both as MISSING.
    """
    authority = (req.authority or "").strip().lower()
    if authority in _NO_PROPOSAL_AUTHORITIES or not authority:
        return False
    return authority == "hint" or "/" in authority or authority == "governed"


def _outstanding(cap: ColumnCapability) -> tuple[ColumnRequirement, ...]:
    """Blocking requirements not yet confirmed. External previews are never blocking and are
    handled separately — they are an unknown, not an omission."""
    return tuple(r for r in cap.requirements
                 if r.blocking and r.status != "confirmed" and not r.external_preview)


def _data_checks(cap: ColumnCapability) -> tuple[ColumnRequirement, ...]:
    return tuple(r for r in cap.requirements if r.external_preview)


@dataclass(frozen=True)
class RoleUsability:
    """One role, in the language of someone deciding whether to build a feature from this column."""

    role: str
    label: str
    state: Usability
    headline: str
    detail: str
    action: str | None
    #: The raw requirement ids behind this verdict — kept for the disclosure, never the headline.
    outstanding: tuple[str, ...] = field(default_factory=tuple)
    data_checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def usable(self) -> bool:
        return self.state in _USABLE


@dataclass(frozen=True)
class ColumnUsability:
    """The five roles plus the one-line answer to "can I use this column?"."""

    object_ref: str
    roles: tuple[RoleUsability, ...]
    usable_roles: int
    total_roles: int
    headline: str


def _describe(cap: ColumnCapability) -> tuple[Usability, str, str, str | None]:
    """``(state, headline, detail, action)`` for one capability."""
    if cap.operational_status == "unavailable":
        return (Usability.UNAVAILABLE, "Unavailable",
                "The governed projection could not be read, so this role cannot be judged right now.",
                None)

    outstanding = _outstanding(cap)
    checks = _data_checks(cap)
    check_text = ("Needs a data check first: "
                  + "; ".join(_check_phrase(c) for c in checks) + ".") if checks else ""

    if not outstanding:
        if checks:
            return (Usability.NEEDS_DATA_CHECK, "Needs a data check",
                    "The metadata is settled. " + check_text, "run_data_check")
        return (Usability.CONFIRMED, "Confirmed", "Confirmed, with nothing outstanding.", None)

    if all(_has_proposal(r) for r in outstanding):
        detail = ("Proposed by AI and not yet reviewed by a person. The tool will use it as-is; "
                  "confirming it makes it governed.")
        return (Usability.AI_PROPOSED, "AI proposed",
                (detail + " " + check_text).strip(), "confirm")

    missing = ", ".join(r.requirement_id for r in outstanding if not _has_proposal(r))
    return (Usability.NOT_SET, "Not set",
            f"Nothing — person or AI — has proposed {missing} for this column. "
            f"{check_text}".strip(), "assign")


def _check_phrase(req: ColumnRequirement) -> str:
    """The external check as a QUESTION about the data, not an opaque constant."""
    name = req.requirement_id.rsplit(":", 1)[-1]
    return {
        "GRAIN_IS_UNIQUE": "is this key unique per row",
        "TYPE_IS_NUMERIC": "is the column numeric",
        "TEMPORAL_IS_POPULATED": "is the column reliably populated",
        "TEMPORAL_LAG_BOUNDED": "is the event-time lag bounded",
    }.get(name, name.lower().replace("_", " "))


def column_usability(readiness: ColumnReadiness) -> ColumnUsability:
    """Translate the capability matrix into the product view. Pure — no DB, no engine change."""
    caps: Sequence[ColumnCapability] = (
        readiness.as_measure, readiness.as_entity_key, readiness.as_event_time,
        readiness.as_grain_key, readiness.as_join_key,
    )
    roles = []
    for cap in caps:
        state, headline, detail, action = _describe(cap)
        roles.append(RoleUsability(
            role=cap.use, label=_ROLE_LABEL.get(cap.use, cap.use), state=state, headline=headline,
            detail=detail, action=action,
            outstanding=tuple(r.requirement_id for r in _outstanding(cap)),
            data_checks=tuple(r.requirement_id for r in _data_checks(cap))))
    usable = sum(1 for r in roles if r.usable)
    # Counts what the tool will ACT on. "2 / 5 ready · 3 blocked" read as failure for a catalog
    # behaving exactly as designed; usable is the honest and useful number.
    return ColumnUsability(
        object_ref=readiness.object_ref, roles=tuple(roles), usable_roles=usable,
        total_roles=len(roles),
        headline=f"Usable for {usable} of {len(roles)} roles")


# ── the parent-table roll-up ─────────────────────────────────────────────────────────────────────
# The asset-detail payload shipped the table diagnostic whole: on one CIB table that is 341 blocking
# rows plus 445 review rows, rendered as an unbounded list on EVERY column page, to say one thing —
# all 341 share the single cause `unresolved_authority`. Three hundred rows carrying one fact is a
# data dump, not a diagnosis, and it buried the column's own capabilities above it.
#
# Nothing is lost: the full lists already have a dedicated home at
# ``GET /sources/{source}/readiness?subset={table}``, which the "show all" disclosure fetches.

#: A cause code, said the way a person would say it.
_CAUSE_PLAIN: dict[str, str] = {
    "unresolved_authority": "waiting on a review decision",
    "proposed_unconfirmed": "proposed but not yet confirmed",
    "not_promoted_in_phase1": "not yet promoted",
    "fact_expired_awaiting_reverify": "the confirmation expired and needs re-verifying",
    "fact_staled_awaiting_reverify": "the source changed, so it needs re-verifying",
    "proposal_rejected": "a proposal was rejected",
    "ingestion_error": "an ingestion error",
    "subset_not_found": "the table was not found",
}


@dataclass(frozen=True)
class TableRollup:
    """The parent table in one line, plus the counts behind it — never the rows themselves.

    The split mirrors the per-role model rather than the engine's blocking flag, because those two
    disagree under the product rule. The engine marks a PROPOSED-but-unconfirmed field as blocking
    (no operational rule admits `taxonomy/proposed`), but this product USES such a value — so it is
    ``unreviewed``, not outstanding. Only a requirement nobody has answered (``missing``) or that
    two sources disagree on (``conflicting``) genuinely needs a person.
    """

    table: str
    headline: str
    #: Columns carrying an AI/derived proposal nobody has confirmed. USABLE — informational.
    columns_unreviewed: int
    #: Columns where nothing is proposed, or proposals conflict. The real to-do list.
    columns_needing_decision: int
    requirements_total: int
    dominant_cause: str | None
    dominant_cause_plain: str
    #: Every column this caller can see that has ANY item. A REAL field, not a property, because
    #: `asdict` skips properties and this is the read-scope leak surface the API test asserts on: a
    #: count including a column the caller cannot see would be an existence oracle.
    columns_outstanding: int = 0


def _columns_in(requirement_ids) -> set[str]:
    """``field:{logical_ref}:{field_name}`` -> the logical_ref. One column contributing three
    requirements is ONE thing to decide, and counting requirements overstates the work 3x.

    Only ``field:`` requirements are columns. A TABLE-scope requirement (the table's own grain or
    availability, ``{fact_name}:{source}.{schema}.{table}``) is not a column and must not inflate
    the count.
    """
    out = set()
    for rid in requirement_ids:
        if not rid.startswith("field:"):
            continue
        parts = rid.split(":")
        out.add(parts[-2] if len(parts) >= 2 else rid)
    return out


def table_rollup(diagnostic: FeatureReadiness, *, table: str) -> TableRollup:
    """Collapse a table diagnostic to counts + the dominant cause. Pure; no rows travel."""
    every = (*diagnostic.blocking_requirements, *diagnostic.review_requirements)
    unreviewed = _columns_in(r.requirement_id for r in every if r.status == "proposed")
    needs = _columns_in(r.requirement_id for r in every
                        if r.status in ("missing", "conflicting"))
    # A column with BOTH is counted once, where the action is.
    unreviewed -= needs

    causes: dict[str, int] = {}
    for req in every:
        if req.status in ("missing", "conflicting"):
            causes[req.cause] = causes.get(req.cause, 0) + 1
    dominant = max(causes, key=lambda c: causes[c]) if causes else None
    plain = _CAUSE_PLAIN.get(dominant or "", dominant or "")

    parts = []
    if unreviewed:
        parts.append(f"{len(unreviewed)} columns are AI-proposed and not yet reviewed")
    if needs:
        parts.append(f"{len(needs)} need a decision ({plain})")
    headline = "; ".join(parts) + "." if parts else "Nothing outstanding on this table."
    return TableRollup(
        table=table, headline=headline, columns_unreviewed=len(unreviewed),
        columns_needing_decision=len(needs), requirements_total=len(every),
        dominant_cause=dominant, dominant_cause_plain=plain,
        columns_outstanding=len(unreviewed) + len(needs))
