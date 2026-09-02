# Derived Target Labels — Conversational Authoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a hypothesis into a registered label through a dialogue — search the registry first, propose only for the gap, ask about what code cannot determine, and register one signed definition with the transcript that explains it.

**Architecture:** Stateless turns. Each request carries the transcript; the server answers and returns the next question or a final proposal; nothing is persisted until registration. The proposer emits complete structured rules validated by Plan 1's contract — the conversation decides *what* the rule says, code decides whether it is well-formed.

**Tech Stack:** Python 3.11, frozen slots dataclasses, `psycopg`, `audited_formula_call` (the per-turn governed seam `formula/author.py` already uses), pytest with the repo's `db` fixture and `FakeLLM`.

**Spec:** `docs/superpowers/specs/2026-09-01-derived-target-labels-design.md` (§7.5 is this plan)

**Depends on:** `docs/superpowers/plans/2026-09-01-derived-target-labels-contract-and-registry.md` — shipped, `main` `8b6d209b`, migration 1142 applied. `TargetHeaderV1`, `StateChangeRuleV1`, `EventWindowRuleV1`, `refs_read`, `register_target`, `targets_for_entity`, `check_target_against_catalog` all exist.

## Global Constraints

- **The order is the design.** Search the registry BEFORE proposing. A registry that is written and never read is a junkyard. Spec §7.5 Step 1.
- **Conversational interface, structured artifact.** The dialogue must land on a `TargetRuleV1`, never on prose. The platform already has the prose version — `ModelFeatureSpecV1.target_definition`, "the label, in reviewed words" — and it is exactly why no label can be computed. Spec §7.5.
- **Stateless.** No session table. The transcript rides each request and is persisted ONCE, at registration. A half-finished conversation leaves no trace, which is also the correct audit answer: provenance is only meaningful for a label that exists.
- **Prompt-injection stance, inherited from `formula/author.py`:** the instruction on every turn is FIXED protocol text. Human answers and search results ride `catalog_metadata`, redacted and audited — **never concatenated into instruction text.**
- **Technical honesty, inherited:** exhausting the turn budget, an egress block, or an invalid body returns a technical outcome. **A rule is never fabricated**, and no label is ever adopted by default.
- **Validation is never conversational.** Every proposal runs Plan 1's contract construction plus `check_target_against_catalog`. An invented ref is rejected, not repaired. `direction` is always `forward`. Spec §7.5.
- **Ask only what the platform cannot determine.** Never ask whether a ref resolves, whether join keys share an entity, or the base currency where `graph_node.currency` declares it. A question about the known teaches people to click through questions — including the one that mattered. Spec §7.5, §11.

---

### Task 1: Registry search — the step that runs before any model call

**Files:**
- Create: `src/featuregen/overlay/upload/target_search.py`
- Test: `tests/featuregen/overlay/upload/test_target_search.py`

**Interfaces:**
- Consumes: `targets_for_entity` (Plan 1).
- Produces: `search_targets(conn, *, entity, hypothesis, limit=5) -> list[dict]` — each dict is the store row plus `match_terms: tuple[str, ...]`.

**Why first.** Spec §7.5 Step 1 is a no-model step, and putting it first is what makes reuse the default rather than an afterthought. It is also the cheapest task to verify.

- [ ] **Step 1: Write the failing test**

```python
"""Registry search — the step that runs BEFORE any model call, so an existing label surfaces
instead of being re-invented."""
from __future__ import annotations

from featuregen.overlay.upload.target_contract import StateChangeRuleV1, TargetHeaderV1
from featuregen.overlay.upload.target_search import search_targets
from featuregen.overlay.upload.target_store import register_target


def _rule(name: str, entity: str = "customer") -> StateChangeRuleV1:
    return StateChangeRuleV1(
        header=TargetHeaderV1(name=name, entity=entity, anchor_catalog="cib",
                              grain_ref="public.bo_cib_customer.cust_num",
                              as_of_ref="public.bo_cib_customer.business_dt",
                              window_days=90, label_type="binary",
                              operator=">=", threshold=1.0),
        column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
        from_values=("Performing",), to_values=("Non-performing",))


def test_a_matching_label_surfaces_for_a_related_hypothesis(db):
    register_target(db, _rule("tgt_npe_90d"), description="customer becomes non-performing",
                    registered_by="a")
    hits = search_targets(db, entity="customer",
                          hypothesis="which customers will become non-performing")
    assert [h["name"] for h in hits] == ["tgt_npe_90d"]
    assert "performing" in hits[0]["match_terms"]


def test_search_is_scoped_to_the_entity(db):
    """A customer label must not surface for an account hypothesis — the grain differs, so the
    label is not reusable however similar the words."""
    register_target(db, _rule("tgt_npe_90d"), description="non-performing", registered_by="a")
    assert search_targets(db, entity="account", hypothesis="non-performing") == []


def test_an_unrelated_hypothesis_matches_nothing(db):
    register_target(db, _rule("tgt_npe_90d"), description="non-performing", registered_by="a")
    assert search_targets(db, entity="customer",
                          hypothesis="which payments settle late") == []


def test_an_empty_registry_returns_empty_rather_than_failing(db):
    """The FIRST person to define a label for an entity is the common case on a new deployment —
    it must be an ordinary empty result, not an error path."""
    assert search_targets(db, entity="customer", hypothesis="anything") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.upload.target_search'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Registry search — spec §7.5 Step 1, and deliberately the step with no model call in it.

Ranked term overlap between the hypothesis and a label's name plus description. Deliberately not
an embedding or an LLM: this runs on every authoring request, the corpus is small, and a search
nobody can explain is a poor basis for "use this one instead". When the registry grows past what
term overlap serves, the existing catalog search is the thing to reuse — not a bespoke ranker here.
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
            # Sorted by name within a score so a listing is stable across calls.
            scored.append((len(overlap), row["name"], hit))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [hit for _, _, hit in scored[:limit]]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_search.py -v`
Expected: PASS — 4 tests.

**Note on the first test:** `"non-performing"` tokenises to `non`, `performing`; the stored name
`tgt_npe_90d` gives `tgt`, `npe`, `90d` and the description gives `becomes`, `non`, `performing`.
The overlap is `{non, performing}`. If this fails, the stop-list or the tokeniser is wrong — do not
weaken the assertion to make it pass.

- [ ] **Step 5: Add near-duplicate detection**

Spec §7.5 Step 3: content-hashing catches an exact repeat, but `tgt_churned_60d` proposed beside an
existing `tgt_churned_90d` is the case that quietly fills a registry with twins. Write the failing
test first:

```python
def test_a_proposal_differing_only_in_its_WINDOW_is_named_as_a_near_duplicate(db):
    """The twin case. Content-addressing cannot catch it — the hashes differ, legitimately — so it
    must be SAID before the person picks, or the registry fills with near-identical labels."""
    register_target(db, _rule("tgt_npe_90d"), description="d", registered_by="a")
    proposed = _rule("tgt_npe_60d")
    proposed = replace_window(proposed, 60)
    twins = near_duplicates(db, proposed)
    assert [(t["name"], t["differs_in"]) for t in twins] == [("tgt_npe_90d", ("window_days",))]


def test_a_genuinely_different_rule_is_not_a_near_duplicate(db):
    """Only fields that make two rules the SAME QUESTION asked slightly differently count. A
    different watched column is a different label, not a twin."""
    register_target(db, _rule("tgt_npe_90d"), description="d", registered_by="a")
    other = _rule("tgt_susp_90d")
    other = replace_column(other, "public.bo_cib_customer.cust_susp_flg")
    assert near_duplicates(db, other) == []
```

Then implement in `target_search.py`:

```python
#: Fields whose difference makes two rules the SAME question asked slightly differently. A
#: different column or shape is a different label; a different window or threshold is a twin.
_TWIN_FIELDS = ("window_days", "threshold")


def near_duplicates(conn, rule) -> list[dict]:
    """Registered labels for this entity that differ from `rule` ONLY in a twin field.

    Content-addressing cannot catch these — the hashes differ, correctly — so the difference has
    to be stated before a person picks, or the registry fills with `tgt_churned_60d` beside
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
        # Every OTHER field must match, or it is a different label rather than a twin.
        rest_same = (
            {k: v for k, v in stored_head.items() if k not in _TWIN_FIELDS + ("name",)}
            == {k: v for k, v in proposed_head.items() if k not in _TWIN_FIELDS + ("name",)}
            and {k: v for k, v in stored.items() if k != "header"}
            == {k: v for k, v in proposed.items() if k != "header"})
        if rest_same:
            hit = dict(row)
            hit["differs_in"] = differs
            out.append(hit)
    return out
```

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_search.py -v` — expected PASS, 6 tests.
(`replace_window` / `replace_column` are one-line `dataclasses.replace` helpers on the test's own
fixture; write them beside `_rule`.)

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(target): registry search and near-duplicate detection, before any model call"
```

---

### Task 2: The turn contract — what a conversation turn is

**Files:**
- Create: `src/featuregen/overlay/upload/target_authoring.py`
- Test: `tests/featuregen/overlay/upload/test_target_authoring.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `AuthoringTurnV1(role, text)`, `AuthoringStateV1(hypothesis, entity, catalog_source, turns)`, `AUTHORING_ROLES`, `AuthoringError`, `transcript_text(state) -> str`.

**Why a typed transcript rather than a list of strings.** The transcript is persisted as provenance
and read by a human later. An untyped list cannot say who said what, and "why is this label 180
days?" is exactly the question it exists to answer.

- [ ] **Step 1: Write the failing test**

```python
"""The authoring conversation: stateless turns that must land on a structured rule."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_authoring import (
    AuthoringError,
    AuthoringStateV1,
    AuthoringTurnV1,
    transcript_text,
)


def _state(*turns: AuthoringTurnV1) -> AuthoringStateV1:
    return AuthoringStateV1(
        hypothesis="customers who go quiet are about to leave",
        entity="customer", catalog_source="cib", turns=tuple(turns))


def test_a_state_with_no_turns_is_the_opening_of_a_conversation():
    assert _state().turns == ()


def test_only_the_two_known_roles_are_accepted():
    """An unrecognised role in a persisted transcript is unreadable provenance."""
    with pytest.raises(AuthoringError, match="role"):
        AuthoringTurnV1(role="system", text="hello")


def test_a_turn_cannot_be_empty():
    with pytest.raises(AuthoringError, match="text"):
        AuthoringTurnV1(role="human", text="   ")


def test_the_transcript_renders_who_said_what():
    """Persisted for a human to read later — "why is this 180 days?" is the question it answers."""
    state = _state(AuthoringTurnV1(role="tool", text="What counts as FX here?"),
                   AuthoringTurnV1(role="human", text="conversion"))
    assert transcript_text(state) == "tool: What counts as FX here?\nhuman: conversion"


def test_a_transcript_must_alternate_starting_with_the_tool():
    """The tool asks and the human answers. Two human turns in a row means an answer was recorded
    against no question, which makes the provenance a lie."""
    with pytest.raises(AuthoringError, match="alternate"):
        _state(AuthoringTurnV1(role="human", text="conversion"),
               AuthoringTurnV1(role="human", text="180"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_authoring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.overlay.upload.target_authoring'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Conversational target authoring — spec §7.5.

STATELESS. Each request carries the transcript; the server answers; nothing is persisted until
registration, when the transcript is stored ONCE beside the definition. No session table, no
expiry, no orphaned half-conversations — and it is the correct audit answer too, since provenance
is only meaningful for a label that exists.

CONVERSATIONAL INTERFACE, STRUCTURED ARTIFACT. The dialogue must land on a `TargetRuleV1`. The
platform already has the prose version (`ModelFeatureSpecV1.target_definition`, "the label, in
reviewed words") and that is exactly why no label can be computed today.
"""
from __future__ import annotations

from dataclasses import dataclass

#: `tool` asks, `human` answers. No `system` role: the protocol instruction is FIXED text supplied
#: per turn (the `formula/author.py` injection stance), never part of the transcript.
AUTHORING_ROLES = ("tool", "human")


class AuthoringError(ValueError):
    """A malformed turn or transcript — refused at construction."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AuthoringError(message)


@dataclass(frozen=True, slots=True)
class AuthoringTurnV1:
    role: str
    text: str

    def __post_init__(self) -> None:
        _require(self.role in AUTHORING_ROLES,
                 f"role {self.role!r} not in {AUTHORING_ROLES}")
        _require(bool(self.text.strip()), "text is mandatory — an empty turn records nothing")


@dataclass(frozen=True, slots=True)
class AuthoringStateV1:
    """Everything a turn needs. Carried by the caller; never stored mid-conversation."""

    hypothesis: str
    entity: str
    catalog_source: str
    turns: tuple[AuthoringTurnV1, ...] = ()

    def __post_init__(self) -> None:
        _require(bool(self.hypothesis.strip()), "hypothesis is mandatory")
        _require(bool(self.entity.strip()), "entity is mandatory")
        _require(bool(self.catalog_source.strip()),
                 "catalog_source is mandatory — the shortlist and every ref are scoped by it")
        for index, turn in enumerate(self.turns):
            expected = "tool" if index % 2 == 0 else "human"
            _require(turn.role == expected,
                     f"turns must alternate tool/human starting with tool; turn {index} is "
                     f"{turn.role!r}, expected {expected!r}")


def transcript_text(state: AuthoringStateV1) -> str:
    """The transcript as stored provenance — who said what, in order."""
    return "\n".join(f"{turn.role}: {turn.text}" for turn in state.turns)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_authoring.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(target): the authoring turn contract — a typed, alternating transcript"
```

---

### Task 3: The proposer — one governed call that emits complete rules

**Files:**
- Modify: `src/featuregen/overlay/upload/target_authoring.py`
- Test: `tests/featuregen/overlay/upload/test_target_authoring.py`
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` — register the output schema

**Interfaces:**
- Consumes: `AuthoringStateV1`, `_shortlist` (from `contract/intake_ticket.py`), `TargetHeaderV1`, `StateChangeRuleV1`, `EventWindowRuleV1`, `check_target_against_catalog`.
- Produces: `TARGET_AUTHORING_TASK`, `TARGET_AUTHORING_SCHEMA_ID`, `propose_or_ask(conn, client, state, *, roles, actor=None) -> AuthoringResponseV1`; `AuthoringResponseV1(kind, question, rule, reasons, reason_code)` where `kind` is `"question" | "proposal" | "unavailable"`.

**The output schema** — register in `enrich_llm._SCHEMAS` under `("target_authoring", 1)`:

```python
    ("target_authoring", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            # The model either asks ONE question or emits ONE complete rule. Never both, and
            # never a rule with a question attached — a proposal a person must still interpret
            # is not a proposal.
            "kind": {"type": "string", "enum": ["question", "proposal"]},
            "question": {"type": ["string", "null"]},
            "rule": {"type": ["object", "null"], "additionalProperties": True},
        },
        "required": ["kind"]},
```

- [ ] **Step 1: Write the failing test**

```python
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.target_authoring import (
    TARGET_AUTHORING_TASK, propose_or_ask,
)

CIB = "cib"
_GRAIN = "public.customers.cust_num"
_ASOF = "public.customers.business_dt"
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
    return FakeLLM(script={TARGET_AUTHORING_TASK: FakeResponse(output=output)})


def _cib_state(*turns):
    return AuthoringStateV1(hypothesis="which customers will go non-performing in 90 days",
                            entity="customer", catalog_source=CIB, turns=tuple(turns))


def _valid_rule() -> dict:
    return {"shape": "state_change", "name": "tgt_npe_90d", "entity": "customer",
            "anchor_catalog": CIB, "grain_ref": _GRAIN, "as_of_ref": _ASOF,
            "window_days": 90, "label_type": "binary", "operator": ">=", "threshold": 1,
            "column_ref": _FLAG, "from_values": ["P"], "to_values": ["N"]}


def test_the_model_may_ask_a_question(db):
    _catalog(db)
    resp = propose_or_ask(db, _client({"kind": "question",
                                       "question": "Does the flag hold P and N?"}),
                          _cib_state(), roles=("data_owner",))
    assert (resp.kind, resp.rule) == ("question", None)
    assert "P and N" in resp.question


def test_a_complete_valid_rule_comes_back_as_a_proposal(db):
    _catalog(db)
    resp = propose_or_ask(db, _client({"kind": "proposal", "rule": _valid_rule()}),
                          _cib_state(), roles=("data_owner",))
    assert resp.kind == "proposal"
    assert resp.rule.header.name == "tgt_npe_90d"
    assert resp.rule.shape == "state_change"


def test_a_rule_with_an_INVENTED_ref_is_refused_not_repaired(db):
    """The contract's discipline, held at the seam: an invented ref is rejected. Repairing it
    would let the model name a column that is not there and have the platform agree."""
    _catalog(db)
    bad = _valid_rule() | {"column_ref": "public.customers.invented"}
    resp = propose_or_ask(db, _client({"kind": "proposal", "rule": bad}),
                          _cib_state(), roles=("data_owner",))
    assert resp.kind == "unavailable"
    assert any("invented" in r for r in resp.reasons)


def test_a_BACKWARD_rule_is_refused_rather_than_flipped(db):
    """A backward rule is a FEATURE. Silently correcting it hides the confusion."""
    _catalog(db)
    bad = _valid_rule() | {"direction": "backward"}
    resp = propose_or_ask(db, _client({"kind": "proposal", "rule": bad}),
                          _cib_state(), roles=("data_owner",))
    assert resp.kind == "unavailable"
    assert any("forward" in r for r in resp.reasons)


def test_no_client_degrades_and_never_fabricates_a_rule(db):
    """Technical honesty, inherited from formula/author.py: no provider, no proposal — and
    certainly no invented one."""
    _catalog(db)
    resp = propose_or_ask(db, None, _cib_state(), roles=("data_owner",))
    assert (resp.kind, resp.rule, resp.reason_code) == ("unavailable", None, "no_client")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_authoring.py -k propose -v`
Expected: FAIL — `ImportError: cannot import name 'propose_or_ask'`

- [ ] **Step 3: Write minimal implementation**

Append to `target_authoring.py`:

```python
TARGET_AUTHORING_TASK = "overlay.target.authoring"
TARGET_AUTHORING_PROMPT_ID = "target_authoring"
TARGET_AUTHORING_PROMPT_VERSION = 1
TARGET_AUTHORING_SCHEMA_ID = "target_authoring"

#: FIXED protocol text. Human answers and search results ride `catalog_metadata`, never here —
#: the `formula/author.py` injection stance: they are DATA, not instructions.
_INSTRUCTION = (
    "You are defining a PREDICTION TARGET as a rule, from the analyst's objective and the "
    "candidate columns supplied. Do ONE of two things.\n\n"
    "ASK, when something you cannot determine is still open: which values a flag holds; whether "
    "the population is everyone or only those who have not yet had the outcome; which of two "
    "defensible business definitions is meant; the horizon when the text states none. One "
    "question, plainly worded, no jargon.\n\n"
    "PROPOSE, when nothing is open: emit ONE complete rule. `shape` is `state_change` (a column's "
    "value at the as-of date versus inside the window) or `event_window` (rows in another table "
    "inside the window). Copy every ref EXACTLY from the candidates; never invent one. "
    "`direction` is always `forward` — you are describing what happens AFTER the as-of date.\n\n"
    "Do not ask about anything the candidates already answer: whether a column exists, what type "
    "it is, or which catalog it is in. Never both ask and propose."
)


@dataclass(frozen=True, slots=True)
class AuthoringResponseV1:
    """What one turn produced. `unavailable` is a TECHNICAL outcome — never a rule."""

    kind: str                       # "question" | "proposal" | "unavailable"
    question: str | None = None
    rule: object | None = None      # TargetRuleV1 when kind == "proposal"
    reasons: tuple[str, ...] = ()
    reason_code: str = ""


def _rule_from_output(body: dict):
    """Build a typed rule from the model's body. Raises TargetContractError when malformed —
    the contract's own refusals, unchanged and not softened for having come from a model."""
    from featuregen.overlay.upload.target_contract import (
        EventWindowRuleV1, StateChangeRuleV1, TargetHeaderV1,
    )
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
        event_filter=body.get("event_filter"), measure_ref=body.get("measure_ref"),
        population_lookback_days=int(body.get("population_lookback_days", 0)),
        population_having=str(body.get("population_having", "any")))


def propose_or_ask(conn, client, state: AuthoringStateV1, *, roles=(), actor=None):
    """One turn: the model asks a question or emits a complete rule.

    Degrades, never fabricates. No client, an egress block, an invalid body or a rule that fails
    the contract or the catalog all return `unavailable` WITH reasons — never a repaired rule.
    """
    from featuregen.overlay.upload.contract.intake_ticket import _shortlist
    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call
    from featuregen.overlay.upload.target_catalog_check import check_target_against_catalog
    from featuregen.overlay.upload.target_contract import TargetContractError

    if client is None:
        return AuthoringResponseV1(kind="unavailable", reason_code="no_client")

    shortlist = _shortlist(conn, state.catalog_source, roles)
    try:
        call = drive_audited_structured_call(
            conn, client, task=TARGET_AUTHORING_TASK,
            prompt_id=f"{TARGET_AUTHORING_PROMPT_ID}_v{TARGET_AUTHORING_PROMPT_VERSION}",
            schema_id=TARGET_AUTHORING_SCHEMA_ID,
            # Every key is DATA. The transcript rides here, never in the instruction.
            catalog_metadata={"objective": state.hypothesis, "candidates": shortlist,
                              "transcript": transcript_text(state),
                              "entity": state.entity},
            instruction=_INSTRUCTION, actor=actor)
    except Exception:  # noqa: BLE001 — a turn is never load-bearing
        return AuthoringResponseV1(kind="unavailable", reason_code="dispatch_error")
    if call.output is None:
        return AuthoringResponseV1(kind="unavailable", reason_code="no_output")

    body = dict(call.output)
    if body.get("kind") == "question":
        question = (body.get("question") or "").strip()
        if not question:
            return AuthoringResponseV1(kind="unavailable", reason_code="empty_question")
        return AuthoringResponseV1(kind="question", question=question)

    raw = body.get("rule")
    if not isinstance(raw, dict):
        return AuthoringResponseV1(kind="unavailable", reason_code="no_rule")
    try:
        rule = _rule_from_output(raw)
    except (TargetContractError, TypeError, ValueError) as exc:
        return AuthoringResponseV1(kind="unavailable", reason_code="contract_refused",
                                   reasons=(str(exc),))
    reasons = check_target_against_catalog(conn, rule, roles=roles)
    if reasons:
        return AuthoringResponseV1(kind="unavailable", reason_code="catalog_refused",
                                   reasons=reasons)
    return AuthoringResponseV1(kind="proposal", rule=rule)
```

- [ ] **Step 4: Register the schema**

In `src/featuregen/overlay/upload/enrich_llm.py`, add the `("target_authoring", 1)` entry from the
**output schema** block above to `_SCHEMAS`, beside the other overlay entries.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_authoring.py -v`
Expected: PASS — 10 tests.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(target): the authoring proposer — asks or proposes, never fabricates"
```

---

### Task 4: Registration with its transcript

**Files:**
- Create: `src/featuregen/db/migrations/1143_target_authoring_transcript.sql`
- Modify: `src/featuregen/overlay/upload/target_store.py`
- Test: `tests/featuregen/overlay/upload/test_target_store.py`

**Interfaces:**
- Consumes: `register_target` (Plan 1), `AuthoringStateV1`, `transcript_text`.
- Produces: `register_target(..., transcript: str = "", adapted_from: str | None = None)` — two new keyword arguments, both defaulted so every Plan 1 caller is unchanged.

**Migration number 1143.** 1142 is applied live (2026-09-02 08:29); 1143 is the next free number and
this plan allocates it. Verify against the branch before writing — `ls src/featuregen/db/migrations/ | sort -n | tail -3`.

- [ ] **Step 1: Write the failing test**

Append to `tests/featuregen/overlay/upload/test_target_store.py`:

```python
def test_the_transcript_is_stored_with_the_definition(db):
    """A pick-from-list flow records WHAT was chosen; a conversation records WHY — that conversion
    was meant rather than foreign-currency, that 180 days was deliberate. For a label feeding
    regulated models that reasoning is worth as much as the rule."""
    register_target(db, _rule(), description="d", registered_by="a",
                    transcript="tool: Which flag values?\nhuman: P and N")
    assert "human: P and N" in target_by_name(db, "customer", "tgt_npe_90d")["transcript"]


def test_a_label_registered_without_a_conversation_has_an_empty_transcript(db):
    """Registration is not conversation-only — a rule authored in code is first-class, and its
    honest transcript is empty rather than invented."""
    register_target(db, _rule(), description="d", registered_by="a")
    assert target_by_name(db, "customer", "tgt_npe_90d")["transcript"] == ""


def test_ADAPTING_a_label_records_its_ancestor(db):
    """Spec §7.5 Step 4. Without this, "we moved churn from 90 days to 60" is two unrelated-looking
    rows and nobody can see that one replaced the other."""
    ancestor = register_target(db, _rule(), description="d", registered_by="a")
    register_target(db, _rule(name="tgt_npe_60d", window=60), description="d",
                    registered_by="a", adapted_from=ancestor)
    assert target_by_name(db, "customer", "tgt_npe_60d")["adapted_from"] == ancestor


def test_a_label_cannot_be_its_own_ancestor(db):
    """Guarded in the schema, because a self-referential lineage renders as an infinite chain to
    anything that walks it."""
    import pytest as _pytest
    register_target(db, _rule(), description="d", registered_by="a")
    with _pytest.raises(Exception):
        db.execute("UPDATE target_definition SET adapted_from = definition_id")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_store.py -k transcript -v`
Expected: FAIL — `TypeError: register_target() got an unexpected keyword argument 'transcript'`

- [ ] **Step 3: Write the migration**

```sql
-- src/featuregen/db/migrations/1143_target_authoring_transcript.sql
--
-- The authoring conversation, stored ONCE with the definition it produced (spec §7.5).
--
-- RESERVATION. 1142 was allocated 2026-09-01 and applied live 2026-09-02 08:29; 1143 is allocated
-- here for the transcript column. Migration files apply lexically and are checksummed — immutable
-- once applied anywhere.
--
-- WHY A COLUMN AND NOT A TABLE: the transcript is 1:1 with the definition, immutable once written,
-- and never queried independently. A side table would add a join to every read for no property the
-- column does not already have. It is nullable-by-default '' so every pre-1143 row and every
-- code-authored label reads as an honest empty transcript rather than a missing one.

ALTER TABLE target_definition
    ADD COLUMN IF NOT EXISTS transcript text NOT NULL DEFAULT '';

-- ADAPT (spec §7.5 Step 4): "we moved churn from 90 days to 60" must be a VISIBLE FACT rather than
-- an archaeology exercise across two unrelated-looking rows. Nullable because most labels are not
-- adaptations of anything, and a self-reference is refused — a label cannot descend from itself.
ALTER TABLE target_definition
    ADD COLUMN IF NOT EXISTS adapted_from text REFERENCES target_definition(definition_id);

ALTER TABLE target_definition
    DROP CONSTRAINT IF EXISTS target_definition_adapted_from_not_self;
ALTER TABLE target_definition
    ADD CONSTRAINT target_definition_adapted_from_not_self
    CHECK (adapted_from IS NULL OR adapted_from <> definition_id);
```

- [ ] **Step 4: Thread it through the store**

In `target_store.py`: add `transcript: str = ""` and `adapted_from: str | None = None` to
`register_target`'s keyword arguments, add both to the INSERT's column list and values tuple, add
both to `_SELECT` and to `_row`'s parameters and returned dict. Keep the argument order
`(conn, rule, *, description, registered_by, transcript="", adapted_from=None)` so every Plan 1
call site keeps working unchanged.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_target_store.py -v`
Expected: PASS — 10 tests.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat(target): migration 1143 — the transcript, and the ancestor an adaptation descends from"
```

---

### Task 5: The route

**Files:**
- Create: `src/featuregen/api/routes/targets.py`
- Modify: `src/featuregen/api/app.py` — register the router
- Test: `tests/featuregen/api/test_targets.py`

**Interfaces:**
- Consumes: `search_targets`, `propose_or_ask`, `AuthoringStateV1`, `AuthoringTurnV1`, `register_target`, `transcript_text`.
- Produces: `POST /targets/authoring/turn`, `POST /targets`, `GET /targets?entity=`.

**Read the existing route conventions before writing:** `src/featuregen/api/routes/contract.py`
for the `_Conn` / `_Identity` / `_LLM` dependency aliases and the permission decorator, and
`tests/featuregen/api/_helpers.py` for `AUTH`.

- [ ] **Step 1: Write the failing test**

```python
"""The authoring route: search first, then a turn at a time, then one registration."""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH, upload_csv


def test_a_turn_returns_the_registry_hits_BEFORE_any_proposal(make_client):
    """Spec §7.5 Step 1 — search precedes proposing, and the response says so, or the reuse
    nudge arrives too late to be taken."""
    client = make_client(_authoring_fake(question="Which flag values?"))
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/targets/authoring/turn", json={
        "hypothesis": "which customers go non-performing",
        "entity": "customer", "catalog_source": "deposits", "turns": []},
        headers=AUTH).json()
    assert "existing" in body
    assert body["kind"] == "question"


def test_an_answered_question_carries_the_transcript_back(make_client):
    """Stateless: the client returns the transcript and the server never held it."""
    client = make_client(_authoring_fake(question="How far back?"))
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/targets/authoring/turn", json={
        "hypothesis": "h", "entity": "customer", "catalog_source": "deposits",
        "turns": [{"role": "tool", "text": "Which flag values?"},
                  {"role": "human", "text": "P and N"}]}, headers=AUTH)
    assert res.status_code == 200


def test_a_malformed_transcript_is_a_typed_422(make_client):
    """Two human turns in a row records an answer against no question — the provenance would be
    a lie, so it is refused rather than stored."""
    client = make_client(_authoring_fake(question="q"))
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/targets/authoring/turn", json={
        "hypothesis": "h", "entity": "customer", "catalog_source": "deposits",
        "turns": [{"role": "human", "text": "a"}, {"role": "human", "text": "b"}]},
        headers=AUTH)
    assert res.status_code == 422
    assert "alternate" in res.json()["detail"]


def test_registering_stores_the_rule_and_its_transcript(make_client, conn):
    client = make_client(_authoring_fake(question="q"))
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/targets", json={
        "rule": _valid_rule_for_deposits(),
        "description": "credit deterioration",
        "turns": [{"role": "tool", "text": "Which values?"},
                  {"role": "human", "text": "P and N"}]}, headers=AUTH)
    assert res.status_code == 200
    listed = client.get("/targets?entity=customer", headers=AUTH).json()
    assert listed[0]["name"] == "tgt_npe_90d"
    assert "human: P and N" in listed[0]["transcript"]
```

**Note:** `_authoring_fake` and `_valid_rule_for_deposits` are fixtures this task writes; model
them on `_proxy_fake` in `tests/featuregen/api/test_contract_intake.py`, whose `FakeLLM` script
shape is the one this route needs.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/api/test_targets.py -v`
Expected: FAIL — 404, the route is not registered.

- [ ] **Step 3: Write the route and register the router**

```python
"""Target authoring — search, one turn at a time, then one registration.

STATELESS: the transcript rides every request and is stored only when a label is registered.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, get_llm
from featuregen.api.permissions import require_feature_generate
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.target_authoring import (
    AuthoringError,
    AuthoringStateV1,
    AuthoringTurnV1,
    propose_or_ask,
    transcript_text,
)
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


class TurnIn(BaseModel):
    role: str
    text: str


class AuthoringTurnIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    catalog_source: str = Field(min_length=1)
    turns: list[TurnIn] = []


class RegisterTargetIn(BaseModel):
    rule: dict
    description: str = ""
    turns: list[TurnIn] = []
    adapted_from: str | None = None


def _state(body) -> AuthoringStateV1:
    """A malformed transcript is a typed 422, never a stored lie: two human turns in a row records
    an answer against no question."""
    try:
        return AuthoringStateV1(
            hypothesis=body.hypothesis, entity=body.entity,
            catalog_source=body.catalog_source,
            turns=tuple(AuthoringTurnV1(role=t.role, text=t.text) for t in body.turns))
    except AuthoringError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/targets/authoring/turn", dependencies=[Depends(require_feature_generate)])
def authoring_turn(body: AuthoringTurnIn, conn: _Conn, identity: _Identity,
                   client: _LLM) -> dict:
    """One turn. SEARCH RUNS FIRST — spec §7.5 Step 1 — because a reuse nudge that arrives after
    a proposal arrives too late to be taken."""
    state = _state(body)
    existing = search_targets(conn, entity=body.entity, hypothesis=body.hypothesis)
    response = propose_or_ask(conn, client, state, roles=identity.role_claims, actor=identity)
    twins = (near_duplicates(conn, response.rule)
             if response.kind == "proposal" else [])
    return {
        # Existing labels are a DECISION the organisation already made; a proposal is a draft.
        # Kept in separate keys so a client cannot render them as one undifferentiated list.
        "existing": [{"name": e["name"], "description": e["description"],
                      "window_days": e["window_days"], "match_terms": list(e["match_terms"])}
                     for e in existing],
        "kind": response.kind,
        "question": response.question,
        "rule": None if response.rule is None else _rule_json(response.rule),
        "near_duplicates": [{"name": t["name"], "differs_in": list(t["differs_in"])}
                            for t in twins],
        "reasons": list(response.reasons),
        "reason_code": response.reason_code,
    }


def _rule_json(rule) -> dict:
    from featuregen.overlay.upload.target_contract import canonical_target
    return canonical_target(rule)


@router.post("/targets", dependencies=[Depends(require_feature_generate)])
def create_target(body: RegisterTargetIn, conn: _Conn, identity: _Identity) -> dict:
    """Register one signed definition, with the conversation that produced it."""
    from featuregen.overlay.upload.target_authoring import _rule_from_output
    from featuregen.overlay.upload.target_catalog_check import check_target_against_catalog
    from featuregen.overlay.upload.target_contract import TargetContractError

    try:
        rule = _rule_from_output(body.rule)
    except (TargetContractError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    reasons = check_target_against_catalog(conn, rule, roles=identity.role_claims)
    if reasons:
        raise HTTPException(status_code=422, detail={"reasons": list(reasons)})

    state = AuthoringStateV1(
        hypothesis="registered", entity=rule.header.entity,
        catalog_source=rule.header.anchor_catalog,
        turns=tuple(AuthoringTurnV1(role=t.role, text=t.text) for t in body.turns))
    try:
        definition_id = register_target(
            conn, rule, description=body.description, registered_by=identity.subject,
            transcript=transcript_text(state), adapted_from=body.adapted_from)
    except TargetNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"definition_id": definition_id, "name": rule.header.name}


@router.get("/targets", dependencies=[Depends(require_feature_generate)])
def list_targets(entity: str, conn: _Conn, identity: _Identity) -> list[dict]:
    return targets_for_entity(conn, entity)
```

Then register the router in `src/featuregen/api/app.py` beside the others (they are grouped around
line 244):

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
git add -A && git commit -m "feat(target): the authoring route — search, turn, register"
```

---

## What this plan deliberately does NOT build

- **The UI.** A third plan, once the route's shape has survived contact with real use.
- **Join-entity agreement.** `graph_node.entity` covers 8 of `cib`'s 111 columns and 15 of `ftr`'s 126, so the check would abstain more often than it fires. The conversation can ask instead.
- **`target_consumer` writes.** The table exists; nothing links a run to a label until generation consumes one.
- **A turn budget.** `formula/author.py` bounds a MODEL-driven loop because the model decides when to stop. Here a human decides, and a person who wants twelve turns is not a runaway. Revisit if telemetry shows otherwise.
- **Anything that executes a rule.** Spec §4.

## Open questions this plan does not settle

- **Spec §12.1 — `event_filter` is free text**, and Task 3 lets a model author it. That is the sharpest remaining risk in the design: a closed `{column, op, value}` structure would cost `OR` chains but remove an un-auditable grammar from a model-authored field. Worth settling before Task 3 rather than after.
- **Spec §12.2 — who may change a definition** other models are trained against.
