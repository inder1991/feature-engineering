"""C-A6 — backward-READABLE, not cross-version RESUMABLE.

The product owner's policy (2026-08-16), as its eight acceptance tests.

**How a legacy run is made.** ``formula_authoring_run`` is WRITE-ONCE by database trigger, so a
legacy manifest cannot be pasted onto an existing run — it has to be the manifest the run was opened
under. That is a better fixture than the one I first reached for: the three version constants are
patched to their pre-bump values, the run is driven to completion, and the patch is removed. The
resulting row is a genuine v1 manifest with a genuine complete trace, which is exactly what is
sitting in the database today.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.formula.test_replay_authoring_v2 import (
    _INTENT,
    _monetary_facts,
    _raw,
    _run,
    _scripted_author,
)

from featuregen.formula.author import run_tool
from featuregen.formula.authoring_v2 import AUTHORING_ORCHESTRATOR_VERSION_V2
from featuregen.formula.authoring_versions import (
    LEGACY_RESTART_REQUIRED,
    VERSION_KEYS,
    BundleClassV2,
    classify_version_bundle,
    legacy_bundle_v1,
)
from featuregen.formula.critic import CriticReview
from featuregen.formula.replay_authoring_v2 import (
    AUTHORING_ORCHESTRATOR_VERSION_V2_REPLAY,
    run_authoring_v2_replay,
)
from featuregen.formula.replay_trace import (
    RecoveryRequiresReconciliation,
    open_authoring_run,
)
from featuregen.formula.result_v2 import DISPOSITION_POLICY_VERSION_V2

_MODULE = "featuregen.formula.replay_authoring_v2"


# ══ the constants moved, and are asserted ════════════════════════════════════════════════════════
def test_the_three_constants_are_at_VERSION_2():
    """C-A6's gate: "version constants moved and asserted"."""
    assert DISPOSITION_POLICY_VERSION_V2 == 2
    assert AUTHORING_ORCHESTRATOR_VERSION_V2 == 2
    assert AUTHORING_ORCHESTRATOR_VERSION_V2_REPLAY == 2


# ══ classification ═══════════════════════════════════════════════════════════════════════════════
def _current() -> dict:
    return {"orchestrator": 2, "formula_schema": 2, "operation_grammar": 7, "critic": 3,
            "disposition": 2, "authoring_v2": 2, "frozen_configuration_policy": 1}


def test_the_current_bundle_classifies_CURRENT():
    assert classify_version_bundle(_current(), current=_current()) is BundleClassV2.CURRENT


def test_the_pre_bump_bundle_classifies_LEGACY():
    assert classify_version_bundle(
        legacy_bundle_v1(_current()), current=_current()) is BundleClassV2.LEGACY


@pytest.mark.parametrize("key", ["orchestrator", "disposition", "authoring_v2"])
def test_a_PARTIAL_match_refuses(key):
    """Acceptance #5. The key it differs on is precisely the one that decided something
    differently, so picking an adapter by majority vote would run a trace under software that did
    not write it."""
    assert classify_version_bundle(
        {**_current(), key: 1}, current=_current()) is BundleClassV2.UNKNOWN


def test_an_unknown_or_absent_bundle_refuses():
    assert classify_version_bundle(None, current=_current()) is BundleClassV2.UNKNOWN
    assert classify_version_bundle({}, current=_current()) is BundleClassV2.UNKNOWN
    assert classify_version_bundle({"orchestrator": 99}, current=_current()) is BundleClassV2.UNKNOWN


def test_a_differing_NON_bumped_key_is_also_unknown():
    """`operation_grammar` moving is genuinely a different software version, not a legacy run."""
    assert classify_version_bundle(
        {**legacy_bundle_v1(_current()), "operation_grammar": 99},
        current=_current()) is BundleClassV2.UNKNOWN


def test_the_per_run_frozen_config_HASH_is_not_a_version():
    """Including it would classify every run with a different frozen configuration as different
    software. The full-identity comparison that DOES include it still happens in
    `load_verified_checkpoint`."""
    assert "frozen_configuration_hash" not in VERSION_KEYS


# ══ fixtures over a real trace ═══════════════════════════════════════════════════════════════════
def _no_provider(monkeypatch) -> None:
    """Drive the loop deterministically — no provider, so a stray call is a loud failure."""
    monkeypatch.setattr(f"{_MODULE}.author_formula", _scripted_author(_raw()))
    monkeypatch.setattr(f"{_MODULE}.critique",
                        lambda *a, **k: CriticReview((), "critic_hash_v2", False, None, 1, {}))


def _as_version_1(monkeypatch) -> None:
    """Make this build write the PRE-BUMP manifest, so the run row is a genuine legacy one."""
    monkeypatch.setattr(f"{_MODULE}.AUTHORING_ORCHESTRATOR_VERSION_V2_REPLAY", 1)
    monkeypatch.setattr(f"{_MODULE}.AUTHORING_ORCHESTRATOR_VERSION_V2", 1)
    monkeypatch.setattr(f"{_MODULE}.DISPOSITION_POLICY_VERSION_V2", 1)


def _stored(db, run_id: str) -> dict:
    return dict(db.execute(
        "SELECT versions FROM formula_authoring_run WHERE authoring_run_id=%s",
        (run_id,)).fetchone()[0])


def _trace_bytes(db, run_id: str) -> list[tuple]:
    return db.execute(
        "SELECT seq, stage, idempotency_key, payload FROM formula_authoring_trace_event "
        "WHERE authoring_run_id=%s ORDER BY seq", (run_id,)).fetchall()


def _real_intent_hash() -> str:
    """The intent hash the orchestrator itself computes — so an incomplete-legacy fixture differs
    from a real run in its VERSION BUNDLE only, which is the variable under test."""
    from featuregen.formula.replay_authoring_v2 import _intent_material
    from featuregen.overlay.field_evidence import canonical_hash

    return canonical_hash(_intent_material(_INTENT))


def _explode(*args, **kwargs):
    raise AssertionError("a legacy read-only replay must not call a provider")


def _real_bundle(db, monkeypatch, *, probe_id: str) -> dict:
    """The bundle THIS BUILD actually writes, read back from a real run.

    Hand-writing one would test the fixture rather than the code: the real bundle omits
    `frozen_configuration_policy` when no frozen configuration is supplied, and carries the live
    `critic` and `operation_grammar` values.
    """
    _no_provider(monkeypatch)
    _run(db, run_id=probe_id)
    return _stored(db, probe_id)


# ══ acceptance #1, #2, #7 — completed legacy replays identically, read-only ══════════════════════
def test_A_COMPLETED_VERSION_1_TRACE_REPLAYS_TO_THE_IDENTICAL_RESULT(db, monkeypatch):
    """Acceptance #1."""
    run_id = "far_ca6_legacy_ok"
    with monkeypatch.context() as legacy:
        _no_provider(legacy)
        _as_version_1(legacy)
        first = _run(db, run_id=run_id)
    assert _stored(db, run_id)["orchestrator"] == 1, "a genuine v1 manifest"

    _no_provider(monkeypatch)
    replayed = _run(db, run_id=run_id)
    assert replayed == first


def test_LEGACY_REPLAY_MAKES_NO_PROVIDER_CALL_AND_LEAVES_THE_BYTES_UNTOUCHED(db, monkeypatch):
    """Acceptance #2 and #7 — the promise everything else rests on."""
    run_id = "far_ca6_legacy_readonly"
    with monkeypatch.context() as legacy:
        _no_provider(legacy)
        _as_version_1(legacy)
        _run(db, run_id=run_id)
    before = _trace_bytes(db, run_id)

    monkeypatch.setattr(f"{_MODULE}.author_formula", _explode)
    monkeypatch.setattr(f"{_MODULE}.critique", _explode)
    _run(db, run_id=run_id)

    assert _trace_bytes(db, run_id) == before, "stored trace events are never rewritten"


# ══ acceptance #3 — an incomplete legacy run refuses ═════════════════════════════════════════════
def test_AN_INTERRUPTED_VERSION_1_TRACE_REFUSES_TO_RESUME(db, monkeypatch):
    """Acceptance #3. Resuming would append new-orchestrator events — including REVIEW_BYPASSED,
    which the old stage table never allowed — under a manifest saying version 1 decided this run."""
    run_id = "far_ca6_legacy_incomplete"
    legacy = legacy_bundle_v1(_real_bundle(db, monkeypatch, probe_id="far_ca6_probe_a"))
    open_authoring_run(db, intent_hash=_real_intent_hash(), versions=legacy, actor=None,
                       authoring_run_id=run_id)
    before = _trace_bytes(db, run_id)

    _no_provider(monkeypatch)
    with pytest.raises(RecoveryRequiresReconciliation, match=LEGACY_RESTART_REQUIRED):
        _run(db, run_id=run_id)

    assert _trace_bytes(db, run_id) == before, "a refusal writes nothing either"


# ══ acceptance #4 — a current trace resumes normally ═════════════════════════════════════════════
def test_A_VERSION_2_TRACE_RESUMES_NORMALLY(db, monkeypatch):
    """Acceptance #4."""
    _no_provider(monkeypatch)
    first = _run(db, run_id="far_ca6_current")
    assert _stored(db, "far_ca6_current")["disposition"] == DISPOSITION_POLICY_VERSION_V2

    monkeypatch.setattr(f"{_MODULE}.author_formula", _explode)
    monkeypatch.setattr(f"{_MODULE}.critique", _explode)
    assert _run(db, run_id="far_ca6_current") == first


# ══ acceptance #5 — unknown and mixed bundles refuse ═════════════════════════════════════════════
@pytest.mark.parametrize("mutation,expected", [
    ({"orchestrator": 99}, "does not recognise"),
    ({"disposition": 1}, "partial match is not close enough"),
    ({"operation_grammar": 99}, "does not recognise"),
])
def test_UNKNOWN_AND_MIXED_BUNDLES_REFUSE(db, monkeypatch, mutation, expected):
    """Acceptance #5, end to end. The run row is write-once, so these are OPENED under the odd
    bundle rather than edited into one."""
    run_id = f"far_ca6_odd_{'_'.join(mutation)}"
    real = _real_bundle(db, monkeypatch, probe_id=f"far_ca6_probe_{'_'.join(mutation)}")
    open_authoring_run(db, intent_hash=_real_intent_hash(), versions={**real, **mutation},
                       actor=None, authoring_run_id=run_id)
    with pytest.raises(RecoveryRequiresReconciliation, match=expected):
        _run(db, run_id=run_id)


# ══ acceptance #6 — a changed intent still refuses ═══════════════════════════════════════════════
def test_A_CHANGED_INTENT_STILL_REFUSES_ON_THE_LEGACY_PATH(db, monkeypatch):
    """Acceptance #6. The legacy adapter verifies against the manifest it was written under, but
    the intent hash is still compared — a legacy trace is not a licence to replay any intent."""
    run_id = "far_ca6_legacy_intent"
    with monkeypatch.context() as legacy:
        _no_provider(legacy)
        _as_version_1(legacy)
        _run(db, run_id=run_id)

    _no_provider(monkeypatch)
    other = type(_INTENT)("a different question", _INTENT.hypothesis, _INTENT.target_entity,
                          target_grain_keys=_INTENT.target_grain_keys)
    with pytest.raises(RecoveryRequiresReconciliation, match="identity changed"):
        run_authoring_v2_replay(
            db, other, None, None, actor=None, authoring_run_id=run_id,
            facts_reader=_monetary_facts, tool_runner=run_tool,
            critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref})


# ══ acceptance #8 — a V3 run records schema 3 ════════════════════════════════════════════════════
def test_A_V3_RUN_RECORDS_SCHEMA_VERSION_3(db, monkeypatch):
    """It must not inherit the previously hardcoded `2`: the manifest would say a v2 formula was
    authored, and every later reader keyed on it would agree."""
    _no_provider(monkeypatch)
    _run(db, run_id="far_ca6_v3", formula_schema_version=3)
    assert _stored(db, "far_ca6_v3")["formula_schema"] == 3


def test_a_v2_run_still_records_schema_version_2(db, monkeypatch):
    _no_provider(monkeypatch)
    _run(db, run_id="far_ca6_v2")
    assert _stored(db, "far_ca6_v2")["formula_schema"] == 2


def test_a_v3_run_and_a_v2_run_are_DIFFERENT_bundles(db, monkeypatch):
    """So a v3 run can never be mistaken for a legacy v2 one — `formula_schema` is a version key."""
    _no_provider(monkeypatch)
    _run(db, run_id="far_ca6_v3b", formula_schema_version=3)
    _run(db, run_id="far_ca6_v2b")
    assert classify_version_bundle(
        _stored(db, "far_ca6_v3b"), current=_stored(db, "far_ca6_v2b")) is BundleClassV2.UNKNOWN


# ══ the manifest itself is write-once, which the policy depends on ══════════════════════════════
def test_THE_RUN_MANIFEST_IS_WRITE_ONCE_IN_THE_DATABASE(db, monkeypatch):
    """Not merely a convention this code follows. Everything above assumes the stored bundle is what
    decided the run; if a manifest could be edited, an incomplete legacy run could be relabelled
    current and resumed — the exact thing the policy forbids."""
    import psycopg

    _no_provider(monkeypatch)
    _run(db, run_id="far_ca6_writeonce")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute("UPDATE formula_authoring_run SET versions=%s::jsonb WHERE authoring_run_id=%s",
                   (json.dumps(legacy_bundle_v1(_current())), "far_ca6_writeonce"))


# ══ the new stage is legal exactly where the critic's was ════════════════════════════════════════
def test_REVIEW_BYPASSED_is_an_ALTERNATIVE_to_the_critic_stage_not_a_successor():
    """A run either ran the critic or stood on a reviewed blueprint; a trace carrying both would
    claim two different reviews of one formula."""
    import inspect

    from featuregen.formula import replay_trace

    table = inspect.getsource(replay_trace)
    assert '"REVIEW_BYPASSED": lambda value: value == "EXPECTATION_VALIDATED"' in table
    assert '"OUTPUT_POLICY_RESOLVED": lambda value: value in {' in table
    assert '"CRITIC_COMPLETED", "REVIEW_BYPASSED"' in table
