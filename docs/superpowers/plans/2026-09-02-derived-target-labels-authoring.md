# Derived Target Labels — Proposed-Form Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a hypothesis into a registered label through a form the tool fills in — search the registry first, propose a complete draft, leave blank what the platform cannot know, let a person edit and submit.

**Architecture:** One model call, not a dialogue. The proposer returns a **draft** (loose, may have blanks); the form renders it; the submitted rule is built and validated by Plan 1's strict contract. Two representations on purpose: a draft may be incomplete, a registered rule may not.

**Tech Stack:** Python 3.11, frozen slots dataclasses, `psycopg`, `drive_audited_structured_call`, pytest with the repo's `db` fixture and `FakeLLM`.

**Spec:** `docs/superpowers/specs/2026-09-01-derived-target-labels-design.md` (§7.5)

**Depends on:** Plan 1 — shipped, `main` `8b6d209b`, migration 1142 applied. `TargetHeaderV1`, `StateChangeRuleV1`, `EventWindowRuleV1`, `EventFilterV1`, `refs_read`, `register_target`, `targets_for_entity`, `check_target_against_catalog` all exist.

## Why a form and not a conversation

Owner's decision, 2026-09-02: *"it is not a chat bot so form is better."* A dialogue is slow and
unskippable for someone who already knows what they want — the fifth label should not need four
questions — and the machinery serving it (turn contract, dialogue state, alternating-transcript
validation) exists only for the interaction.

**But not a blank form.** The owner's original requirement is that the tool *"come up with the
options as well as the logic"*, and a blank form proposes nothing. So the tool fills it in and the
person edits it. That also removes the conditional-field problem for free: the proposal picks the
shape, so the form shows only that shape's fields rather than a union of both.

## Global Constraints

- **Search the registry BEFORE proposing.** A registry written and never read is a junkyard. Spec §7.5 Step 1.
- **GUESS WHERE THE CATALOG JUSTIFIES IT; LEAVE BLANK WHERE IT DOES NOT.** The load-bearing rule of this plan. Nothing profiles column values, so the tool cannot know that `cust_perf_nonperf_flg` holds `Performing`/`Non-performing`. A wrong state value gives a label that is silently always-**0**; a wrong filter literal gives one that is silently always-**1** — a model that trains, scores and is worthless. A confidently pre-filled wrong answer gets accepted, because people confirm defaults; that is why the intake ticket already leaves a low-confidence target blank rather than pre-filling it. Spec §11.
- **Draft ≠ rule.** A draft may have blanks and is never registered. Only a complete rule passing the contract and `check_target_against_catalog` is.
- **Prompt-injection stance, inherited from `formula/author.py`:** the instruction is FIXED text; the hypothesis and the candidates ride `catalog_metadata`, redacted and audited — never concatenated into instruction text.
- **Technical honesty, inherited:** no client, an egress block or an invalid body returns a technical outcome. **A rule is never fabricated.**
- **Validation is never negotiable.** An invented ref is rejected, not repaired. `direction` is always `forward`.

---

### Task 1: Registry search and near-duplicate detection

**Files:**
- Create: `src/featuregen/overlay/upload/target_search.py`
- Test: `tests/featuregen/overlay/upload/test_target_search.py`

**Interfaces:**
- Consumes: `targets_for_entity`, `canonical_target` (Plan 1).
- Produces: `search_targets(conn, *, entity, hypothesis, limit=5) -> list[dict]` (store row + `match_terms`); `near_duplicates(conn, rule) -> list[dict]` (store row + `differs_in`).

**Why first.** No model call, so it is the cheapest task to verify — and putting it first is what makes reuse the default rather than an afterthought.

- [ ] **Step 1: Write the failing test**

```python
"""Registry search — the step that runs BEFORE any model call, so an existing label surfaces
instead of being re-invented."""
from __future__ import annotations

from dataclasses import replace

from featuregen.overlay.upload.target_contract import StateChangeRuleV1, TargetHeaderV1
from featuregen.overlay.upload.target_search import near_duplicates, search_targets
from featuregen.overlay.upload.target_store import register_target


def _rule(name: str = "tgt_npe_90d", entity: str = "customer",
          window: int = 90) -> StateChangeRuleV1:
    return StateChangeRuleV1(
        header=TargetHeaderV1(name=name, entity=entity, anchor_catalog="cib",
                              grain_ref="public.bo_cib_customer.cust_num",
                              as_of_ref="public.bo_cib_customer.business_dt",
                              window_days=window, label_type="binary",
                              operator=">=", threshold=1.0),
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Non-performing",))


def test_a_matching_label_surfaces_for_a_related_hypothesis(db):
    register_target(db, _rule(), description="customer becomes non-performing",
                    registered_by="a")
    hits = search_targets(db, entity="customer",
                          hypothesis="which customers will become non-performing")
    assert [h["name"] for h in hits] == ["tgt_npe_90d"]
    assert "performing" in hits[0]["match_terms"]


def test_search_is_scoped_to_the_entity(db):
    """A customer label must not surface for an account hypothesis — the grain differs, so it is
    not reusable however similar the words."""
    register_target(db, _rule(), description="non-performing", registered_by="a")
    assert search_targets(db, entity="account", hypothesis="non-performing") == []


def test_an_unrelated_hypothesis_matches_nothing(db):
    register_target(db, _rule(), description="non-performing", registered_by="a")
    assert search_targets(db, entity="customer", hypothesis="which payments settle late") == []


def test_an_empty_registry_returns_empty_rather_than_failing(db):
    """The FIRST person to define a label for an entity is the common case on a new deployment —
    an ordinary empty result, not an error path."""
    assert search_targets(db, entity="customer", hypothesis="anything") == []


def test_a_proposal_differing_only_in_its_WINDOW_is_named_as_a_near_duplicate(db):
    """The twin case. Content-addressing cannot catch it — the hashes differ, legitimately — so it
    must be SAID before the person submits, or the registry fills with near-identical labels."""
    register_target(db, _rule(), description="d", registered_by="a")
    twins = near_duplicates(db, _rule(name="tgt_npe_60d", window=60))
    assert [(t["name"], t["differs_in"]) for t in twins] == [("tgt_npe_90d", ("window_days",))]


def test_a_genuinely_different_rule_is_not_a_near_duplicate(db):
    """Only fields that make two rules the SAME QUESTION asked slightly differently count. A
    different watched column is a different label, not a twin."""
    register_target(db, _rule(), description="d", registered_by="a")
    other = replace(_rule(name="tgt_susp_90d"),
                    column_ref="public.bo_cib_customer.cust_susp_flg")
    assert near_duplicates(db, other) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.upload.target_search'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Registry search and near-duplicate detection — spec §7.5 Step 1, and deliberately the step with
no model call in it.

Ranked term overlap between the hypothesis and a label's name plus description. Deliberately not an
embedding or an LLM: this runs on every authoring request, the corpus is small, and a search nobody
can explain is a poor basis for "use this one instead". When the registry outgrows term overlap,
the existing catalog search is the thing to reuse — not a bespoke ranker here.
"""
from __future__ import annotations

import re

from featuregen.overlay.upload.target_store import targets_for_entity

_WORD_RE = re.compile(r"[a-z0-9_]+")

#: Words that match everything and therefore rank nothing.
_STOP = frozenset({
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "is", "are", "will", "which",
    "who", "that", "this", "with", "by", "from", "at", "as", "be", "next", "customer", "customers",
    "predict", "predicts", "predicting", "likely", "more", "days", "within", "over",
})

#: Fields whose difference makes two rules the SAME question asked slightly differently. A
#: different column or shape is a different label; a different window or threshold is a twin.
_TWIN_FIELDS = ("window_days", "threshold")


def _terms(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP and len(w) > 2}


def search_targets(conn, *, entity: str, hypothesis: str, limit: int = 5) -> list[dict]:
    """Labels already registered for this entity, ranked against the hypothesis.

    Entity-scoped and not merely entity-filtered: a label at a different grain is not reusable
    however similar the words, so it must not appear as a reuse candidate at all.
    """
    wanted = _terms(hypothesis)
    if not wanted:
        return []
    scored: list[tuple[int, str, dict]] = []
    for row in targets_for_entity(conn, entity):
        # A label's NAME carries its meaning here (`tgt_npe_90d` -> npe), so both are searched.
        have = _terms(f"{row['name']} {row['description']}")
        overlap = wanted & have
        if overlap:
            hit = dict(row)
            hit["match_terms"] = tuple(sorted(overlap))
            scored.append((len(overlap), row["name"], hit))
    # Sorted by name within a score so a listing is stable across calls.
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [hit for _, _, hit in scored[:limit]]


def near_duplicates(conn, rule) -> list[dict]:
    """Registered labels differing from `rule` ONLY in a twin field.

    Content-addressing cannot catch these — the hashes differ, correctly — so the difference has to
    be stated before a person submits, or the registry fills with `tgt_churned_60d` beside
    `tgt_churned_90d` and nobody can say which one the bank means.
    """
    from featuregen.overlay.upload.target_contract import canonical_target

    proposed = canonical_target(rule)
    proposed_head = dict(proposed["header"])
    out: list[dict] = []
    for row in targets_for_entity(conn, rule.header.entity):
        stored = row["rule"]
        if stored.get("shape") != proposed["shape"]:
            continue
        stored_head = dict(stored.get("header") or {})
        differs = tuple(f for f in _TWIN_FIELDS
                        if stored_head.get(f) != proposed_head.get(f))
        if not differs:
            continue
        ignore = _TWIN_FIELDS + ("name",)
        rest_same = (
            {k: v for k, v in stored_head.items() if k not in ignore}
            == {k: v for k, v in proposed_head.items() if k not in ignore}
            and {k: v for k, v in stored.items() if k != "header"}
            == {k: v for k, v in proposed.items() if k != "header"})
        if rest_same:
            hit = dict(row)
            hit["differs_in"] = differs
            out.append(hit)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_search.py -v`
Expected: PASS — 6 tests.

**Note on the first test:** `"non-performing"` tokenises to `non`, `performing`; the stored name
`tgt_npe_90d` gives `tgt`, `npe`, `90d` and the description gives `becomes`, `non`, `performing`.
The overlap is `{non, performing}`. If it fails, the stop-list or tokeniser is wrong — do not
weaken the assertion to make it pass.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(target): registry search and near-duplicate detection, before any model call"
```

---

### Task 2: The draft — what the tool proposes, blanks included

**Files:**
- Create: `src/featuregen/overlay/upload/target_draft.py`
- Test: `tests/featuregen/overlay/upload/test_target_draft.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `TargetDraftV1(shape, fields, needs_input, notes)`, `DraftError`, `DRAFT_SHAPES`, `NEEDS_INPUT_REASONS`.

**Why a draft type at all.** A `TargetRuleV1` refuses incomplete construction — `from_values` is
mandatory, a binary label requires a threshold. That is right for a registered rule and wrong for a
form the tool has partly filled. Two representations, on purpose: **a draft may have blanks; a
registered rule may not.**

- [ ] **Step 1: Write the failing test**

```python
"""The proposed draft: what the tool fills in, what it leaves blank, and why."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_draft import DraftError, TargetDraftV1


def _draft(**over) -> TargetDraftV1:
    base = dict(
        shape="state_change",
        fields={"name": "tgt_npe_90d", "entity": "customer", "anchor_catalog": "cib",
                "window_days": 90, "column_ref": "public.bo_cib_customer.cust_perf_nonperf_flg"},
        needs_input=("from_values", "to_values"),
        notes={"from_values": "no_value_profile", "to_values": "no_value_profile"})
    return TargetDraftV1(**{**base, **over})


def test_a_draft_may_be_INCOMPLETE_where_a_rule_may_not():
    """The whole reason this type exists: the tool cannot know the flag's values, so it must be
    able to hand back a form with those fields blank rather than guessing them."""
    d = _draft()
    assert "from_values" in d.needs_input
    assert "from_values" not in d.fields


def test_a_field_cannot_be_both_FILLED_and_NEEDED():
    """That contradiction is how a guessed value gets rendered as if a person supplied it."""
    with pytest.raises(DraftError, match="both"):
        _draft(fields={"window_days": 90}, needs_input=("window_days",),
               notes={"window_days": "not_stated"})


def test_every_needed_field_must_say_WHY_it_is_needed():
    """A blank with no reason is indistinguishable from a bug, and gets filled in carelessly."""
    with pytest.raises(DraftError, match="reason"):
        TargetDraftV1(shape="state_change", fields={}, needs_input=("from_values",), notes={})


def test_the_reason_must_be_one_the_form_can_render():
    with pytest.raises(DraftError, match="reason"):
        TargetDraftV1(shape="state_change", fields={}, needs_input=("from_values",),
                      notes={"from_values": "because I said so"})


def test_the_shape_is_closed():
    with pytest.raises(DraftError, match="shape"):
        _draft(shape="whatever")


def test_a_complete_draft_needs_nothing():
    assert _draft(needs_input=(), notes={}).needs_input == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_draft.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.upload.target_draft'`

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_draft.py -v`
Expected: PASS — 6 tests.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(target): the proposed draft — a form the tool fills, with justified blanks"
```

---

### Task 3: The proposer — one call, catalog-grounded

**Files:**
- Modify: `src/featuregen/overlay/upload/target_draft.py`
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` — register the output schema
- Test: `tests/featuregen/overlay/upload/test_target_draft.py`

**Interfaces:**
- Consumes: `TargetDraftV1`, `_shortlist` (from `contract/intake_ticket.py`).
- Produces: `TARGET_DRAFT_TASK`, `propose_target_draft(conn, client, *, hypothesis, entity, catalog_source, roles=(), actor=None) -> TargetDraftV1 | None`.

**The output schema** — register in `enrich_llm._SCHEMAS` under `("target_draft", 1)`:

```python
    ("target_draft", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "shape": {"type": "string", "enum": ["state_change", "event_window"]},
            "fields": {"type": "object", "additionalProperties": True},
            "needs_input": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "object", "additionalProperties": True}},
        "required": ["shape", "fields"]},
```

- [ ] **Step 1: Write the failing test**

Append to `tests/featuregen/overlay/upload/test_target_draft.py`:

```python
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.target_draft import TARGET_DRAFT_TASK, propose_target_draft

CIB = "cib"
_FLAG = "public.customers.perf_flg"


def _catalog(db):
    rows = [
        (CanonicalRow(CIB, "customers", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(CIB, "customers", "business_dt", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow(CIB, "customers", "perf_flg", "text"), "npe_flag"),
    ]
    build_graph(db, CIB, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _client(output: dict) -> FakeLLM:
    return FakeLLM(script={TARGET_DRAFT_TASK: FakeResponse(output=output)})


def _propose(db, client):
    return propose_target_draft(
        db, client, hypothesis="which customers go non-performing in 90 days",
        entity="customer", catalog_source=CIB, roles=("data_owner",))


def test_a_draft_comes_back_with_its_blanks_and_reasons(db):
    _catalog(db)
    draft = _propose(db, _client({
        "shape": "state_change",
        "fields": {"name": "tgt_npe_90d", "column_ref": _FLAG, "window_days": 90},
        "needs_input": ["from_values", "to_values"],
        "notes": {"from_values": "no_value_profile", "to_values": "no_value_profile"}}))
    assert draft.shape == "state_change"
    assert "from_values" in draft.needs_input
    assert draft.fields["column_ref"] == _FLAG


def test_a_ref_the_model_INVENTED_is_dropped_and_becomes_a_blank(db):
    """Selection, never generation — the intake ticket's rule, applied here. A ref that is not in
    the candidates cannot be trusted, and repairing it would let the model name a column that is
    not there and have the platform agree."""
    _catalog(db)
    draft = _propose(db, _client({
        "shape": "state_change",
        "fields": {"name": "tgt_npe_90d", "column_ref": "public.customers.invented"},
        "needs_input": [], "notes": {}}))
    assert "column_ref" not in draft.fields
    assert "column_ref" in draft.needs_input


def test_no_client_returns_nothing_rather_than_a_fabricated_draft(db):
    _catalog(db)
    assert _propose(db, None) is None


def test_a_body_that_contradicts_itself_returns_nothing(db):
    """Filled AND needed is refused by the draft type; the proposer must not paper over it."""
    _catalog(db)
    assert _propose(db, _client({
        "shape": "state_change", "fields": {"window_days": 90},
        "needs_input": ["window_days"], "notes": {"window_days": "not_stated"}})) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_draft.py -k propose -v`
Expected: FAIL — `ImportError: cannot import name 'propose_target_draft'`

- [ ] **Step 3: Write minimal implementation**

Append to `target_draft.py`:

```python
TARGET_DRAFT_TASK = "overlay.target.draft"
TARGET_DRAFT_PROMPT_ID = "target_draft"
TARGET_DRAFT_PROMPT_VERSION = 1
TARGET_DRAFT_SCHEMA_ID = "target_draft"

#: Fields that must name a candidate column. Anything else is dropped to a blank rather than
#: trusted — the intake ticket's rule ("an off-shortlist target is ABSTAIN, never trusted").
_REF_FIELDS = ("grain_ref", "as_of_ref", "column_ref", "event_date_ref",
               "join_left", "join_right", "measure_ref")

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
                         roles=(), actor=None) -> "TargetDraftV1 | None":
    """One governed call. Returns None on any technical outcome — never a fabricated draft."""
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
                notes[name] = "no_value_profile"
    try:
        return TargetDraftV1(shape=str(body.get("shape", "")), fields=fields,
                             needs_input=tuple(needs), notes=notes)
    except DraftError:
        return None
```

- [ ] **Step 4: Register the schema**

Add the `("target_draft", 1)` entry above to `_SCHEMAS` in `enrich_llm.py`, beside the other
overlay entries.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_draft.py -v`
Expected: PASS — 10 tests.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(target): the proposer — one call, catalog-grounded, blanks where a guess would lie"
```

---

### Task 4: Registration with the proposal it came from

**Files:**
- Create: `src/featuregen/db/migrations/1143_target_authoring_provenance.sql`
- Modify: `src/featuregen/overlay/upload/target_store.py`
- Test: `tests/featuregen/overlay/upload/test_target_store.py`

**Interfaces:**
- Produces: `register_target(..., proposed_draft: dict | None = None, author_comment: str = "", adapted_from: str | None = None)` — three new keyword arguments, all defaulted so every Plan 1 caller is unchanged.

**Provenance, without a transcript.** The **diff** between what the tool proposed and what the
person submitted is a more precise record than a conversation ever was — *"proposed 90 days, human
changed it to 180"* is machine-readable. The comment explains the **why** behind that diff. Storing
the proposal is what makes the diff computable later.

**Migration 1143.** 1142 was applied live 2026-09-02 08:29. Verify 1143 is free before writing:
`ls src/featuregen/db/migrations/ | sort -n | tail -3`.

- [ ] **Step 1: Write the failing test**

Append to `tests/featuregen/overlay/upload/test_target_store.py`:

```python
def test_the_proposal_and_the_comment_are_stored_with_the_definition(db):
    """The DIFF between proposed and submitted is the provenance — "proposed 90 days, human changed
    it to 180" — and the comment carries the why. For a label feeding regulated models that
    reasoning is worth as much as the rule."""
    register_target(db, _rule(), description="d", registered_by="a",
                    proposed_draft={"fields": {"window_days": 90}},
                    author_comment="180 because the FX desk reviews quarterly")
    row = target_by_name(db, "customer", "tgt_npe_90d")
    assert row["proposed_draft"]["fields"]["window_days"] == 90
    assert "FX desk" in row["author_comment"]


def test_a_label_registered_in_code_has_no_proposal_and_an_empty_comment(db):
    """Registration is not form-only — a rule authored in code is first-class, and its honest
    provenance is absent rather than invented."""
    register_target(db, _rule(), description="d", registered_by="a")
    row = target_by_name(db, "customer", "tgt_npe_90d")
    assert (row["proposed_draft"], row["author_comment"]) == (None, "")


def test_ADAPTING_a_label_records_its_ancestor(db):
    """Spec §7.5 Step 4. Without this, "we moved churn from 90 days to 60" is two
    unrelated-looking rows and nobody can see that one replaced the other."""
    ancestor = register_target(db, _rule(), description="d", registered_by="a")
    register_target(db, _rule(name="tgt_npe_60d", window=60), description="d",
                    registered_by="a", adapted_from=ancestor)
    assert target_by_name(db, "customer", "tgt_npe_60d")["adapted_from"] == ancestor
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_store.py -k "proposal or ADAPTING" -v`
Expected: FAIL — `TypeError: register_target() got an unexpected keyword argument 'proposed_draft'`

- [ ] **Step 3: Write the migration**

```sql
-- src/featuregen/db/migrations/1143_target_authoring_provenance.sql
--
-- What the tool proposed, what the person said about changing it, and what it was adapted from.
--
-- RESERVATION. 1142 was allocated 2026-09-01 and applied live 2026-09-02 08:29; 1143 is allocated
-- here. Migration files apply lexically and are checksummed — immutable once applied anywhere.
--
-- WHY COLUMNS AND NOT TABLES: all three are 1:1 with the definition, immutable once written, and
-- never queried independently. Side tables would add joins to every read for no property columns
-- do not already have.
--
-- The DIFF between `proposed_draft` and the registered rule is the provenance — "proposed 90 days,
-- human changed it to 180" — which is why the proposal is stored rather than discarded once it has
-- been edited. `author_comment` carries the why behind that diff.

ALTER TABLE target_definition
    ADD COLUMN IF NOT EXISTS proposed_draft jsonb;

ALTER TABLE target_definition
    ADD COLUMN IF NOT EXISTS author_comment text NOT NULL DEFAULT '';

-- ADAPT (spec §7.5 Step 4). Nullable because most labels descend from nothing; a self-reference is
-- refused because a self-referential lineage renders as an infinite chain to anything walking it.
ALTER TABLE target_definition
    ADD COLUMN IF NOT EXISTS adapted_from text REFERENCES target_definition(definition_id);

ALTER TABLE target_definition
    DROP CONSTRAINT IF EXISTS target_definition_adapted_from_not_self;
ALTER TABLE target_definition
    ADD CONSTRAINT target_definition_adapted_from_not_self
    CHECK (adapted_from IS NULL OR adapted_from <> definition_id);
```

- [ ] **Step 4: Thread them through the store**

In `target_store.py`: add `proposed_draft: dict | None = None`, `author_comment: str = ""` and
`adapted_from: str | None = None` as keyword arguments to `register_target`, keeping the order
`(conn, rule, *, description, registered_by, proposed_draft=None, author_comment="",
adapted_from=None)` so every Plan 1 call site is unchanged. Add all three to the INSERT's column
list and values tuple (`json.dumps(proposed_draft) if proposed_draft is not None else None`), to
`_SELECT`, and to `_row`'s parameters and returned dict.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_store.py -v`
Expected: PASS — 11 tests.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(target): migration 1143 — the proposal, the comment, and the ancestor"
```

---

### Task 5: The route

**Files:**
- Create: `src/featuregen/api/routes/targets.py`
- Modify: `src/featuregen/api/app.py`
- Test: `tests/featuregen/api/test_targets.py`

**Interfaces:**
- Produces: `POST /targets/propose`, `POST /targets`, `GET /targets?entity=`.

**Read first:** `src/featuregen/api/routes/contract.py` for the `_Conn` / `_Identity` / `_LLM`
dependency aliases and the permission decorator, and `tests/featuregen/api/_helpers.py` for `AUTH`.

- [ ] **Step 1: Write the failing test**

```python
"""The authoring route: propose a filled form, then register what the person submits."""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv


def test_propose_returns_the_registry_hits_BESIDE_the_draft(make_client):
    """Spec §7.5 Step 1 — search runs with the proposal, in a SEPARATE key. An existing label is a
    decision the organisation already made; a draft is a draft, and merging them into one list
    would hide which is which."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/targets/propose", json={
        "hypothesis": "which customers go non-performing",
        "entity": "customer", "catalog_source": "deposits"}, headers=AUTH).json()
    assert "existing" in body and "draft" in body
    assert body["draft"]["shape"] in ("state_change", "event_window")


def test_a_proposal_that_fails_technically_is_reported_not_faked(make_client):
    client = make_client(_no_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/targets/propose", json={
        "hypothesis": "h", "entity": "customer", "catalog_source": "deposits"},
        headers=AUTH).json()
    assert body["draft"] is None


def test_registering_an_INVALID_rule_is_a_typed_422(make_client):
    """The contract's refusals reach the caller intact — a backward rule is a feature, and saying
    so is more useful than a 500."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    bad = _valid_rule_for_deposits() | {"direction": "backward"}
    res = client.post("/targets", json={"rule": bad}, headers=AUTH)
    assert res.status_code == 422
    assert "forward" in str(res.json()["detail"])


def test_registering_stores_the_rule_its_proposal_and_the_comment(make_client):
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/targets", json={
        "rule": _valid_rule_for_deposits(),
        "description": "credit deterioration",
        "proposed_draft": {"fields": {"window_days": 90}},
        "author_comment": "180 because the desk reviews quarterly"}, headers=AUTH)
    assert res.status_code == 200
    listed = client.get("/targets?entity=customer", headers=AUTH).json()
    assert listed[0]["name"] == "tgt_npe_90d"
    assert "quarterly" in listed[0]["author_comment"]
```

**Note:** `_draft_fake`, `_no_draft_fake` and `_valid_rule_for_deposits` are fixtures this task
writes; model the `FakeLLM` script shape on `_proxy_fake` in
`tests/featuregen/api/test_contract_intake.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/api/test_targets.py -v`
Expected: FAIL — 404, the route is not registered.

- [ ] **Step 3: Write the route**

```python
"""Target authoring — propose a filled form, register what the person submits."""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, get_llm
from featuregen.api.permissions import require_feature_generate
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.target_catalog_check import check_target_against_catalog
from featuregen.overlay.upload.target_contract import (
    EventFilterV1,
    EventWindowRuleV1,
    StateChangeRuleV1,
    TargetContractError,
    TargetHeaderV1,
    canonical_target,
)
from featuregen.overlay.upload.target_draft import propose_target_draft
from featuregen.overlay.upload.target_search import near_duplicates, search_targets
from featuregen.overlay.upload.target_store import (
    TargetNameTaken,
    register_target,
    targets_for_entity,
)

router = APIRouter()

_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
_LLM = Annotated[LLMClient, Depends(get_llm)]


class ProposeIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    catalog_source: str = Field(min_length=1)


class RegisterIn(BaseModel):
    rule: dict
    description: str = ""
    proposed_draft: dict | None = None
    author_comment: str = ""
    adapted_from: str | None = None


def _rule_from_body(body: dict):
    """Build a typed rule from the submitted form. The contract's refusals are the caller's."""
    header = TargetHeaderV1(
        name=str(body.get("name", "")), entity=str(body.get("entity", "")),
        anchor_catalog=str(body.get("anchor_catalog", "")),
        grain_ref=str(body.get("grain_ref", "")), as_of_ref=str(body.get("as_of_ref", "")),
        window_days=int(body.get("window_days", 0)),
        label_type=str(body.get("label_type", "")),
        direction=str(body.get("direction", "forward")),
        operator=body.get("operator"), threshold=body.get("threshold"))
    if body.get("shape") == "state_change":
        return StateChangeRuleV1(
            header=header, column_ref=str(body.get("column_ref", "")),
            from_values=tuple(body.get("from_values") or ()),
            to_values=tuple(body.get("to_values") or ()),
            population_filter=str(body.get("population_filter", "from_values")))
    return EventWindowRuleV1(
        header=header, event_catalog=str(body.get("event_catalog", "")),
        event_table=str(body.get("event_table", "")),
        event_date_ref=str(body.get("event_date_ref", "")),
        join_left=str(body.get("join_left", "")), join_right=str(body.get("join_right", "")),
        aggregate=str(body.get("aggregate", "count")),
        event_filters=tuple(
            EventFilterV1(column_ref=str(f.get("column_ref", "")), op=str(f.get("op", "")),
                          value=f.get("value"), values=tuple(f.get("values") or ()),
                          value_ref=f.get("value_ref"))
            for f in (body.get("event_filters") or ())),
        measure_ref=body.get("measure_ref"),
        population_lookback_days=int(body.get("population_lookback_days", 0)),
        population_having=str(body.get("population_having", "any")))


@router.post("/targets/propose", dependencies=[Depends(require_feature_generate)])
def propose(body: ProposeIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Search FIRST, then propose. The two travel in separate keys: an existing label is a decision
    the organisation already made, a draft is a draft."""
    existing = search_targets(conn, entity=body.entity, hypothesis=body.hypothesis)
    draft = propose_target_draft(
        conn, client, hypothesis=body.hypothesis, entity=body.entity,
        catalog_source=body.catalog_source, roles=identity.role_claims, actor=identity)
    return {
        "existing": [{"name": e["name"], "description": e["description"],
                      "window_days": e["window_days"], "match_terms": list(e["match_terms"])}
                     for e in existing],
        "draft": None if draft is None else {
            "shape": draft.shape, "fields": draft.fields,
            "needs_input": list(draft.needs_input), "notes": draft.notes},
    }


@router.post("/targets", dependencies=[Depends(require_feature_generate)])
def create_target(body: RegisterIn, conn: _Conn, identity: _Identity) -> dict:
    try:
        rule = _rule_from_body(body.rule)
    except (TargetContractError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    reasons = check_target_against_catalog(conn, rule, roles=identity.role_claims)
    if reasons:
        raise HTTPException(status_code=422, detail={"reasons": list(reasons)})
    twins = near_duplicates(conn, rule)
    try:
        definition_id = register_target(
            conn, rule, description=body.description, registered_by=identity.subject,
            proposed_draft=body.proposed_draft, author_comment=body.author_comment,
            adapted_from=body.adapted_from)
    except TargetNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"definition_id": definition_id, "name": rule.header.name,
            "rule": canonical_target(rule),
            # Reported, never blocking: a twin may be deliberate, and the person has submitted.
            "near_duplicates": [{"name": t["name"], "differs_in": list(t["differs_in"])}
                                for t in twins]}


@router.get("/targets", dependencies=[Depends(require_feature_generate)])
def list_targets(entity: str, conn: _Conn, identity: _Identity) -> list[dict]:
    return targets_for_entity(conn, entity)
```

Then register it in `src/featuregen/api/app.py` beside the others (grouped around line 244):

```python
    app.include_router(targets.router)
```

adding `targets` to that module's route imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/api/test_targets.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Run the full suite and the linter**

```bash
uv run pytest -q -p no:randomly
uv run ruff check src/ tests/
```

Expected: green; ruff unchanged from the baseline of 56 pre-existing errors — do not "fix" them here.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(target): the authoring route — propose a filled form, register what is submitted"
```

---

## What this plan deliberately does NOT build

- **The UI.** A third plan, once the route's shape has survived contact with real use.
- **Join-entity agreement.** `graph_node.entity` covers 8 of `cib`'s 111 columns and 15 of `ftr`'s 126, so the check would abstain more often than it fires.
- **`target_consumer` writes.** The table exists; nothing links a run to a label until generation consumes one.
- **A diff renderer.** The proposal is stored so the diff is computable; showing it is the UI's job.
- **Anything that executes a rule.** Spec §4.

## Open questions this plan does not settle

- **Spec §12.2 — who may change a definition** other models are trained against. `adapted_from` records lineage; it does not decide authority.
