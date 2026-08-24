"""S1C-3 — the shadow V2 parameter chooser: audited, content-addressed, evaluation-only.

The chooser answers ONE question — which member of a closed ``ParameterSpecV2`` menu does this
hypothesis imply? — under three disciplines the tests pin separately:

* **Closed selection.** The model's answer is re-validated against the menu AFTER the audited
  call. An off-menu answer is recorded honestly as ``invalid_pick`` with an empty pick — never
  trusted, never retried beyond the repair the audited machinery itself runs.
* **Content addressing.** One provider dispatch per (menu, hypothesis, prompt version) address —
  replays are free, THROUGH THE SHARED ``structured_result`` STORE (1039), the same replay surface
  the audited machinery's other content-addressed callers already ride. ``unavailable`` is NEVER
  cached: a billing outage must not poison an address forever.
* **Fail-soft.** A provider/infrastructure failure degrades to ``status="unavailable"`` — logged,
  never raised. Live cluster LLM stages fail closed on exhausted billing; the chooser must record
  honest absence instead of taking the telemetry item down with it.

Every client below is a fake — zero real provider calls anywhere in this suite.
"""
from __future__ import annotations

import hashlib

import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.param_choice import (
    PARAM_CHOICE_PROMPT_VERSION,
    PARAM_CHOICE_RESULT_TYPE,
    PARAM_CHOICE_RESULT_VERSION,
    PARAM_CHOICE_TASK,
    ParamChoiceV1,
    choose_parameter,
    param_choice_content_address,
)
from featuregen.overlay.upload.structured_results import find_structured_result

_MENU = ("30", "90", "180")
_HYPOTHESIS = "churn follows a 90 day drop in card activity"


class _Counting:
    """Wraps a scripted FakeLLM, counting PHYSICAL provider dispatches."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls = 0

    def call(self, request):
        self.calls += 1
        return self._inner.call(request)


def _client(pick: str = "90") -> _Counting:
    return _Counting(FakeLLM(script={PARAM_CHOICE_TASK: FakeResponse(output={"pick": pick})}))


class _Exploding:
    """A provider that is DOWN (billing exhausted, network gone) — .call always raises."""

    def __init__(self) -> None:
        self.calls = 0

    def call(self, request):
        self.calls += 1
        raise RuntimeError("billing exhausted")


class _MustNotBeCalled:
    def call(self, request):  # pragma: no cover — reaching this IS the failure
        raise AssertionError("a replayed choice must never re-dispatch")


# ── closed selection ──────────────────────────────────────────────────────────────────────────


def test_a_menu_member_is_chosen(db):
    choice = choose_parameter(db, llm=_client("90"), hypothesis=_HYPOTHESIS,
                              parameter="window", menu=_MENU)
    assert choice.status == "chosen"
    assert choice.pick == "90"
    assert choice.parameter == "window"
    assert choice.menu == _MENU
    assert choice.prompt_version == PARAM_CHOICE_PROMPT_VERSION
    assert choice.content_address == param_choice_content_address(
        parameter="window", menu=_MENU, hypothesis=_HYPOTHESIS)


def test_an_out_of_menu_answer_is_recorded_invalid_never_trusted(db):
    """A rogue value the schema cannot refuse (it is a string) is refused by the CLOSED-SELECTION
    check after the call: ``invalid_pick``, empty pick — recorded honestly, never retried."""
    choice = choose_parameter(db, llm=_client("9999"), hypothesis=_HYPOTHESIS,
                              parameter="window", menu=_MENU)
    assert choice.status == "invalid_pick"
    assert choice.pick == ""
    # an invalid pick IS cached (the address answered; asking again buys the same rogue answer)
    again = choose_parameter(db, llm=_MustNotBeCalled(), hypothesis=_HYPOTHESIS,
                             parameter="window", menu=_MENU)
    assert (again.status, again.pick) == ("invalid_pick", "")


# ── content addressing ────────────────────────────────────────────────────────────────────────


def test_the_content_address_is_the_specified_material():
    expected = hashlib.sha256(
        ("param_choice|" + PARAM_CHOICE_PROMPT_VERSION + "|window|"
         + "\x1f".join(_MENU) + "|" + _HYPOTHESIS).encode("utf-8")).hexdigest()
    address = param_choice_content_address(parameter="window", menu=_MENU,
                                           hypothesis=_HYPOTHESIS)
    assert address == expected
    assert len(address) == 64


def test_a_repeat_question_replays_free_with_one_dispatch(db):
    client = _client("90")
    first = choose_parameter(db, llm=client, hypothesis=_HYPOTHESIS,
                             parameter="window", menu=_MENU)
    second = choose_parameter(db, llm=client, hypothesis=_HYPOTHESIS,
                              parameter="window", menu=_MENU)
    assert client.calls == 1                       # ONE physical dispatch covers both
    assert first == second
    assert first.content_address == second.content_address


def test_a_different_hypothesis_is_a_new_address_and_a_new_dispatch(db):
    client = _client("90")
    first = choose_parameter(db, llm=client, hypothesis=_HYPOTHESIS,
                             parameter="window", menu=_MENU)
    other = choose_parameter(db, llm=client, hypothesis="structuring within a reporting period",
                             parameter="window", menu=_MENU)
    assert client.calls == 2
    assert first.content_address != other.content_address


def test_the_choice_round_trips_through_the_shared_replay_store(db):
    """The replay surface is the SHARED content-addressed ``structured_result`` store — no new
    table. The stored row is keyed by the chooser's own address and carries the validated pick."""
    choice = choose_parameter(db, llm=_client("180"), hypothesis=_HYPOTHESIS,
                              parameter="window", menu=_MENU)
    stored = find_structured_result(
        db, result_type=PARAM_CHOICE_RESULT_TYPE,
        result_version=PARAM_CHOICE_RESULT_VERSION,
        input_content_hash=choice.content_address)
    assert stored is not None
    assert stored.output["pick"] == "180"
    assert stored.output["status"] == "chosen"


# ── fail-soft: honest absence, never an exception ─────────────────────────────────────────────


def test_a_provider_failure_degrades_to_unavailable_and_never_raises(db):
    choice = choose_parameter(db, llm=_Exploding(), hypothesis=_HYPOTHESIS,
                              parameter="window", menu=_MENU)
    assert choice.status == "unavailable"
    assert choice.pick == ""


def test_unavailable_is_never_cached_so_recovery_re_asks(db):
    """A billing outage must not poison the content address forever: the failed call stores
    NOTHING, and the next call with a working provider dispatches again and succeeds."""
    down = _Exploding()
    first = choose_parameter(db, llm=down, hypothesis=_HYPOTHESIS,
                             parameter="window", menu=_MENU)
    assert first.status == "unavailable"
    assert find_structured_result(
        db, result_type=PARAM_CHOICE_RESULT_TYPE,
        result_version=PARAM_CHOICE_RESULT_VERSION,
        input_content_hash=first.content_address) is None
    recovered_client = _client("90")
    recovered = choose_parameter(db, llm=recovered_client, hypothesis=_HYPOTHESIS,
                                 parameter="window", menu=_MENU)
    assert recovered_client.calls == 1             # re-dispatched: the outage was not cached
    assert (recovered.status, recovered.pick) == ("chosen", "90")


# ── the typed result validates itself ─────────────────────────────────────────────────────────


def test_a_chosen_result_must_name_a_menu_member():
    with pytest.raises(ValueError):
        ParamChoiceV1(parameter="window", menu=_MENU, pick="9999", status="chosen",
                      content_address="a" * 64,
                      prompt_version=PARAM_CHOICE_PROMPT_VERSION)


def test_a_pickless_status_must_carry_an_empty_pick():
    with pytest.raises(ValueError):
        ParamChoiceV1(parameter="window", menu=_MENU, pick="90", status="unavailable",
                      content_address="a" * 64,
                      prompt_version=PARAM_CHOICE_PROMPT_VERSION)


def test_the_status_vocabulary_is_closed():
    with pytest.raises(ValueError):
        ParamChoiceV1(parameter="window", menu=_MENU, pick="", status="abstained",
                      content_address="a" * 64,
                      prompt_version=PARAM_CHOICE_PROMPT_VERSION)


def test_an_empty_menu_is_a_caller_bug_not_a_choice(db):
    with pytest.raises(ValueError):
        choose_parameter(db, llm=_MustNotBeCalled(), hypothesis=_HYPOTHESIS,
                         parameter="window", menu=())
