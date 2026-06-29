from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Json

from featuregen.contracts import PredicateRegistry
from featuregen.state_machine.guard_expr import parse_guard_expr, predicate_names

_TABLE_NAMES = {"run": "run_transition_table", "feature": "feature_lifecycle_table"}


class TransitionTableError(Exception):
    """Raised on a load-time/registration-time transition-table defect (§4.1)."""


@dataclass(frozen=True, slots=True)
class Transition:
    table_version: int
    from_state: str
    to_state: str
    trigger: str
    guard_expr: str | None
    guard_inputs: Mapping[str, Any]
    precedence: int
    on_success: Mapping[str, str]
    on_guard_fail: Mapping[str, str] | None


@dataclass(frozen=True, slots=True)
class TransitionTable:
    kind: str
    table_version: int
    transitions: tuple[Transition, ...]

    def matches(self, from_state: str, trigger: str) -> list[Transition]:
        hits = [t for t in self.transitions if t.from_state == from_state and t.trigger == trigger]
        return sorted(hits, key=lambda t: t.precedence, reverse=True)

    @property
    def states(self) -> frozenset[str]:
        out: set[str] = set()
        for t in self.transitions:
            out.add(t.from_state)
            out.add(t.to_state)
            if "to" in t.on_success:
                out.add(t.on_success["to"])
            if t.on_guard_fail is not None and "to" in t.on_guard_fail:
                out.add(t.on_guard_fail["to"])
        return frozenset(out)


def _binding_set(bound: Any) -> frozenset[str]:
    if isinstance(bound, str):
        return frozenset({bound})
    if isinstance(bound, (list, tuple)):
        return frozenset(bound)
    raise TransitionTableError(f"guard_inputs binding {bound!r} must be a str or list")


def _validate(transitions: list[Transition], registry: PredicateRegistry) -> None:
    seen: set[tuple[str, str, int]] = set()
    for t in transitions:
        if "to" not in t.on_success or "emits" not in t.on_success:
            raise TransitionTableError(
                f"on_success for {t.from_state}/{t.trigger} must declare 'to' and 'emits'"
            )
        if t.guard_expr is not None:
            ogf = t.on_guard_fail
            if ogf is None or "to" not in ogf or "emits" not in ogf:
                raise TransitionTableError(
                    f"guarded transition {t.from_state}/{t.trigger} must declare "
                    "on_guard_fail with 'to' and 'emits' (no fall-through, §4.1)"
                )
            for name in predicate_names(parse_guard_expr(t.guard_expr)):
                try:
                    predicate = registry.get(name)
                except KeyError as exc:
                    raise TransitionTableError(f"guard predicate {name!r} not registered") from exc
                bound = t.guard_inputs.get(name)
                if bound is None:
                    raise TransitionTableError(
                        f"transition {t.from_state}/{t.trigger} missing guard_inputs "
                        f"binding for predicate {name!r}"
                    )
                if _binding_set(bound) != frozenset(predicate.declared_inputs):
                    raise TransitionTableError(
                        f"guard_inputs binding for {name!r} "
                        f"({sorted(_binding_set(bound))}) != declared_inputs "
                        f"({sorted(predicate.declared_inputs)})"
                    )
        key = (t.from_state, t.trigger, t.precedence)
        if key in seen:
            raise TransitionTableError(
                f"precedence tie for {t.from_state}/{t.trigger} at {t.precedence}"
            )
        seen.add(key)


def install_transition_table(
    conn: Any,
    kind: str,
    table_version: int,
    transitions: list[Transition],
    registry: PredicateRegistry,
) -> None:
    if kind not in _TABLE_NAMES:
        raise TransitionTableError(f"unknown table kind {kind!r}")
    _validate(transitions, registry)
    table = _TABLE_NAMES[kind]
    with conn.cursor() as cur:
        for t in transitions:
            cur.execute(
                f"INSERT INTO {table} "
                "(table_version, from_state, to_state, trigger, guard_expr, "
                " guard_inputs, precedence, on_success, on_guard_fail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    table_version,
                    t.from_state,
                    t.to_state,
                    t.trigger,
                    t.guard_expr,
                    Json(dict(t.guard_inputs)),
                    t.precedence,
                    Json(dict(t.on_success)),
                    Json(dict(t.on_guard_fail)) if t.on_guard_fail is not None else None,
                ),
            )


def load_transition_table(conn: Any, kind: str, table_version: int) -> TransitionTable:
    if kind not in _TABLE_NAMES:
        raise TransitionTableError(f"unknown table kind {kind!r}")
    table = _TABLE_NAMES[kind]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT table_version, from_state, to_state, trigger, guard_expr, "
            f"guard_inputs, precedence, on_success, on_guard_fail "
            f"FROM {table} WHERE table_version = %s",
            (table_version,),
        )
        rows = cur.fetchall()
    transitions = tuple(
        Transition(
            table_version=r[0],
            from_state=r[1],
            to_state=r[2],
            trigger=r[3],
            guard_expr=r[4],
            guard_inputs=r[5] or {},
            precedence=r[6],
            on_success=r[7],
            on_guard_fail=r[8],
        )
        for r in rows
    )
    return TransitionTable(kind=kind, table_version=table_version, transitions=transitions)
