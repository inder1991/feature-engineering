"""v3 multi-output honesty — a legacy card never silently inherits one atom's readiness.

The alias RESOLVER refuses to pick an output for a multi-target alias; the v3 SERIALIZER
surveys instead of resolving, so its headline readiness is the best atom's. These tests pin
the honesty contract around that: a multi-output card declares ``output_selection_required``,
carries every replacement's OWN readiness, and the page counts the affected cards — additively,
so nothing byte-frozen moves.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_readiness import READINESS_LADDER
from featuregen.overlay.upload.recipe_registry_v2 import LEGACY_ALIAS_MAP
from featuregen.overlay.upload.suggestion_contract import execution_block_v3

MULTI = next(k for k, v in sorted(LEGACY_ALIAS_MAP.items()) if len(v) > 1)
SINGLE = next(k for k, v in sorted(LEGACY_ALIAS_MAP.items()) if len(v) == 1)


def test_a_multi_output_alias_declares_the_selection_and_every_atoms_own_state():
    block = execution_block_v3(MULTI, "clean")
    targets = LEGACY_ALIAS_MAP[MULTI]
    assert block["output_selection_required"] is True
    assert [r["recipe_id"] for r in block["replacement_readiness"]] == list(targets)
    for row in block["replacement_readiness"]:
        assert row["execution_readiness"] in READINESS_LADDER
        assert row["computation_kind"]
    # The headline is the best atom's state — present in the per-replacement list, never
    # an invention of the serializer.
    assert block["execution_readiness"] in {
        r["execution_readiness"] for r in block["replacement_readiness"]}


def test_a_single_output_alias_requires_no_selection_and_matches_its_one_atom():
    block = execution_block_v3(SINGLE, "clean")
    assert block["output_selection_required"] is False
    assert len(block["replacement_readiness"]) == 1
    assert block["replacement_readiness"][0]["execution_readiness"] \
        == block["execution_readiness"]


def test_the_legacy_fallback_stays_honest_and_selection_free():
    block = execution_block_v3("not_a_template", "clean")
    assert block["output_selection_required"] is False
    assert block["replacement_readiness"] == []
    assert block["execution_readiness"] == "UNASSESSED"


def test_every_multi_target_alias_in_the_registry_declares_selection():
    multi = [k for k, v in LEGACY_ALIAS_MAP.items() if len(v) > 1]
    assert multi                                     # 27 at authoring; never silently zero
    for template_id in multi:
        assert execution_block_v3(template_id, "clean")["output_selection_required"] is True
