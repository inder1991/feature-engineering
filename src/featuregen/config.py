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
    # Verifier-held key for the tamper-evident security-audit HMAC chain (§6.2). Held by the
    # process, NOT stored in the DB: a writer who can recompute an unkeyed hash could forge
    # the chain, so the signature must be keyed. Fail-closed — audit.py refuses to sign when
    # this is unset rather than fall back to a default (see security.audit).
    audit_hmac_key: str | None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            dsn=os.environ.get("FEATUREGEN_DSN"),
            test_dsn=os.environ.get("FEATUREGEN_TEST_DSN"),
            audit_hmac_key=os.environ.get("FEATUREGEN_AUDIT_HMAC_KEY"),
        )


def get_settings() -> Settings:
    """Return settings resolved from the current environment."""
    return Settings.from_env()
