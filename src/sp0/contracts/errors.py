from __future__ import annotations


class ConcurrencyError(Exception):
    """Raised when expected_version != the stream's current stream_version (OCC)."""


class ProjectionApplyError(Exception):
    """Raised by a fail-closed projection that cannot apply an event; carries the
    affected aggregate so the runner can mark it `degraded` and block its commands."""

    def __init__(self, aggregate: str, aggregate_id: str, reason: str) -> None:
        self.aggregate, self.aggregate_id, self.reason = aggregate, aggregate_id, reason
        super().__init__(f"{aggregate}:{aggregate_id}: {reason}")


class SchemaValidationError(Exception):
    """Raised by SchemaRegistry.validate on a schema mismatch."""
