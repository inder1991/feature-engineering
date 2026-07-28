"""Raising the stage deadline must actually raise the ceiling.

`stage_deadline_s` and `budget().wallclock_budget_ms` are documented as "one coherent ceiling" — the
per-run budget exists so a single slow chunk cannot trip `over_budget()` and abort the rest of a run
before the stage deadline is even reached. They were two INDEPENDENT env vars that merely shared the
same 240s default, so raising the deadline alone left the wall-clock budget binding at 240s and the
change did nothing.

That is not hypothetical. FTR's concept stage needs 261s for 126 columns and was cut off at 240,
losing six columns' concepts — including `transaction_id`. Raising only the documented knob would
have looked like a fix and changed nothing.

The budget now DERIVES from the deadline unless explicitly set, which makes the docstring's claim
true rather than aspirational.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload import enrich_config


def test_the_two_ceilings_agree_by_default(monkeypatch):
    monkeypatch.delenv("OVERLAY_ENRICH_STAGE_DEADLINE_S", raising=False)
    monkeypatch.delenv("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS", raising=False)
    assert enrich_config.budget("concept").wallclock_budget_ms == \
        int(enrich_config.stage_deadline_s() * 1000)


@pytest.mark.parametrize("deadline", ["600", "1800"])
def test_raising_the_deadline_raises_the_budget_with_it(monkeypatch, deadline):
    """THE property. Setting one knob must not leave the other silently binding at the old value."""
    monkeypatch.setenv("OVERLAY_ENRICH_STAGE_DEADLINE_S", deadline)
    monkeypatch.delenv("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS", raising=False)
    assert enrich_config.budget("concept").wallclock_budget_ms == int(float(deadline) * 1000)


def test_an_explicit_budget_still_wins(monkeypatch):
    """Deriving is a DEFAULT, not a coupling — an operator who sets both means both."""
    monkeypatch.setenv("OVERLAY_ENRICH_STAGE_DEADLINE_S", "1800")
    monkeypatch.setenv("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS", "90000")
    assert enrich_config.budget("concept").wallclock_budget_ms == 90000


def test_the_real_ftr_run_would_now_fit(monkeypatch):
    """126 columns took 260.9s against a 240s ceiling. At the deployed 1800s it fits with room."""
    monkeypatch.setenv("OVERLAY_ENRICH_STAGE_DEADLINE_S", "1800")
    monkeypatch.delenv("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS", raising=False)
    assert enrich_config.stage_deadline_s() > 261
    assert enrich_config.budget("concept").wallclock_budget_ms > 261_000
