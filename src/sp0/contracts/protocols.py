from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from sp0.contracts.db import DbConn
from sp0.contracts.envelopes import (
    EventEnvelope,
    GuardOutcome,
    HandlerContext,
    HandlerResult,
)

GuardInputs = Mapping[str, Any]
Upcaster = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@runtime_checkable
class Projection(Protocol):
    name: str
    is_analytics: bool

    def apply(self, conn: "DbConn", event: EventEnvelope) -> None:
        """Apply ONE event (events arrive in strict global_seq order). State-bearing
        projections raise ProjectionApplyError on an unappliable event."""

    def reset(self, conn: "DbConn") -> None:
        """Truncate this projection's tables for a from-zero rebuild."""


@runtime_checkable
class Handler(Protocol):
    name: str
    version: int
    timeout_seconds: float

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        """IDEMPOTENT (§5.3). MUST NOT emit feature-/request-stream events, write outside its
        run_id, or read mutable projections. Returns events (validated against the registry) and
        optionally one document. Signals retryable/permanent via HandlerResult.disposition."""


@runtime_checkable
class GuardPredicate(Protocol):
    name: str
    declared_inputs: tuple[str, ...]

    def __call__(self, inputs: GuardInputs) -> bool: ...


class PredicateRegistry(Protocol):
    def register(self, predicate: GuardPredicate) -> None: ...

    def get(self, name: str) -> GuardPredicate: ...

    def evaluate(self, guard_expr: str, inputs: GuardInputs) -> GuardOutcome: ...


class SchemaRegistry(Protocol):
    """Implemented twice: an event registry and a document/artifact registry."""

    def register_schema(
        self,
        type_name: str,
        schema_version: int,
        json_schema: Mapping[str, Any],
        owner: str,
        *,
        status: str = "active",
    ) -> None: ...

    def register_upcaster(
        self,
        type_name: str,
        from_version: int,
        to_version: int,
        upcaster: Upcaster,
    ) -> None: ...

    def validate(self, type_name: str, schema_version: int, body: Mapping[str, Any]) -> None: ...

    def upcast(
        self, type_name: str, body: Mapping[str, Any], from_version: int, to_version: int
    ) -> Mapping[str, Any]: ...

    def snapshot_version(self) -> str: ...
