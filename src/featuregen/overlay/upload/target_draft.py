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


TARGET_DRAFT_TASK = "overlay.target.draft"
TARGET_DRAFT_PROMPT_ID = "target_draft"
TARGET_DRAFT_PROMPT_VERSION = 1
TARGET_DRAFT_SCHEMA_ID = "target_draft"

#: Fields the model may name, and which must therefore be candidate columns. Anything off the
#: shortlist is dropped to a blank rather than trusted — the intake ticket's rule ("an off-shortlist
#: target is ABSTAIN, never trusted").
#:
#: `grain_ref` and `as_of_ref` are NOT here: the person chose the entity, and `selectable_entities`
#: already returns its `spine_ref`. Letting the model guess a grain the person has effectively
#: already picked only invites a disagreement the grain check then rejects — with the person unable
#: to see why.
_REF_FIELDS = ("column_ref", "event_date_ref", "join_left", "join_right", "measure_ref")

#: Stamped from the person's own choice, and never taken from the model.
_CHOSEN_FIELDS = ("entity", "anchor_catalog", "grain_ref", "as_of_ref")

#: FIXED protocol text. The hypothesis and candidates ride `catalog_metadata` — the
#: `formula/author.py` injection stance: they are DATA, not instructions.
_INSTRUCTION = (
    "Propose a PREDICTION TARGET as a form, from the analyst's objective and the candidate "
    "columns supplied.\n\n"
    "`shape` is `state_change` (a column's value at the as-of date versus inside the window — use "
    "this when the outcome is a flag flipping) or `event_window` (rows in another table inside the "
    "window — use this when the outcome is something happening, or not happening).\n\n"
    "FILL a field only when the catalog justifies it: refs copied EXACTLY from the candidates, the "
    "window from a horizon the objective states, a currency where the catalog declares one.\n\n"
    "`as_of_frequency` says WHICH as-of dates the label is evaluated on (daily, weekly, monthly, "
    "quarterly, single). It has no safe default — leave it blank with reason `not_stated` unless "
    "the objective says. `require_full_window` stays true: a row whose window runs past the end of "
    "history has an outcome nobody can observe, and labelling it 0 says 'did not happen' when the "
    "truth is 'cannot see'.\n\n"
    "LEAVE BLANK — put the field in `needs_input` with a reason in `notes` — whatever you cannot "
    "know: which values a flag holds (`no_value_profile`); which of two defensible business "
    "definitions is meant (`business_choice`); whether the population is everyone or only those "
    "who have not yet had the outcome (`population_choice`); the horizon when the text states none "
    "(`not_stated`).\n\n"
    "A GUESS IS WORSE THAN A BLANK. A wrong flag value produces a label that is always 0; a wrong "
    "filter value produces one that is always 1. Both look like working models. Never invent a "
    "ref, and never both fill a field and list it in `needs_input`."
)


def propose_target_draft(conn, client, *, hypothesis: str, entity: str, catalog_source: str,
                         grain_ref: str, as_of_ref: str,
                         roles=(), actor=None) -> TargetDraftV1 | None:
    """One governed call. Returns None on any technical outcome — never a fabricated draft.

    `grain_ref` and `as_of_ref` come from the entity the PERSON chose (`selectable_entities`), and
    are stamped onto the draft rather than proposed. The model is never asked for a decision that
    has already been made.
    """
    from featuregen.overlay.upload.contract.intake_ticket import _shortlist
    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call

    if client is None:
        return None
    shortlist = _shortlist(conn, catalog_source, roles)
    known = {entry["ref"] for entry in shortlist}
    try:
        call = drive_audited_structured_call(
            conn, client, task=TARGET_DRAFT_TASK,
            prompt_id=f"{TARGET_DRAFT_PROMPT_ID}_v{TARGET_DRAFT_PROMPT_VERSION}",
            schema_id=TARGET_DRAFT_SCHEMA_ID,
            catalog_metadata={"objective": hypothesis, "candidates": shortlist,
                              "entity": entity},
            instruction=_INSTRUCTION, actor=actor)
    except Exception:  # noqa: BLE001 — a proposal is never load-bearing
        return None
    if call.output is None:
        return None

    body = dict(call.output)
    fields = dict(body.get("fields") or {})
    needs = list(body.get("needs_input") or ())
    notes = dict(body.get("notes") or {})

    # An off-candidate ref is DROPPED to a blank, never repaired and never trusted.
    for name in _REF_FIELDS:
        value = fields.get(name)
        if isinstance(value, str) and value not in known:
            del fields[name]
            if name not in needs:
                needs.append(name)
                # NOT `no_value_profile` — that would tell the person "nothing records what this
                # column contains" about a column that does not exist. The form renders a sentence
                # per reason, so a wrong code shows a wrong explanation for the blank.
                notes[name] = "not_in_catalog"

    # The chosen entity's spine is stamped on, never guessed; and it overrides anything the model
    # supplied for those keys.
    fields["entity"] = entity
    fields["anchor_catalog"] = catalog_source
    fields["grain_ref"] = grain_ref
    fields["as_of_ref"] = as_of_ref
    needs = [n for n in needs if n not in _CHOSEN_FIELDS]
    try:
        return TargetDraftV1(shape=str(body.get("shape", "")), fields=fields,
                             needs_input=tuple(needs), notes=notes)
    except DraftError:
        return None
