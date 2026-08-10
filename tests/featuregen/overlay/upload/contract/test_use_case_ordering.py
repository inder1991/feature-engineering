"""TASK 4 — rank by the taxonomy that already exists; do not filter.

The whole ranking step is deterministic set intersection over the intake build's SIGNED
``business_domain`` versus each template's ``use_cases ∪ {family}`` — no model anywhere in the
ordering (the mapping call rode intake). THE RULE it encodes: the hypothesis may remove a recipe
only for being UNSAFE, never for being irrelevant — relevance is ORDER, safety is REMOVAL. The sort
is stable, so an unmappable hypothesis (nothing signed, or zero overlap) provably falls back to
today's registry order byte-for-byte. Shadow counters always fire (log-and-compare); the order is
APPLIED only under ``FEATUREGEN_USE_CASE_ORDERING``.
"""
from __future__ import annotations

from featuregen.overlay.upload.contract.gate1 import order_ideas_by_use_case
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.templates import ALL_TEMPLATES
from featuregen.runtime.observability import counters

# Two REAL registry templates whose use_cases diverge: dormancy_days carries "engagement";
# balance-slope style cards carry "deposit_attrition" — found from the registry, not invented.
_DORMANCY = next(t for t in ALL_TEMPLATES if t.id == "dormancy_days")
_OTHER = next(t for t in ALL_TEMPLATES
              if "engagement" not in t.use_cases and t.family != _DORMANCY.family)


def _idea(template_id: str, name: str) -> FeatureIdea:
    return FeatureIdea(name=name, description="", derives_from=[], aggregation=None,
                       grain_table=None, generation_source="recipe", recipe_id=template_id)


def _count(name: str) -> int:
    return counters.snapshot()["counters"].get(name, 0)


_IDEAS = [_idea(_OTHER.id, "other_first"), _idea(_DORMANCY.id, "dormancy_second")]


def test_shadow_counts_the_would_be_change_and_applies_nothing_by_default(monkeypatch):
    monkeypatch.delenv("FEATUREGEN_USE_CASE_ORDERING", raising=False)
    before = _count("overlay.use_case_order.changed")
    out = order_ideas_by_use_case(_IDEAS, ("engagement",))
    assert [i.name for i in out] == ["other_first", "dormancy_second"], \
        "flag off: today's order stands byte-identically"
    assert _count("overlay.use_case_order.changed") == before + 1, \
        "…but the would-be change was counted (log-and-compare)"


def test_flag_on_orders_by_signed_domain_and_removes_nothing(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_USE_CASE_ORDERING", "1")
    out = order_ideas_by_use_case(_IDEAS, ("engagement",))
    assert [i.name for i in out] == ["dormancy_second", "other_first"], \
        "the engagement-signed hypothesis surfaces the engagement recipe first"
    assert {i.name for i in out} == {i.name for i in _IDEAS}, "ORDERS, never removes"


def test_an_unmappable_hypothesis_provably_falls_back_to_todays_order(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_USE_CASE_ORDERING", "1")
    before = _count("overlay.use_case_order.unmappable")
    # nothing signed
    assert [i.name for i in order_ideas_by_use_case(_IDEAS, ())] \
        == ["other_first", "dormancy_second"]
    # signed but zero overlap with any grounded template
    assert [i.name for i in order_ideas_by_use_case(_IDEAS, ("a_domain_no_template_carries",))] \
        == ["other_first", "dormancy_second"]
    assert _count("overlay.use_case_order.unmappable") == before + 2


def test_equal_overlap_keeps_registry_order_stable(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_USE_CASE_ORDERING", "1")
    # both share retail_churn → equal overlap → the stable sort preserves today's order
    both = [_idea(_DORMANCY.id, "a"), _idea(_DORMANCY.id, "b")]
    assert [i.name for i in order_ideas_by_use_case(both, ("retail_churn",))] == ["a", "b"]


def test_an_llm_origin_idea_has_no_template_and_never_gains_rank(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_USE_CASE_ORDERING", "1")
    llm = FeatureIdea(name="freeform", description="", derives_from=[], aggregation=None,
                      grain_table=None)   # recipe_id None — overlap 0 by construction
    out = order_ideas_by_use_case([llm, _idea(_DORMANCY.id, "dormancy")], ("engagement",))
    assert [i.name for i in out] == ["dormancy", "freeform"]
