"""``FEATUREGEN_CROSSWALK_EXECUTION`` — the Release-C gate, and its TRUE BOOT REFUSAL (D8).

**Why this flag does not inherit Release B's precedent.** ``FEATUREGEN_SOURCE_TEMPORAL_SELECTION``
ships its dependency as fail-closed-at-every-call-site plus a loud boot LOG, and
``api/app.py:_startup_flag_dependency_check`` says in as many words that it never blocks startup
because "the running configuration is a valid one (the feature is simply off)". That reading is
correct for Release B and WRONG here, and the verified-interfaces D8 row says so explicitly
(2026-08-03): Release C's row REQUIRES a true boot refusal and must NOT inherit the log-only
precedent.

The reason is what the two flags switch on. Release B's flag opens read/authoring surfaces; with its
dependency unmet those surfaces 404 and nothing else in the product changes. Release C's flag opens
GENERATED EXECUTION over a two-leg mapping-table traversal whose mapping rows are selected by the
Release-B temporal policy — the row rule that decides whether uniqueness was measured over the
current mapping row or over its whole SCD history. A deployment that asked for crosswalk execution
while source/temporal selection is disabled is not "the feature is simply off": it is a deployment
whose operator asked to execute crosswalks and would get a mapping-row rule nobody resolved. Booting
into that and logging about it is how a silent wrong number gets published, so this configuration
refuses to boot at all.

**Three legal states, one illegal one.** The dependency is the source/temporal feature being
ENABLED, not the variable being set — ``FEATUREGEN_SOURCE_TEMPORAL_SELECTION=1`` with
``FEATUREGEN_DATASET_PROFILES`` off is itself an invalid combination that leaves the feature
disabled, so a crosswalk flag riding on the raw variable would ride on a feature that is not
running. The refusal message therefore names the transitive flag too, so an operator who set the
middle variable is not sent round in a circle.

Truthy set: ``{"1", "true", "yes", "on"}`` — the widened set every flag in this program reads (D8;
the ``feature_assist.py:193`` pattern), read on EVERY call so flipping the variable is a path a test
can exercise rather than a switch captured at import.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "CROSSWALK_EXECUTION_FLAG",
    "CrosswalkConfigurationError",
    "CrosswalkExecutionFlagStatus",
    "crosswalk_execution_enabled",
    "crosswalk_execution_status",
    "require_valid_crosswalk_configuration",
]

CROSSWALK_EXECUTION_FLAG = "FEATUREGEN_CROSSWALK_EXECUTION"

#: The widened truthy set every flag in this program reads (D8).
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class CrosswalkConfigurationError(RuntimeError):
    """An INVALID flag combination, raised where configuration is validated.

    A ``RuntimeError`` and not a ``ValueError``: nothing about a request or a governed artifact is
    wrong, the process is misconfigured, and the only correct response is to not run.
    """


@dataclass(frozen=True, slots=True)
class CrosswalkExecutionFlagStatus:
    """The flag's configured intent, its dependency's EFFECTIVE state, and the verdict."""

    configured_value: str | None
    requested: bool
    #: ``source_temporal_selection_enabled()`` — the feature's effective state, never the raw
    #: variable (see the module docstring).
    dependency_met: bool
    #: Whether the middle variable was ASKED for. Only ever used to word the refusal: an operator
    #: who set it and whose own dependency is unmet needs to be told about the third flag.
    dependency_requested: bool

    @property
    def enabled(self) -> bool:
        return self.requested and self.dependency_met

    @property
    def configuration_valid(self) -> bool:
        return not (self.requested and not self.dependency_met)


def crosswalk_execution_status() -> CrosswalkExecutionFlagStatus:
    """Read both flags and report the combined posture. Never raises — the caller decides."""
    from featuregen.overlay.upload.source_selection import source_temporal_selection_status

    configured = os.environ.get(CROSSWALK_EXECUTION_FLAG)
    dependency = source_temporal_selection_status()
    return CrosswalkExecutionFlagStatus(
        configured_value=configured,
        requested=(configured or "").strip().lower() in _TRUTHY,
        dependency_met=dependency.enabled,
        dependency_requested=dependency.requested,
    )


def crosswalk_execution_enabled() -> bool:
    """The single env gate for every Release-C execution surface. Default OFF.

    Fail-closed on the dependency as well as on its own value, so a surface that forgot the boot
    check still cannot run a crosswalk in a configuration the boot check would have refused.
    """
    return crosswalk_execution_status().enabled


def require_valid_crosswalk_configuration() -> None:
    """Raise :class:`CrosswalkConfigurationError` on the ONE illegal combination (D8).

    Called at application startup BEFORE anything is served. The three legal states — both off,
    crosswalk off with source/temporal on, and both on — return ``None``.
    """
    status = crosswalk_execution_status()
    if status.configuration_valid:
        return
    from featuregen.overlay.upload.profile_vocab import DATASET_PROFILES_FLAG
    from featuregen.overlay.upload.source_selection import SOURCE_TEMPORAL_SELECTION_FLAG

    if status.dependency_requested:
        remedy = (
            f"{SOURCE_TEMPORAL_SELECTION_FLAG} IS set, but it is itself disabled because its own "
            f"dependency {DATASET_PROFILES_FLAG} is off — set {DATASET_PROFILES_FLAG}=1 as well, "
            f"or unset {CROSSWALK_EXECUTION_FLAG}")
    else:
        remedy = (
            f"set {SOURCE_TEMPORAL_SELECTION_FLAG}=1 (which itself requires "
            f"{DATASET_PROFILES_FLAG}=1), or unset {CROSSWALK_EXECUTION_FLAG}")
    raise CrosswalkConfigurationError(
        f"INVALID FLAG COMBINATION — REFUSING TO BOOT. {CROSSWALK_EXECUTION_FLAG}="
        f"{status.configured_value!r} asks for generated two-leg crosswalk execution while "
        f"{SOURCE_TEMPORAL_SELECTION_FLAG} is DISABLED. A crosswalk's mapping rows are selected by "
        f"the Release-B temporal policy, so with that feature off the mapping row rule is one "
        f"nobody resolved and uniqueness would be claimed over whatever rows the mapping table "
        f"happens to hold — including its whole history. The dependency is required "
        f"(verified-interfaces D8, which requires this to be a boot REFUSAL and not the log-only "
        f"precedent Release B ships). To fix: {remedy}.")
