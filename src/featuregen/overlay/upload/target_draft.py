"""The proposed draft — a partly-filled form, not a rule.

GUESS WHERE THE CATALOG JUSTIFIES IT; LEAVE BLANK WHERE IT DOES NOT. Nothing profiles column
values, so the tool cannot know whether a flag holds `Performing` or `P`. Guessing produces a label
that is silently always-0 (a wrong state value) or silently always-1 (a wrong filter literal) — and
a confidently pre-filled wrong answer gets ACCEPTED, because people confirm defaults.

A draft may therefore be incomplete. A registered rule may not: that is `TargetRuleV1`, and this
type exists precisely so the strict one never has to relax.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DRAFT_SHAPES = ("state_change", "event_window")

#: Why a field was left blank. CLOSED, because the form renders a sentence per reason and an
#: unrecognised one renders as nothing — a blank with no explanation gets filled carelessly.
NEEDS_INPUT_REASONS = (
    "no_value_profile",        # nothing records what this column contains
    "business_choice",         # two defensible definitions; not the tool's call
    "population_choice",       # "who will do it at all" vs "who will START"
    "not_stated",              # the objective gives no horizon
    "not_in_catalog",          # the model named a column that is not in the candidates
)


class DraftError(ValueError):
    """A malformed draft — refused at construction."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DraftError(message)


@dataclass(frozen=True, slots=True)
class TargetDraftV1:
    """What the tool proposes: the fields it could justify, the fields it could not, and why."""

    shape: str
    fields: dict
    needs_input: tuple[str, ...] = ()
    notes: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # `frozen=True` protects the ATTRIBUTES, not the dicts they point at. Copying on
        # construction makes the guarantee real rather than advertised: a caller cannot reach in
        # and change a draft after it has been validated.
        object.__setattr__(self, "fields", dict(self.fields))
        object.__setattr__(self, "notes", dict(self.notes))
        _require(self.shape in DRAFT_SHAPES, f"shape {self.shape!r} not in {DRAFT_SHAPES}")
        both = set(self.fields) & set(self.needs_input)
        _require(not both,
                 f"{sorted(both)!r} are both filled and needed — a guessed value rendered as if a "
                 "person supplied it is exactly the failure this type exists to prevent")
        for name in self.needs_input:
            reason = self.notes.get(name)
            _require(reason in NEEDS_INPUT_REASONS,
                     f"{name} is needed but its reason {reason!r} is not one of "
                     f"{NEEDS_INPUT_REASONS} — a blank nobody explains gets filled in carelessly")
