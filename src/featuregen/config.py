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
    # The TRUSTED PUBLIC key (PEM) for verifying the Phase-3B.4 3C-enablement-gate artifact's
    # ed25519 DETACHED signature (§10.7). ASYMMETRIC by design: the PRIVATE key is held by a
    # separate signing authority outside the evaluator's process, so the evaluator that computes
    # the gate cannot sign (forge) its own PASS — only this public half is a config input, and it
    # is NEVER embedded in the artifact. Fail-closed: verification refuses when unset.
    intent_gate_public_key: str | None
    # The producing code version stamped on each shadow run's dispatch manifest (the "cohort" the 3C.1
    # gate windows over). Set at deploy (e.g. the git SHA). Unset -> the sentinel "unset", which the
    # window selector treats as an uncertified cohort (fail-closed exclusion).
    producer_commit: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            dsn=os.environ.get("FEATUREGEN_DSN"),
            test_dsn=os.environ.get("FEATUREGEN_TEST_DSN"),
            audit_hmac_key=os.environ.get("FEATUREGEN_AUDIT_HMAC_KEY"),
            intent_gate_public_key=os.environ.get("FEATUREGEN_INTENT_GATE_PUBLIC_KEY"),
            producer_commit=os.environ.get("FEATUREGEN_PRODUCER_COMMIT", "unset"),
        )


def get_settings() -> Settings:
    """Return settings resolved from the current environment."""
    return Settings.from_env()
