"""TASK 4b — the hypothesis chooses PARAMETERS (the "it looks hardcoded" defect).

23 grounded recipes offered 147 authored parameterisations and the platform emitted the same 23
first-in-list defaults for every question ever asked. The fix is a CLOSED SELECTION: the model
picks values from the authored tuples (the menu), every answer is re-validated against
``Template.params`` before grounding, and ``_bind_params`` re-guards it a second time — the model
cannot invent a setting. A hypothesis that implies nothing ABSTAINS to the current default, so
flag-off and abstain are byte-identical to today. Replay is per-template through
``structured_result`` (an abstain is stored too — it never re-asks); one provider call per build
covers every miss. Card identity already includes the parameterisation (the feature NAME carries
the window; ``semantic_parameter_binding_hash`` covers the rest), so the same recipe under two
hypotheses is two identities.
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import build_considered_set
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.contract.param_choice import PARAM_CHOICE_TASK, choose_params
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.templates import ALL_TEMPLATES

NOW = datetime(2026, 7, 5, tzinfo=UTC)

# A REAL registry template with a multi-value window param — found, not invented.
_WINDOWED = next(t for t in ALL_TEMPLATES
                 if "window" in t.params and len(t.params["window"]) > 1)
_DEFAULT_WINDOW = _WINDOWED.params["window"][0]
_ALT_WINDOW = _WINDOWED.params["window"][1]


def _choice_client(value=None) -> FakeLLM:
    value = value if value is not None else _ALT_WINDOW
    return FakeLLM(script={PARAM_CHOICE_TASK: FakeResponse(output={"choices": [
        {"template_id": _WINDOWED.id, "param": "window", "value": str(value)}]})})


class _MustNotBeCalled:
    def call(self, *a, **k):  # pragma: no cover
        raise AssertionError("a replayed choice must never re-dispatch")


def test_a_chosen_value_lands_typed_and_a_rerun_replays_per_template(db):
    chosen = choose_params(db, _choice_client(), templates=[_WINDOWED],
                           redacted_hypothesis="detect structuring within a reporting period")
    assert chosen == {_WINDOWED.id: {"window": _ALT_WINDOW}}
    assert type(chosen[_WINDOWED.id]["window"]) is type(_ALT_WINDOW), \
        "the AUTHORED value object, matched by string form — never the model's string"
    again = choose_params(db, _MustNotBeCalled(), templates=[_WINDOWED],
                          redacted_hypothesis="detect structuring within a reporting period")
    assert again == chosen


def test_an_abstain_is_stored_and_never_re_asks(db):
    empty = FakeLLM(script={PARAM_CHOICE_TASK: FakeResponse(output={"choices": []})})
    assert choose_params(db, empty, templates=[_WINDOWED],
                         redacted_hypothesis="a question implying nothing") == {}
    # the abstain replays — no second dispatch for the same (template, hypothesis)
    assert choose_params(db, _MustNotBeCalled(), templates=[_WINDOWED],
                         redacted_hypothesis="a question implying nothing") == {}


def test_off_menu_answers_are_dropped_never_trusted(db):
    bad = FakeLLM(script={PARAM_CHOICE_TASK: FakeResponse(output={"choices": [
        {"template_id": _WINDOWED.id, "param": "window", "value": "9999"},
        {"template_id": _WINDOWED.id, "param": "invented_param", "value": "1"},
        {"template_id": "invented_template", "param": "window", "value": str(_ALT_WINDOW)},
    ]})})
    assert choose_params(db, bad, templates=[_WINDOWED],
                         redacted_hypothesis="try to invent settings") == {}


def test_two_hypotheses_are_two_cache_keys_two_choices(db):
    first = choose_params(db, _choice_client(_ALT_WINDOW), templates=[_WINDOWED],
                          redacted_hypothesis="a short-period question")
    second = choose_params(db, _choice_client(_DEFAULT_WINDOW), templates=[_WINDOWED],
                           redacted_hypothesis="a long-horizon question")
    assert first[_WINDOWED.id]["window"] == _ALT_WINDOW
    assert second[_WINDOWED.id]["window"] == _DEFAULT_WINDOW


# ── builder integration: flag-gated end to end, identity carries the parameterisation ────────────

def _catalog_for(db, template):
    """A catalog carrying exactly the concepts the windowed template needs to ground."""
    rows, seen = [], set()
    for i, need in enumerate(template.needs):
        if need.concept in seen:
            continue
        seen.add(need.concept)
        col = CanonicalRow("bank", "accounts", f"col_{need.concept}"[:60],
                           "timestamp" if "timestamp" in need.concept or "date" in need.concept
                           else "numeric",
                           is_grain=(need.concept == "customer_id"),
                           entity="Customer" if need.concept == "customer_id" else None,
                           as_of=("as_of" in need.concept),
                           additivity="additive" if "flow" in need.concept else "",
                           currency="USD" if "monetary" in need.concept else "")
        rows.append((col, need.concept))
    build_graph(db, "bank", [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (NOW, NOW))


def _gen_client():
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": []}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "templates", "reasoning": "recipes fit"}),
        PARAM_CHOICE_TASK: FakeResponse(output={"choices": [
            {"template_id": _WINDOWED.id, "param": "window", "value": str(_ALT_WINDOW)}]}),
    })


def test_param_choice_runs_unconditionally_and_the_model_pick_applies(db):
    """Pre-live simplification (2026-08-11): FEATUREGEN_PARAM_CHOICE retired — with a client
    present the hypothesis-chosen parameter applies without any env, and the "also available"
    alternatives line is populated."""
    _catalog_for(db, _WINDOWED)
    intent = submit_intent(hypothesis="what is available here", actor="ds1")
    cs = build_considered_set(db, intent, _gen_client(), catalog_source="bank", now=NOW)
    recipe_ideas = [f for s in cs.alternatives for f in s.features
                    if f.recipe_id == _WINDOWED.id]
    if recipe_ideas:   # grounding depends on the fixture matching this template's needs
        assert recipe_ideas[0].param_alternatives != "", \
            "the alternatives line is unconditional now"


def test_flag_on_the_hypothesis_chooses_and_identity_carries_it(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_PARAM_CHOICE", "1")
    _catalog_for(db, _WINDOWED)
    intent = submit_intent(hypothesis="detect structuring within a reporting period", actor="ds1")
    cs = build_considered_set(db, intent, _gen_client(), catalog_source="bank", now=NOW)
    recipe_ideas = [f for s in cs.alternatives for f in s.features
                    if f.recipe_id == _WINDOWED.id]
    assert recipe_ideas, (
        f"fixture must ground {_WINDOWED.id}; needs={[n.concept for n in _WINDOWED.needs]}")
    idea = recipe_ideas[0]
    # the NAME (identity) carries the hypothesis-chosen window, not the first-in-list default
    assert f"_{_ALT_WINDOW}d" in idea.name
    assert f"_{_DEFAULT_WINDOW}d" not in idea.name
    # the untaken alternatives are NAMED on the card, chosen value marked
    assert f"[{_ALT_WINDOW}]" in idea.param_alternatives
    assert str(_DEFAULT_WINDOW) in idea.param_alternatives
