"""The deployed LLM bounds, and the arithmetic that makes them safe.

**Why a test and not a comment.** `FEATUREGEN_LLM_TIMEOUT` is not a number that can be read on its
own. What it actually costs is `timeout x (physical calls in one logical call) x (escalation scale)
x (SDK internal retries)`, and every one of those four factors lives in a DIFFERENT module from the
manifest that sets the timeout:

* the physical-call count is `llm.DEFAULT_RETRY_BUDGET` + `llm.DEFAULT_REPAIR_BUDGET`,
* the escalation scale is `llm._TRUNCATION_ESCALATION` clamped by `llm._MAX_TOKENS_CEILING`,
  measured against the manifest's own `FEATUREGEN_LLM_MAX_TOKENS`,
* the SDK multiplier is `llm_claude._SDK_MAX_RETRIES`.

So a reviewer can approve any one of those five edits in isolation without ever seeing the product.
:func:`test_the_worst_case_logical_call_is_the_number_the_manifest_claims` recomputes the product
from the live constants and pins it, which is the only thing that fails when someone restores the
SDK's default `max_retries`, widens a budget, or lowers `FEATUREGEN_LLM_MAX_TOKENS` "to save money".

**The inversion worth failing over.** Lowering `FEATUREGEN_LLM_MAX_TOKENS` RAISES the worst case: a
smaller ceiling escalates through more doublings before it clamps, and the clock scales with it.
That is exactly the edit an operator reaches for to reduce spend, and it is the one that lengthens
how long a hung chain holds the source advisory lock.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from featuregen.intake.llm import (
    _MAX_TOKENS_CEILING,
    _TRUNCATION_ESCALATION,
    DEFAULT_REPAIR_BUDGET,
    DEFAULT_RETRY_BUDGET,
    transient_backoff_s,
)
from featuregen.intake.llm_claude import _SDK_MAX_RETRIES

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ENV_EXAMPLE = _ROOT / ".env.example"
_KIND_BACKEND = _ROOT / "deploy" / "kind" / "k8s" / "20-backend.yaml"

#: The values this deployment ships. Task 4's brief fixes all three; they are restated here so a
#: silent revert in the manifest is a red test rather than a quieter cluster.
#: `OVERLAY_ENRICH_MAX_PROVIDER_CALLS` was raised 100 -> 512 by Task 4b (the zero-truncation cap
#: raise), and :func:`test_the_call_ceiling_cannot_bind_before_the_wall_clock_does` below derives
#: that number rather than restating it.
_EXPECTED = {
    "FEATUREGEN_FEATURE_CONTEXT": "1",
    "OVERLAY_ENRICH_MAX_PROVIDER_CALLS": "512",
    "FEATUREGEN_LLM_TIMEOUT": "300",
}

#: The largest catalog this repo exercises end-to-end: the `wide_catalogs` fixture in
#: `tests/featuregen/overlay/upload/test_feature_context_budget.py` — 126 real FTR glossary columns
#: plus 111 CIB-shaped technical columns.
_LARGEST_CATALOG_ITEMS = 237

#: The worst-case wall clock of ONE logical call at the deployed configuration, in seconds, as
#: computed by :func:`_worst_case_logical_call_seconds` and asserted below. Written out so the
#: number in the manifest comment and the number the code produces cannot drift apart silently.
#: 2700s of provider ceilings + 2s of bounded transient backoff.
_EXPECTED_WORST_CASE_S = 2702.0


@pytest.fixture(scope="module")
def config_map() -> dict[str, str]:
    documents = yaml.safe_load_all(_KIND_BACKEND.read_text(encoding="utf-8"))
    return next(d for d in documents if d.get("kind") == "ConfigMap")["data"]


def _worst_case_logical_call_seconds(timeout: float, max_tokens: int) -> float:
    """Replay `drive_structured_call`'s longest possible chain, in wall-clock seconds.

    Faithful to `llm._escalated`: a truncation retry raises `max_tokens` by
    `_TRUNCATION_ESCALATION`, clamped at `_MAX_TOKENS_CEILING`, and scales the per-attempt clock by
    the ratio ACTUALLY applied (so the attempt that lands on the cap gets a partial raise, and
    exactly that much more time). Once the cap is reached the scale stops growing but the remaining
    attempts still run at it — including repairs, which retain the escalated ceiling.

    The chain is `1 initial + DEFAULT_RETRY_BUDGET retries + DEFAULT_REPAIR_BUDGET repairs`, and
    every physical attempt is multiplied by the SDK's own internal retry count.
    """
    scale, current, backoff = 1.0, max_tokens, 0.0
    total = timeout * scale                              # the initial attempt
    for retry_no in range(1, DEFAULT_RETRY_BUDGET + 1):
        raised = min(int(current * _TRUNCATION_ESCALATION), _MAX_TOKENS_CEILING)
        if raised > current:
            scale *= raised / current                    # a truncation retry: escalates, no wait
            current = raised
        else:
            # This retry can no longer escalate the clock, so the LONGEST chain spends it on the
            # PROVIDER_TRANSIENT class instead — same per-attempt ceiling, plus bounded backoff.
            # Choosing per retry is what makes this an upper bound over the class mix rather than
            # over one assumed sequence.
            backoff += transient_backoff_s(retry_no)
        total += timeout * scale
    total += DEFAULT_REPAIR_BUDGET * timeout * scale     # repairs keep the escalated ceiling/clock
    # Backoff is OURS, inside the driver loop — the SDK multiplier applies to provider attempts only.
    return total * (_SDK_MAX_RETRIES + 1) + backoff


# ── the values themselves ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("key", "value"), sorted(_EXPECTED.items()))
def test_the_manifest_ships_the_governed_value(config_map, key: str, value: str) -> None:
    """A config change with no test is how these revert. `FEATUREGEN_FEATURE_CONTEXT` in particular
    is the switch that makes `_feature_schema_version()` return 4 and activates the entire v4
    feature-generation payload — flipping it back is a functional change wearing a one-character
    diff."""
    assert config_map[key] == value


def test_the_v3_rollback_lever_is_ABSENT_so_the_flag_means_v4(config_map) -> None:
    """The D8 ladder: flag on + no version override = v4. If a `FEATUREGEN_FEATURE_CONTEXT_VERSION`
    key ever appears in `data` it silently downgrades the contract this task exists to turn on, so
    the key's ABSENCE is part of the configuration, not an accident of it."""
    assert "FEATUREGEN_FEATURE_CONTEXT_VERSION" not in config_map


# ── the arithmetic ───────────────────────────────────────────────────────────────────────────────

def test_the_SDK_retry_layer_is_DISABLED() -> None:
    """The single highest-leverage factor. The Anthropic SDK defaults `max_retries` to 2 and retries
    APITimeoutError internally, so leaving it alone multiplies EVERY per-attempt ceiling by 3 —
    silently, inside one `messages.create`, while the source advisory lock is held. It also
    contradicts the adapter's own APITimeoutError arm, which refuses to re-issue a timed-out request
    identically. Restoring the default triples the bound asserted below."""
    assert _SDK_MAX_RETRIES == 0


def test_the_worst_case_logical_call_is_the_number_the_manifest_claims(config_map) -> None:
    """THE bound. Recomputed from the live driver constants and the manifest's own values, so it
    fails on a change to ANY of the five inputs rather than only on an edit to the timeout."""
    worst_case = _worst_case_logical_call_seconds(
        timeout=float(config_map["FEATUREGEN_LLM_TIMEOUT"]),
        max_tokens=int(config_map["FEATUREGEN_LLM_MAX_TOKENS"]),
    )
    assert worst_case == _EXPECTED_WORST_CASE_S
    assert str(int(_EXPECTED_WORST_CASE_S)) in _KIND_BACKEND.read_text(encoding="utf-8"), (
        "the manifest comment must state the bound it is configured to; it is the only place an "
        "operator editing these values will look")


def test_LOWERING_max_tokens_RAISES_the_worst_case(config_map) -> None:
    """The inversion, pinned because it is counter-intuitive and because reducing `max_tokens` is
    the obvious cost-saving edit. At the 4096 code default the escalation gets two full doublings
    instead of one partial step, so the scale reaches 4x and the chain runs 1.67x longer than the
    deployed configuration's."""
    timeout = float(config_map["FEATUREGEN_LLM_TIMEOUT"])
    deployed = _worst_case_logical_call_seconds(timeout, int(config_map["FEATUREGEN_LLM_MAX_TOKENS"]))
    at_code_default = _worst_case_logical_call_seconds(timeout, 4096)

    assert at_code_default == 4500.0
    assert at_code_default > deployed


def test_the_stage_deadline_CANNOT_bound_a_call_already_in_flight(config_map) -> None:
    """A deliberately uncomfortable assertion, recording a known and accepted overshoot rather than
    letting it be rediscovered in an incident.

    `OVERLAY_ENRICH_STAGE_DEADLINE_S` is checked only BEFORE issuing each new chunk
    (`enrich_batch.run_batched`), so a chunk issued one second under the deadline runs its full
    chain past it, holding the source advisory lock. The worst-case chain is LARGER than the stage
    deadline, which means the effective worst-case lock hold is deadline + chain, not deadline.

    If a future change makes the chain fit inside the deadline, this test fails — and the right
    response is to delete it and the manifest paragraph it guards, not to loosen it.
    """
    deadline = float(config_map["OVERLAY_ENRICH_STAGE_DEADLINE_S"])
    worst_case = _worst_case_logical_call_seconds(
        timeout=float(config_map["FEATUREGEN_LLM_TIMEOUT"]),
        max_tokens=int(config_map["FEATUREGEN_LLM_MAX_TOKENS"]),
    )
    assert worst_case > deadline
    assert deadline + worst_case == 4502.0      # the real worst-case lock hold, stated once


def test_the_call_ceiling_cannot_bind_before_the_wall_clock_does(config_map, monkeypatch) -> None:
    """The ceiling is a RUNAWAY BACKSTOP, and the distinction is the whole point of this test.

    `enrich_config`'s own chunking note: an item that alone exceeds the token budget still forms its
    own chunk, so an under-budgeted bound degrades PROPORTIONALLY — fewer items per chunk, more
    provider calls — never into a lost item. `max_provider_calls` is the ONE thing that converts
    "more calls" into "stopped enriching columns". Task 4b raised every prose and item cap, which is
    exactly the change that makes items bigger and chunks smaller, so if this ceiling can bind on a
    legitimate catalog the raise silently truncates enrichment instead of merely costing more.

    Derived, not measured — a live before/after ingest was not authorised (see DEFERRED-WORK). The
    degenerate case is one item per chunk, so for a column-scoped stage the chunk count equals the
    column count.

    The budget is read through `enrich_config` under the MANIFEST's own environment rather than from
    a literal, so this tracks the deployment instead of a copy of it.
    """
    from featuregen.overlay.upload import enrich_config

    for key, value in config_map.items():
        if key.startswith("OVERLAY_ENRICH_"):
            monkeypatch.setenv(key, value)

    b = enrich_config.budget("concept")
    worst_case = _LARGEST_CATALOG_ITEMS * b.max_batch_attempts + b.max_single_fallback
    assert b.max_provider_calls > worst_case, (
        f"{b.max_provider_calls} can bind at {worst_case} worst-case calls — enrichment would "
        f"truncate. Raise OVERLAY_ENRICH_MAX_PROVIDER_CALLS or lower the item caps.")


def test_the_ceiling_is_PER_STAGE_not_per_upload() -> None:
    """The arithmetic above is only sound if each stage gets its own ledger. `run_batched` builds
    `CallLedger(b.max_provider_calls)` per invocation unless a caller passes a shared one (only Pass
    B does, to make its critic's nested calls spend from the same stage ceiling), so N stages over
    one upload may spend N x the ceiling. Pinned because a change to per-RUN scoping would make the
    derived 512 wrong in the expensive direction without failing anything else."""
    import inspect

    from featuregen.overlay.upload import enrich_batch

    source = inspect.getsource(enrich_batch.run_batched)
    assert "CallLedger(b.max_provider_calls)" in source, (
        "run_batched no longer builds its own per-invocation ledger — re-derive the ceiling")


# ── both deployment files ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variable", ["FEATUREGEN_LLM_TIMEOUT", "OVERLAY_ENRICH_MAX_PROVIDER_CALLS",
                                      "OVERLAY_ENRICH_STAGE_DEADLINE_S"])
@pytest.mark.parametrize("path", [_ENV_EXAMPLE, _KIND_BACKEND])
def test_every_bound_is_DOCUMENTED_in_both_deployment_files(path: pathlib.Path,
                                                            variable: str) -> None:
    """Mirrors the materialization lane's precedent. These three interact — the per-call clock, the
    call count, and the stage deadline — and a deployer who finds only two of them in the file they
    edit will change one without the others."""
    assert variable in path.read_text(encoding="utf-8")
