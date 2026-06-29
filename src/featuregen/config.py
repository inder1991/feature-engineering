"""Environment-based configuration for FeatureGen.

A small, dependency-free settings module. Runtime configuration (database DSN,
etc.) is read from the environment so nothing secret is hard-coded or committed.
See ``.env.example`` for the recognized variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved runtime settings."""

    dsn: str | None
    test_dsn: str | None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            dsn=os.environ.get("FEATUREGEN_DSN"),
            test_dsn=os.environ.get("FEATUREGEN_TEST_DSN"),
        )


def get_settings() -> Settings:
    """Return settings resolved from the current environment."""
    return Settings.from_env()
