"""BR-4 — PIT text is rendered from the typed contract; a hole is a NAMED blocker, never worse
prose. The four banking time shapes render distinctly and cannot share a declaration; real-time
wording is EARNED (governed pre-decision authority + minute grain); a correctable source must
declare knowledge time; and no rendered text can carry a placeholder — the renderer asserts it.
"""
from __future__ import annotations

from dataclasses import replace

from featuregen.overlay.upload.recipe_registry_v2 import PROBE_RECIPE
from featuregen.overlay.upload.recipe_temporal_v2 import (
    BLOCKER_EVENT_ROLE_UNBOUND,
    BLOCKER_KNOWLEDGE_TIME_MISSING,
    BLOCKER_PRE_DECISION_AUTHORITY_UNPROVEN,
    BLOCKER_PRE_DECISION_NOT_MINUTE_GRAINED,
    BLOCKER_SNAPSHOT_POLICY_MISSING,
    BLOCKER_WINDOW_UNBOUND,
    compile_temporal,
)
from featuregen.overlay.upload.taxonomy.ranking_signals import (
    PITCompleteness,
    pit_completeness_v2,
)


def _temporal(**overrides):
    return replace(PROBE_RECIPE.temporal, **overrides)


def test_the_default_variant_compiles_placeholder_free_with_the_canonical_token():
    compiled = compile_temporal(PROBE_RECIPE)
    assert compiled.status == "compiled"
    assert compiled.window_token == "30d"
    assert "{" not in compiled.pit_text and "}" not in compiled.pit_text
    assert "trailing 30d observation window" in compiled.pit_text
    assert "event_ts" in compiled.pit_text


def test_a_different_selection_renders_a_different_window():
    compiled = compile_temporal(PROBE_RECIPE, selection={"window": 90})
    assert compiled.window_token == "90d"
    assert "90d" in compiled.pit_text


def test_an_undeclared_temporal_parameter_cannot_compile():
    """The mismatch class behind BOTH audited defects: a window the selection cannot bind is a
    NAMED blocker, never prose with a hole."""
    compiled = compile_temporal(PROBE_RECIPE, selection={})
    assert compiled.status == "blocked"
    assert BLOCKER_WINDOW_UNBOUND in compiled.blockers
    assert compiled.pit_text == "", "a blocked contract renders NOTHING rather than something wrong"


def test_the_four_time_shapes_render_distinctly():
    event = compile_temporal(PROBE_RECIPE).pit_text
    as_of = compile_temporal(replace(
        PROBE_RECIPE, temporal=_temporal(anchor_kind="as_of",
                                         snapshot_policy="latest-known-at-cutoff"))).pit_text
    future = compile_temporal(replace(
        PROBE_RECIPE, temporal=_temporal(anchor_kind="contractual_future",
                                         future_horizon_policy="contractual-maturity-terms"),
    )).pit_text
    assert "trailing" in event and "FORWARD" not in event
    assert "latest known state" in as_of and "never forward-looking" in as_of
    # future contractual maturity is NEVER represented as a past trailing window
    assert "FORWARD contractual horizon" in future
    assert "(cutoff, cutoff + 30d]" in future
    assert "never a trailing observation window" in future
    assert len({event, as_of, future}) == 3


def test_real_time_wording_is_earned_never_assumed():
    """The merchant_mcc_diversity rule: pre-decision needs a governed feed authority AND minute
    grain — batch day-windows cannot claim to run before the authorization."""
    batch_claiming_realtime = compile_temporal(replace(
        PROBE_RECIPE, temporal=_temporal(anchor_kind="pre_decision")))
    assert batch_claiming_realtime.status == "blocked"
    assert BLOCKER_PRE_DECISION_AUTHORITY_UNPROVEN in batch_claiming_realtime.blockers
    assert BLOCKER_PRE_DECISION_NOT_MINUTE_GRAINED in batch_claiming_realtime.blockers

    minutes = replace(PROBE_RECIPE.parameters[0], allowed_values=(60, 15),
                      identity_projection="window={value}min",
                      display_projection="{value}-minute window")
    genuine = compile_temporal(replace(
        PROBE_RECIPE, parameters=(minutes,),
        temporal=_temporal(anchor_kind="pre_decision", window_unit="minutes",
                           temporal_authority_ref="feed:card-auth-stream")))
    assert genuine.status == "compiled"
    assert "strictly BEFORE the" in genuine.pit_text
    assert "feed:card-auth-stream" in genuine.pit_text
    assert genuine.window_token == "60min"


def test_missing_anchor_pieces_are_named_blockers():
    unbound_event = compile_temporal(replace(
        PROBE_RECIPE, temporal=_temporal(event_time_role="no_such_role")))
    assert BLOCKER_EVENT_ROLE_UNBOUND in unbound_event.blockers
    bare_snapshot = compile_temporal(replace(
        PROBE_RECIPE, temporal=_temporal(anchor_kind="as_of", snapshot_policy="")))
    assert BLOCKER_SNAPSHOT_POLICY_MISSING in bare_snapshot.blockers


def test_a_correctable_source_must_declare_knowledge_time():
    silent = compile_temporal(PROBE_RECIPE, correctable_source=True)
    assert silent.status == "blocked"
    assert BLOCKER_KNOWLEDGE_TIME_MISSING in silent.blockers
    declared = compile_temporal(replace(
        PROBE_RECIPE, temporal=_temporal(knowledge_time_role="ingested_at",
                                         late_arrival_policy="exclude-after-cutoff")),
        correctable_source=True)
    assert declared.status == "compiled"
    assert "knowledge time per ingested_at" in declared.pit_text
    assert "late arrivals per exclude-after-cutoff" in declared.pit_text


def test_pit_completeness_v2_consumes_the_compiler_never_keywords():
    """COMPLETE is structurally unreachable while any blocker exists — the acceptance rule."""
    assert pit_completeness_v2(compile_temporal(PROBE_RECIPE)) is PITCompleteness.COMPLETE
    blocked = compile_temporal(PROBE_RECIPE, selection={})
    assert pit_completeness_v2(blocked) is PITCompleteness.PARTIAL
    # keyword markers carry no weight: a compiled text is COMPLETE whatever words it uses, and a
    # blocked one is PARTIAL even though its (empty) text contains no marker at all
    assert blocked.pit_text == ""
