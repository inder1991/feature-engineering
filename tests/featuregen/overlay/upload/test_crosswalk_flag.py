"""``FEATUREGEN_CROSSWALK_EXECUTION`` — the widened truthy set, the three legal states, and the ONE
combination that must REFUSE TO BOOT (verified-interfaces D8, Release-C row).

The refusal is the point of this suite. Release B's dependency ships as a startup WARNING that never
blocks boot; D8's 2026-08-03 note says Release C must not inherit that precedent, so
``test_the_illegal_combination_refuses_to_boot`` asserts an exception out of the real application
lifespan rather than a log line.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from featuregen.overlay.upload.crosswalk_flag import (
    CROSSWALK_EXECUTION_FLAG,
    CrosswalkConfigurationError,
    crosswalk_execution_enabled,
    crosswalk_execution_status,
    require_valid_crosswalk_configuration,
)

_DEPENDENCIES = ("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "FEATUREGEN_DATASET_PROFILES")


@pytest.fixture(autouse=True)
def clean_flags(monkeypatch):
    for name in (CROSSWALK_EXECUTION_FLAG, *_DEPENDENCIES):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _enable_dependency(monkeypatch) -> None:
    for name in _DEPENDENCIES:
        monkeypatch.setenv(name, "1")


# ── the reader ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_the_widened_truthy_set_is_read(clean_flags, value) -> None:
    _enable_dependency(clean_flags)
    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, value)
    assert crosswalk_execution_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "2", "enabled"])
def test_everything_else_is_off(clean_flags, value) -> None:
    _enable_dependency(clean_flags)
    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, value)
    assert crosswalk_execution_enabled() is False


def test_default_is_off(clean_flags) -> None:
    _enable_dependency(clean_flags)
    assert crosswalk_execution_enabled() is False


def test_the_reader_is_read_on_every_call_not_captured_at_import(clean_flags) -> None:
    _enable_dependency(clean_flags)
    assert crosswalk_execution_enabled() is False
    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, "1")
    assert crosswalk_execution_enabled() is True
    clean_flags.delenv(CROSSWALK_EXECUTION_FLAG)
    assert crosswalk_execution_enabled() is False


def test_the_dependency_is_the_features_effective_state_not_the_raw_variable(clean_flags) -> None:
    """`SOURCE_TEMPORAL=1` with `DATASET_PROFILES` off leaves that feature DISABLED, so a crosswalk
    riding on the raw variable would ride on a feature that is not running."""
    clean_flags.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")
    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, "1")
    status = crosswalk_execution_status()
    assert status.requested is True
    assert status.dependency_requested is True
    assert status.dependency_met is False
    assert status.enabled is False
    assert status.configuration_valid is False


def test_the_reader_fails_closed_even_where_the_boot_check_was_skipped(clean_flags) -> None:
    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, "1")
    assert crosswalk_execution_enabled() is False


# ── the three legal states ───────────────────────────────────────────────────────────────────────


def test_both_off_is_legal(clean_flags) -> None:
    require_valid_crosswalk_configuration()
    assert crosswalk_execution_status().enabled is False


def test_dependency_on_crosswalk_off_is_legal(clean_flags) -> None:
    _enable_dependency(clean_flags)
    require_valid_crosswalk_configuration()
    assert crosswalk_execution_status().enabled is False


def test_both_on_is_legal_and_enabled(clean_flags) -> None:
    _enable_dependency(clean_flags)
    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, "1")
    require_valid_crosswalk_configuration()
    assert crosswalk_execution_status().enabled is True


# ── the illegal one ──────────────────────────────────────────────────────────────────────────────


def test_crosswalk_on_with_dependency_off_raises_and_names_both_flags(clean_flags) -> None:
    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, "1")
    with pytest.raises(CrosswalkConfigurationError) as raised:
        require_valid_crosswalk_configuration()
    message = str(raised.value)
    assert CROSSWALK_EXECUTION_FLAG in message
    assert "FEATUREGEN_SOURCE_TEMPORAL_SELECTION" in message
    assert "REFUSING TO BOOT" in message


def test_the_message_names_the_transitive_flag_when_the_middle_one_was_set(clean_flags) -> None:
    clean_flags.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")
    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, "1")
    with pytest.raises(CrosswalkConfigurationError) as raised:
        require_valid_crosswalk_configuration()
    # An operator who set the middle variable must not be sent round in a circle.
    assert "FEATUREGEN_DATASET_PROFILES" in str(raised.value)


def test_the_illegal_combination_refuses_to_boot(clean_flags) -> None:
    """The REAL application lifespan, not the helper — D8's row is about booting.

    Release B's counterpart deliberately does the opposite (`_startup_flag_dependency_check` logs
    and continues), so this asserts against the shipped app rather than the pure function.
    """
    from featuregen.api.app import create_app

    clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, "1")
    with pytest.raises(CrosswalkConfigurationError):
        with TestClient(create_app()):
            pass


@pytest.mark.parametrize("crosswalk", [None, "1"])
def test_the_legal_combinations_boot(clean_flags, crosswalk) -> None:
    from featuregen.api.app import create_app

    _enable_dependency(clean_flags)
    if crosswalk is not None:
        clean_flags.setenv(CROSSWALK_EXECUTION_FLAG, crosswalk)
    with TestClient(create_app()) as client:
        assert client.app.state.crosswalk_execution_enabled is (crosswalk is not None)
