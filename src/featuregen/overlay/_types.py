"""Domain vocabulary as `Literal` type aliases (SP-1 typing overlay).

Pure type module: these aliases name the closed string vocabularies the overlay already
speaks (fact statuses, fact types, human-gate names, authority roles, join sides) so handler
signatures and the core data model can be annotated with intent instead of bare ``str``.

They are ANNOTATIONS ONLY — a `Literal` is a runtime no-op, so importing this changes no
behaviour, equality, or serialization. The canonical *runtime* values still live with their
logic and MUST stay the single source of truth; the aliases below mirror them and must be kept
in sync by hand:

* ``FactStatus`` mirrors the status constants in ``state.py``
  (``DRAFT``/``PARTIALLY_CONFIRMED``/``VERIFIED``/``REJECTED``/``STALE``/``REVERIFY``).
* ``FactType`` mirrors the fact-type constants in ``facts.py``
  (``AVAILABILITY_TIME``/``GRAIN``/``SCD_EFFECTIVE_DATING``/``APPROVED_JOIN``/``ENTITY_BRIDGE``/
  ``ENTITY_ASSIGNMENT``/``CURRENCY_BINDING``/``POLICY_TAG``); the allowed-value frozensets
  ``facts.DATA_FACT_TYPES`` / ``facts.POLICY_FACT_TYPES`` remain the runtime membership authority —
  not duplicated here.
* ``Gate`` mirrors the ``authority.gate`` human-gate names emitted by ``authority.py``.
* ``Role`` mirrors the authority-role / confirmer-role vocabulary
  (``authority.py``, ``confirmation_commands.py``, ``join_confirmation.py``).
* ``JoinSide`` mirrors the ordered ``from``/``to`` sides of an ``approved_join``.

Event-type names are NOT re-declared here: they already live as the module-level
``OVERLAY_FACT_*`` string constants in ``facts.py`` (``OVERLAY_FACT_PROPOSED`` etc.). Annotate
event-type values as ``str`` and compare against those constants; this module intentionally does
not shadow them.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

# Canonical persisted fact status (§3.4; runtime constants in state.py).
FactStatus = Literal[
    "DRAFT",
    "PARTIALLY_CONFIRMED",
    "VERIFIED",
    "REVERIFY",
    "STALE",
    "REJECTED",
]

# Fact types (§3.3; runtime constants + allowed-value frozensets in facts.py).
FactType = Literal[
    "grain",
    "availability_time",
    "scd_effective_dating",
    "approved_join",
    "entity_bridge",
    "entity_assignment",
    "currency_binding",
    "policy_tag",
]

# Human-gate names stamped onto Authority.gate / GateTaskSpec.gate (authority.py).
Gate = Literal["OVERLAY_DATA_OWNER", "OVERLAY_COMPLIANCE"]

# Authority + confirmer roles. The bare `data_owner` covers a single-side data fact; the
# side-suffixed `data_owner_from`/`data_owner_to` label the two sides of an approved_join.
Role = Literal[
    "data_owner",
    "compliance",
    "platform-admin",
    "data_owner_from",
    "data_owner_to",
]

# Ordered sides of an approved_join (authority.subjects is (from, to)).
JoinSide = Literal["from", "to"]


# --- Fixed-shape overlay payloads (SP-1 typing overlay) -----------------------------------------
#
# These `TypedDict`s name the CLOSED, fixed-shape dict payloads the overlay already builds, so
# handler signatures state the shape instead of a bare `dict`. Like the `Literal` aliases above they
# are ANNOTATIONS ONLY — a `TypedDict` is a plain `dict` at runtime, so importing/using them changes
# no behaviour, equality, or serialization; the runtime payloads stay ordinary dict literals.
#
# Genuinely polymorphic payloads are intentionally NOT modelled here: a `proposed_value` TypedDict
# (its shape varies per fact-type) and per-command `cmd.args` shapes are dynamic and left as `dict`.


class Confirmer(TypedDict):
    """One recorded confirmer on an ``OVERLAY_FACT_CONFIRMED`` payload's ``confirmers`` list.

    ``role`` is the side-labelled confirmer role — a single-side data fact records ``data_owner``
    (or ``compliance`` for a policy_tag); an ``approved_join`` records the two side-suffixed
    ``data_owner_from`` / ``data_owner_to`` roles."""

    subject: str
    role: Role


class EligibleAssignee(TypedDict):
    """A single task's assignee descriptor, as stamped onto ``human_tasks.eligible_assignees``.

    ``role`` is always present; ``subject`` is present for an owner-known side (absent for a
    governance/platform-admin side); ``side`` (``from``/``to``) is present only on the per-side
    ``approved_join`` task plan and absent for single-side facts."""

    role: Role
    subject: NotRequired[str]
    side: NotRequired[JoinSide]


class TaskProposal(TypedDict):
    """The task-scoped proposal read returned by ``task_read.get_task_proposal`` (§7.2) — what a
    task assignee must see to confirm/reject.

    ``proposed_value``/``prior_value``/``evidence`` are left loose (``Any``): the confirmed value
    is polymorphic per fact-type, and ``evidence`` is an ``Evidence`` record or ``None``."""

    object_ref: str
    fact_type: FactType
    use_case: str | None
    proposed_value: Any
    prior_value: Any
    target_event_id: str
    evidence: Any
