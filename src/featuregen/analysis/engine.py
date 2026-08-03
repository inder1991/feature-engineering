"""WHERE a sealed analysis plan actually runs — the one seam between the catalog and a warehouse.

The catalog connection and the ANALYSIS ENGINE are different things and must not be conflated. The
catalog is this platform's own Postgres; the analysis engine is the bank's warehouse, reached
through a governed :class:`DataSourceConnectionV1` (allowlist, principal, credential resolution,
driver table). ``recording_refusals`` already makes this distinction in prose — "learning events and
observations are catalog state; the analysis SQL runs somewhere else entirely" — and this module is
where it becomes a type.

**The default provider REFUSES, deliberately.** Running a sealed plan against the bank's real
warehouse is a separate approval gate (the Release-B pilot). A provider that quietly called
``open_connection`` would take that decision on someone's behalf the first time a route was hit, so
the shipped default is a typed refusal that says which approval is outstanding. A deployment that
has the approval supplies a provider; the test suite supplies the FIXTURE engine, which is the
pilot's own Postgres and touches no cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.data_agent.connection import DataSourceConnectionV1

#: The refusal a caller sees while no engine is wired. Named, so the screen can say what is
#: outstanding rather than reporting a generic failure.
EXECUTION_ENGINE_NOT_APPROVED = "EXECUTION_ENGINE_NOT_APPROVED"


class AnalysisEngineUnavailable(Exception):
    """No engine may run this plan here. Carries the closed code and the subject."""

    def __init__(self, code: str, message: str, *, subject: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.subject = subject


@dataclass(frozen=True, slots=True)
class AnalysisEngineV1:
    """An open cursor source and the dialect that compiles for it."""

    conn: Any
    dialect: Any
    engine: str = ""


def default_analysis_engine(connection: DataSourceConnectionV1) -> AnalysisEngineV1:
    """The shipped provider: refuse, and say which approval is outstanding.

    Not a stub and not a TODO — it is the correct behaviour until the pilot run is approved. The
    whole path above it is built and exercised end to end against the fixture engine, so approving
    the run is a configuration decision rather than a further slice of work.
    """
    raise AnalysisEngineUnavailable(
        EXECUTION_ENGINE_NOT_APPROVED,
        "no analysis engine is wired in this deployment. Running a sealed plan against the bank's "
        f"warehouse through {connection.connection_id!r} is a separate approval gate, and a route "
        "that opened the connection anyway would make that decision on someone's behalf",
        subject=connection.connection_id)
