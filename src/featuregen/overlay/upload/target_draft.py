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


#: What each shape needs before a form can be filled in. A draft must ACCOUNT FOR every one of
#: them — filled, or blank with a reason. Found by the first real model call, which returned
#: `shape: state_change` and nothing else with an empty `needs_input`: valid under the old rules,
#: and rendering as a form of unexplained blanks, which is the failure this type exists to prevent.
_SHARED_FIELDS = ("name", "entity", "anchor_catalog", "grain_ref", "as_of_ref",
                  "window_days", "as_of_frequency", "label_type")
SHAPE_FIELDS = {
    "state_change": _SHARED_FIELDS + ("column_ref", "from_values", "to_values"),
    # `population_having` is here because a live call proved its absence costs the label its
    # meaning: on a hypothesis that literally said "who will START", the draft left it unset and
    # nothing objected, so the rule would have defaulted to `any` — "who will do it at all", the
    # degenerate label the spec warns about. Filled or explicitly a `population_choice` blank; not
    # silently absent.
    "event_window": _SHARED_FIELDS + ("event_catalog", "event_table", "event_date_ref",
                                      "join_left", "join_right", "aggregate",
                                      "population_having", "event_filters"),
}


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
        accounted = set(self.fields) | set(self.needs_input)
        missing = [f for f in SHAPE_FIELDS[self.shape] if f not in accounted]
        _require(not missing,
                 f"{missing!r} are neither filled nor listed in needs_input — every field a "
                 f"{self.shape} rule needs must be accounted for, or the form renders blanks "
                 "with no explanation for any of them")


TARGET_DRAFT_TASK = "overlay.target.draft"
TARGET_DRAFT_PROMPT_ID = "target_draft"
#: Bumped when `_INSTRUCTION` changes materially. It went to 2 when the instruction began naming
#: the fields per shape: the replay key is derived from the redacted INPUTS, so leaving it at 1
#: would let a result produced under the old instruction come back for a request made under the
#: new one.
TARGET_DRAFT_PROMPT_VERSION = 2
TARGET_DRAFT_SCHEMA_ID = "target_draft"
#: v3 adds `event_filters`, without which a label counting FX transactions counted every
#: transaction instead. v2 DECLARES every field a rule can carry. v1 left `fields` an open object with no properties,
#: and the schema — not the instruction — is what steers a structured-output call: two live calls
#: returned `fields: {}` and validated cleanly. v1 stays byte-frozen as their contract.
TARGET_DRAFT_SCHEMA_VERSION = 3

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
    "`fields` is a LIST OF {key, value} PAIRS, and every value is TEXT — write 90, not the number "
    "90. A field taking several values (from_values, to_values) REPEATS its key, one pair per "
    "value.\n\n"
    "EVERY field below must be ACCOUNTED FOR — either as a pair in `fields`, or in `needs_input` "
    "with a reason in `notes`. A field you simply omit is neither, and the form then shows a blank "
    "nobody can explain. `notes` is also a list of pairs: {field, reason}.\n\n"
    "Both shapes need: name (prefixed `tgt_`), window_days, as_of_frequency, label_type "
    "(binary|count|amount), and for a binary label operator + threshold. `state_change` also "
    "needs column_ref, from_values, to_values. `event_window` also needs event_catalog, "
    "event_table, event_date_ref, join_left, join_right, aggregate (count|sum). Do NOT supply "
    "entity, anchor_catalog, grain_ref or as_of_ref — those are already chosen.\n\n"
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
    "For an `event_window`, the event side lives in ANOTHER catalog: use `event_candidates` for "
    "its refs and set `event_catalog` to that candidate's `catalog`. `verified_joins` carries "
    "join keys the organisation has already CONFIRMED between the two — use one rather than "
    "choosing your own, and leave `join_right` blank if none fits.\n\n"
    "`event_filters` narrows WHICH EVENTS COUNT, and it is a separate top-level list of "
    "{column_ref, op, value} — an objective naming a currency, a channel or a product needs one, "
    "and without it the label counts every row in the table and answers a wider question than the "
    "one asked. Send an EMPTY list only when the objective genuinely counts everything. Give "
    "`event_table` as the bare table name.\n\n"
    "`population_having` decides WHICH QUESTION the label asks: `none` is 'who will START' and "
    "excludes anyone already doing this in the lookback; `any` is 'who will do it at all'. An "
    "objective saying start/begin/first-time means `none` with a lookback. Getting this wrong "
    "produces a label that looks fine and answers the other question.\n\n"
    "A GUESS IS WORSE THAN A BLANK. A wrong flag value produces a label that is always 0; a wrong "
    "filter value produces one that is always 1. Both look like working models. Never invent a "
    "ref, and never both fill a field and list it in `needs_input`."
)


#: Fields whose value is a LIST. The wire form has no arrays inside a pair, so the model repeats
#: the key — the way an HTML form encodes a multi-value input — and they are collected here.
_LIST_FIELDS = ("from_values", "to_values")
_INT_FIELDS = ("window_days", "population_lookback_days")
_FLOAT_FIELDS = ("threshold",)
_BOOL_FIELDS = ("require_full_window", "exclude_null_at_as_of", "at_least_once")


def _coerce(key: str, value):
    """Text back into the type the contract expects.

    A value that will NOT coerce is kept verbatim rather than dropped. The draft is a form, not a
    rule: showing `window_days: "ninety"` lets a person see and fix what the model said, whereas
    dropping it would present an unexplained blank and quietly lose the evidence.
    """
    text = str(value).strip()
    try:
        if key in _INT_FIELDS:
            return int(text)
        if key in _FLOAT_FIELDS:
            number = float(text)
            return int(number) if number.is_integer() else number
    except ValueError:
        return text
    if key in _BOOL_FIELDS:
        lowered = text.lower()
        return lowered == "true" if lowered in ("true", "false") else text
    return text


def _fields_from(raw) -> dict:
    """The wire form is a LIST of {key, value} pairs; the draft holds a mapping.

    Anthropic refuses a schema with this many OPTIONAL properties, so the fields cannot be declared
    as an object at all — see the schema comment. The dict form is still accepted so a recorded v1
    body remains readable data.
    """
    if isinstance(raw, dict):
        return dict(raw)
    fields: dict = {}
    for entry in raw or ():
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        key = str(entry["key"])
        if key in _LIST_FIELDS:
            fields.setdefault(key, []).append(str(entry.get("value", "")).strip())
        else:
            fields[key] = _coerce(key, entry.get("value"))
    return fields


def _notes_from(raw) -> dict:
    """The wire form is a LIST of {field, reason} pairs; the draft holds a mapping.

    A map cannot be expressed in Anthropic's schema subset — it requires closed objects, and a
    dict-valued `additionalProperties` is rejected with HTTP 400. Pairs say the same thing in the
    closed form. The dict form is still accepted here so a replayed v1 body reads correctly.
    """
    if isinstance(raw, dict):
        return dict(raw)
    notes = {}
    for entry in raw or ():
        if isinstance(entry, dict) and entry.get("field"):
            notes[str(entry["field"])] = entry.get("reason")
    return notes


def _bridged(conn, entity: str) -> list[dict]:
    """The catalogs a VERIFIED `entity_bridge_edge` says hold this same entity, with the join.

    An `event_window` label is cross-catalog BY CONSTRUCTION — it counts events in a second
    catalog — so a shortlist scoped to the anchor made every event-side field come back
    `not_in_catalog` and the shape could never be fully proposed. A live call proved it.

    Which catalogs are reachable is NOT guessed here, and neither is the join: both come from an
    edge the organisation already confirmed. Making the model invent a join key would be inventing
    something already decided, and `VERIFIED` is the only status that counts — an unconfirmed
    candidate is exactly the kind of guess this design refuses elsewhere.
    """
    rows = conn.execute(
        "SELECT left_catalog_source, left_object_ref, right_catalog_source, right_object_ref"
        "  FROM entity_bridge_edge"
        " WHERE lower(entity_id) = lower(%s) AND status = 'VERIFIED'", (entity,)).fetchall()
    joins = []
    for left_cat, left_ref, right_cat, right_ref in rows:
        joins.append({"left_catalog": left_cat, "left_ref": left_ref,
                      "right_catalog": right_cat, "right_ref": right_ref})
    return joins


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
    shortlist = [dict(entry, catalog=catalog_source)
                 for entry in _shortlist(conn, catalog_source, roles)]
    joins = _bridged(conn, entity)
    # Every OTHER catalog a verified bridge reaches — the event side of a cross-catalog label.
    event_catalogs = sorted({side for join in joins
                             for side in (join["left_catalog"], join["right_catalog"])}
                            - {catalog_source})
    event_candidates = [dict(entry, catalog=cat)
                        for cat in event_catalogs
                        for entry in _shortlist(conn, cat, roles)]
    known = {entry["ref"] for entry in shortlist} | {e["ref"] for e in event_candidates}
    try:
        call = drive_audited_structured_call(
            conn, client, task=TARGET_DRAFT_TASK,
            prompt_id=f"{TARGET_DRAFT_PROMPT_ID}_v{TARGET_DRAFT_PROMPT_VERSION}",
            schema_id=TARGET_DRAFT_SCHEMA_ID,
            schema_version=TARGET_DRAFT_SCHEMA_VERSION,
            catalog_metadata={"objective": hypothesis, "candidates": shortlist,
                              "entity": entity,
                              # The event side of a cross-catalog label, and the CONFIRMED join
                              # between them. Absent for a catalog no verified bridge reaches, in
                              # which case only `state_change` is proposable — which is the truth.
                              "event_candidates": event_candidates,
                              "verified_joins": joins},
            instruction=_INSTRUCTION, actor=actor)
    except Exception:  # noqa: BLE001 — a proposal is never load-bearing
        return None
    if call.output is None:
        return None

    body = dict(call.output)
    fields = _fields_from(body.get("fields"))
    # A LIST of conditions, not a pair — it rides its own top-level key because a filter is a
    # nested object. "No filter" must still be a decision someone made rather than a field nobody
    # mentioned, so an empty list is a filled value and the coverage rule counts it as one.
    if "event_filters" in body:
        fields["event_filters"] = [
            {k: v for k, v in dict(f).items() if v not in (None, "")}
            for f in (body.get("event_filters") or ()) if isinstance(f, dict)]
    # The live model answered `public.comp_financial_tran_repos_dly` where the contract wants the
    # bare table name, and the SQL renderer cross-checks the two — so the rule would have refused
    # to render. It is the same name; normalise rather than refuse.
    table = fields.get("event_table")
    if isinstance(table, str) and "." in table:
        fields["event_table"] = table.rsplit(".", 1)[-1]
    needs = list(body.get("needs_input") or ())
    notes = _notes_from(body.get("notes"))

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
