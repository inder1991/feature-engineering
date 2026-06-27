## Phase 03: State-machine engine (guards, versioning, failure events)

**Goal:** Implement the two versioned declarative transition tables, the pure/deterministic guard-predicate registry with predicate→declared-input binding, precedence selection with no fall-through, symmetric success/failure event typing (`GUARD_FAILED`/`TRANSITION_REJECTED` carrying resolved inputs + result), and per-aggregate `table_version` pinning with explicit audited migration commands — all against the shared contract, consumed by Phases 04/06.

### Boundary note (what this phase does NOT do)

- The engine is **pure**: `evaluate_transition` returns a `TransitionResult` (target state + emitted event type + audit payload). It does **not** append events. The `on_success` domain event and the `GUARD_FAILED`/`TRANSITION_REJECTED` audit events are appended inside the §5.1 atomic boundary owned by Phase 04 (step handlers) / Phase 06 (commands). This phase only **owns** those event *types* (registered schemas) plus the result object.
- The only place this phase appends is the two **migration commands** (they are §4.4 lifecycle commands and emit one audited migration event each), wired into the command catalog by Phase 06.
- `current_state` is supplied **to** the migration command by its caller (read from the `run_workflow_state` projection, owned elsewhere). This phase never reads a mutable projection.

### Provenance placement decision (RAISED TO THE OVERVIEW — sanctioned)

§4.1 says failure events carry "the resolved inputs + boolean result in `provenance`." The shared `ProvenanceEnvelope` (owned by Phase 08) is a **fixed dataclass with no guard slot** and **must not be redefined** (overview "Notes for phase authors": a signature mismatch across phases is a build-breaking bug, so divergence is forbidden). This is a genuine spec-vs-contract conflict, so per the overview's instruction it was **raised back to the overview rather than decided locally**.

**Sanctioned resolution (recorded here as the cross-phase contract):** the guard audit record (`guard_expr`, `passed`, `resolved_inputs`, `per_predicate`) is written into the **event payload** of `GUARD_FAILED`/`TRANSITION_REJECTED` (free-form `jsonb`), nested under the `"guard"` key, which is itself an immutable audited event — fully satisfying "every outcome is an audited event carrying the resolved inputs + result." The literal "in `provenance`" wording is superseded by "in the immutable audited event," because the authoritative `ProvenanceEnvelope` cannot carry it.

**Downstream agreement required (binding on Phases 04 and 06):** Phases 04 (step handlers) and 06 (commands) are the phases that actually **append** these events inside the §5.1 boundary; they MUST read the guard block from `event.payload["guard"]` (NOT from `provenance`) and MUST copy `engine.TransitionResult.audit_payload` verbatim into the event payload. Any later phase that wants this in `provenance` must re-open the overview to add a guard slot to `ProvenanceEnvelope` — it may not be added locally.

---

### File structure

```
src/sp0/db/migrations.py            # EDIT (Phase 01-owned): add STATE_MACHINE_DDL constant + append
                                    #   ("0030_state_machine", STATE_MACHINE_DDL) to the MIGRATIONS list
src/sp0/state_machine/
    __init__.py                     # package marker
    ddl.py                          # STATE_MACHINE_DDL string: run_transition_table, feature_lifecycle_table (verbatim)
    guard_expr.py                   # boolean expr parser/eval (AND/OR/NOT/parens), pure
    guards.py                       # InMemoryPredicateRegistry (implements PredicateRegistry); purity filter
    transition_table.py             # Transition, TransitionTable, install/load + load-time validation
    engine.py                       # evaluate_transition, TransitionResult, GUARD_FAILED/TRANSITION_REJECTED
    event_types.py                  # register Phase-03 event-type schemas into the event registry
    migrations.py                   # migrate_workflow_version / migrate_feature_lifecycle_version
tests/sp0/state_machine/
    _predicates.py                  # test-only GuardPredicate stubs (Truthy/Peeking)
    conftest.py                     # per-test (autouse) registration of SM event types + SM_TEST_SEED
    test_schema.py                  # DDL shape + PK behaviour
    test_guard_expr.py              # parser/eval precedence + malformed
    test_guards.py                  # registry register/get/evaluate + purity
    test_transition_table.py        # install validation (ties, bindings, on_guard_fail), load round-trip
    test_engine.py                  # precedence, no fall-through, no-match, version pinning, audit payload
    test_event_types.py             # registration calls + schema validation behaviour
    test_migrations.py              # audited migration, replay-against-pinned, guards, OCC
```

**Consumed from earlier phases / shared contract** (import, never redefine):
- `sp0.contracts`: `GuardInputs`, `GuardPredicate`, `GuardOutcome`, `PredicateRegistry`, `IdentityEnvelope`, `ProvenanceEnvelope`, `EventEnvelope`, `NewEvent`, `ConcurrencyError`, `SchemaValidationError`.
- `sp0.events.store` (Phase 01): `append_event(conn, new_event, *, expected_version, table_version) -> EventEnvelope`, `load_stream(conn, aggregate, aggregate_id, *, upto_seq=None, expected=None) -> list[EventEnvelope]`.
- `sp0.events.registry` (Phase 01): the **module-level accessor function** `event_registry() -> EventSchemaRegistry` (NOT a bare instance — it is a singleton accessor; you must **call** it: `event_registry().register_schema(...)`, `event_registry().validate(...)`). The returned `EventSchemaRegistry` is what `append_event` validates against and exposes `register_schema(type_name, schema_version, json_schema, owner, *, status="active") -> None` and `validate(type_name, schema_version, body) -> None`. Also `reset_event_registry() -> None` (used by Phase 01's per-test reset fixture). *These import paths (`sp0.events.store`, `sp0.events.registry`) and the function-vs-instance shape of `event_registry()` are the real Phase 01 surface — do not re-create `sp0.event_store` and do not treat `event_registry` as a bare instance.*
- `tests/conftest.py` (Phase 01, repository-root test conftest — applies to ALL tests including `tests/sp0/state_machine/`, inherited automatically): produces the function-scoped `conn` pytest fixture — a real `psycopg` connection (DSN `SP0_TEST_DSN`, default `postgresql:///sp0_test`) in a transaction that is **rolled back after each test**. The DDL is **not** glob-loaded from `*.sql`; it is the hardcoded Python `MIGRATIONS: list[tuple[str, str]]` in `src/sp0/db/migrations.py`, applied **once per session** by the session-scoped `_migrated` fixture via `apply_migrations(conn)` (which `COMMIT`s). The same conftest also defines a **function-scoped autouse `_reset_registry`** fixture that calls `reset_event_registry()` before AND after every test — so any registry registrations a child fixture needs must be (re)done per-test, AFTER `_reset_registry` runs (see Task 6's conftest).

**This phase is authoritative for** these contract symbols (shapes are the contract's verbatim; this phase provides the concrete behaviour): `GuardPredicate`, `GuardOutcome`, `GuardInputs`, `PredicateRegistry`.

---

## Task 1 — DDL migration for the two transition tables

**Files:**
- Create: `src/sp0/state_machine/__init__.py`, `src/sp0/state_machine/ddl.py`
- Edit (Phase 01-owned): `src/sp0/db/migrations.py`
- Test: `tests/sp0/state_machine/test_schema.py`

**Interfaces:**
- Consumes: the `conn` fixture (Phase 01, `tests/conftest.py`) and Phase 01's migration mechanism — the Python `MIGRATIONS: list[tuple[str, str]]` in `src/sp0/db/migrations.py`, applied once per session by `apply_migrations(conn)`. Phase 01 does **not** glob `src/sp0/migrations/*.sql`, so this phase's DDL must be **registered into that list** to be created.
- Produces: the `STATE_MACHINE_DDL` SQL string (in `src/sp0/state_machine/ddl.py`) and tables `run_transition_table`, `feature_lifecycle_table` (DDL exactly as the shared contract declares; `feature_lifecycle_table` is `LIKE run_transition_table INCLUDING ALL`, so it inherits the composite primary key `(table_version, from_state, trigger, precedence)`). Created by appending `("0030_state_machine", STATE_MACHINE_DDL)` to `MIGRATIONS`.

> **Why edit Phase 01's `src/sp0/db/migrations.py`?** Per the overview, the table is declared once in the overview and **the owning phase creates the migration**. Phase 03 owns these two tables, but Phase 01 owns the *migration runner mechanism* (a Python list, not a `*.sql` glob). The only way the harness creates these tables is to add an entry to `MIGRATIONS`. This cross-phase edit is the wiring point; it adds one constant + one list entry and changes no existing Phase 01 DDL. (If a future Phase 01 revision adopts a `src/sp0/migrations/*.sql` glob loader, that is a contract change to raise back to the overview; until then, wire into the list.)

### TDD steps

1. **Write the failing test** — `tests/sp0/state_machine/test_schema.py`:

```python
from __future__ import annotations

import psycopg
import pytest


def test_run_transition_table_has_contract_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'run_transition_table'"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {
        "table_version", "from_state", "to_state", "trigger", "guard_expr",
        "guard_inputs", "precedence", "on_success", "on_guard_fail",
    } <= cols


def test_feature_lifecycle_table_inherits_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'feature_lifecycle_table'"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {
        "table_version", "from_state", "to_state", "trigger", "guard_expr",
        "guard_inputs", "precedence", "on_success", "on_guard_fail",
    } <= cols


def test_feature_lifecycle_table_pk_enforced(conn) -> None:
    ins = (
        "INSERT INTO feature_lifecycle_table "
        "(table_version, from_state, to_state, trigger, precedence, on_success) "
        "VALUES (1, 'A', 'B', 'T', 100, '{}'::jsonb)"
    )
    with conn.cursor() as cur:
        cur.execute(ins)
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(ins)
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/state_machine/test_schema.py -q`. Expected: errors like `psycopg.errors.UndefinedTable: relation "run_transition_table" does not exist`. The tables are absent because the DDL is not yet registered into Phase 01's `MIGRATIONS` list (Phase 01 applies only `0001`–`0006`).

3. **Write minimal implementation.**

First the package marker `src/sp0/state_machine/__init__.py`:

```python
"""SP-0 Phase 03: declarative state-machine engine (§4.1/§4.2)."""
```

Then the DDL string `src/sp0/state_machine/ddl.py` (verbatim from the shared-contract DDL; imports nothing, so wiring it into Phase 01's runner creates no import cycle):

```python
from __future__ import annotations

# Phase 03: versioned declarative state-machine tables (§4.1/§4.2).
# Each row is one transition; aggregates pin a table_version. Registered into
# Phase 01's MIGRATIONS list by editing src/sp0/db/migrations.py (see below).
STATE_MACHINE_DDL = """
CREATE TABLE run_transition_table (
    table_version integer     NOT NULL,
    from_state    text        NOT NULL,
    to_state      text        NOT NULL,
    trigger       text        NOT NULL,
    guard_expr    text        NULL,
    guard_inputs  jsonb       NOT NULL DEFAULT '{}',              -- predicate -> declared input ref
    precedence    integer     NOT NULL,
    on_success    jsonb       NOT NULL,                           -- {"to":..., "emits":...}
    on_guard_fail jsonb       NULL,                               -- {"to":..., "emits":"GUARD_FAILED"}
    PRIMARY KEY (table_version, from_state, trigger, precedence)
);
CREATE TABLE feature_lifecycle_table (LIKE run_transition_table INCLUDING ALL);
"""
```

Then wire it into Phase 01's migration runner — edit `src/sp0/db/migrations.py`: add the import near the existing imports at the top, and **append** one entry to the existing `MIGRATIONS` list (append-only; do not reorder or alter the `0001`–`0006` entries):

```python
# near the top imports of src/sp0/db/migrations.py
from sp0.state_machine.ddl import STATE_MACHINE_DDL

# ... existing GLOBAL_SEQ / EVENTS / ... constants and MIGRATIONS list ...

MIGRATIONS: list[tuple[str, str]] = [
    ("0001_global_seq", GLOBAL_SEQ),
    ("0002_events", EVENTS),
    ("0003_event_type_registry", EVENT_TYPE_REGISTRY),
    ("0004_registry_snapshots", REGISTRY_SNAPSHOTS),
    ("0005_projection_checkpoints", PROJECTION_CHECKPOINTS),
    ("0006_projection_active_alias", PROJECTION_ACTIVE_ALIAS),
    ("0030_state_machine", STATE_MACHINE_DDL),  # <-- Phase 03 (added)
]
```

`apply_migrations` executes the list in order; the SM tables have no FK to the core tables, so they are appended last for clarity. (Like the rest of Phase 01's DDL, these use plain `CREATE TABLE`, so the harness assumes a fresh database per session — the existing Phase 01 contract.)

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/state_machine/test_schema.py -q`. Expected: 3 passed (the session-scoped `_migrated` fixture now creates both tables).

5. **Commit:**

```
git add src/sp0/state_machine/__init__.py src/sp0/state_machine/ddl.py src/sp0/db/migrations.py tests/sp0/state_machine/test_schema.py
git commit -m "SP-0 Phase 03: state-machine transition tables (DDL) wired into Phase 01 MIGRATIONS

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2 — Boolean guard-expression parser/evaluator (pure)

**Files:**
- Create: `src/sp0/state_machine/guard_expr.py` (the package marker `src/sp0/state_machine/__init__.py` already exists from Task 1)
- Test: `tests/sp0/state_machine/test_guard_expr.py`

**Interfaces:**
- Consumes: nothing (pure stdlib).
- Produces:
  - AST nodes `Pred`, `Not`, `And`, `Or` (frozen dataclasses) and alias `Node = Pred | Not | And | Or`.
  - `parse_guard_expr(expr: str) -> Node`
  - `eval_guard_expr(node: Node, predicate_values: Mapping[str, bool]) -> bool`
  - `predicate_names(node: Node) -> frozenset[str]`
  - `GuardExprError(Exception)`

### TDD steps

1. **Write the failing test** — `tests/sp0/state_machine/test_guard_expr.py`:

```python
from __future__ import annotations

import pytest

from sp0.state_machine.guard_expr import (
    GuardExprError,
    eval_guard_expr,
    parse_guard_expr,
    predicate_names,
)


def test_single_predicate() -> None:
    node = parse_guard_expr("a")
    assert predicate_names(node) == frozenset({"a"})
    assert eval_guard_expr(node, {"a": True}) is True
    assert eval_guard_expr(node, {"a": False}) is False


def test_and_or_collects_names() -> None:
    node = parse_guard_expr("a AND b OR c")
    assert predicate_names(node) == frozenset({"a", "b", "c"})


def test_and_binds_tighter_than_or() -> None:
    # a OR (b AND c): if a is true the whole expr is true regardless of b/c.
    node = parse_guard_expr("a OR b AND c")
    assert eval_guard_expr(node, {"a": True, "b": False, "c": False}) is True
    assert eval_guard_expr(node, {"a": False, "b": True, "c": True}) is True
    assert eval_guard_expr(node, {"a": False, "b": True, "c": False}) is False


def test_not_binds_tighter_than_and() -> None:
    node = parse_guard_expr("NOT a AND b")
    assert eval_guard_expr(node, {"a": False, "b": True}) is True
    assert eval_guard_expr(node, {"a": True, "b": True}) is False


def test_parentheses_override_precedence() -> None:
    node = parse_guard_expr("(a OR b) AND c")
    assert eval_guard_expr(node, {"a": True, "b": False, "c": False}) is False
    assert eval_guard_expr(node, {"a": True, "b": False, "c": True}) is True


def test_empty_expression_raises() -> None:
    with pytest.raises(GuardExprError):
        parse_guard_expr("   ")


def test_dangling_operator_raises() -> None:
    with pytest.raises(GuardExprError):
        parse_guard_expr("a AND")


def test_unbalanced_paren_raises() -> None:
    with pytest.raises(GuardExprError):
        parse_guard_expr("(a OR b")


def test_unexpected_character_raises() -> None:
    with pytest.raises(GuardExprError):
        parse_guard_expr("a & b")
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/state_machine/test_guard_expr.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.state_machine.guard_expr'`.

3. **Write minimal implementation** — `src/sp0/state_machine/guard_expr.py` (`src/sp0/state_machine/__init__.py` was already created in Task 1):

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Union


class GuardExprError(Exception):
    """Raised when a guard expression is malformed (load-time error, §4.1)."""


@dataclass(frozen=True, slots=True)
class Pred:
    name: str


@dataclass(frozen=True, slots=True)
class Not:
    operand: "Node"


@dataclass(frozen=True, slots=True)
class And:
    left: "Node"
    right: "Node"


@dataclass(frozen=True, slots=True)
class Or:
    left: "Node"
    right: "Node"


Node = Union[Pred, Not, And, Or]

_TOKEN_RE = re.compile(r"\(|\)|[A-Za-z_][A-Za-z0-9_]*")
_KEYWORDS = frozenset({"AND", "OR", "NOT"})


def _tokenize(expr: str) -> list[str]:
    tokens: list[str] = []
    pos = 0
    n = len(expr)
    while pos < n:
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(expr, pos)
        if m is None:
            raise GuardExprError(f"unexpected character {expr[pos]!r} in {expr!r}")
        tokens.append(m.group(0))
        pos = m.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._i = 0

    def _peek(self) -> str | None:
        return self._tokens[self._i] if self._i < len(self._tokens) else None

    def _advance(self) -> str | None:
        tok = self._peek()
        self._i += 1
        return tok

    def parse(self) -> Node:
        if not self._tokens:
            raise GuardExprError("empty guard expression")
        node = self._or()
        if self._i != len(self._tokens):
            raise GuardExprError(f"trailing tokens: {self._tokens[self._i:]}")
        return node

    def _or(self) -> Node:
        node = self._and()
        while self._peek() == "OR":
            self._advance()
            node = Or(node, self._and())
        return node

    def _and(self) -> Node:
        node = self._not()
        while self._peek() == "AND":
            self._advance()
            node = And(node, self._not())
        return node

    def _not(self) -> Node:
        if self._peek() == "NOT":
            self._advance()
            return Not(self._not())
        return self._atom()

    def _atom(self) -> Node:
        tok = self._advance()
        if tok is None:
            raise GuardExprError("unexpected end of expression")
        if tok == "(":
            node = self._or()
            if self._advance() != ")":
                raise GuardExprError("expected ')'")
            return node
        if tok in _KEYWORDS or tok == ")":
            raise GuardExprError(f"unexpected token {tok!r}")
        return Pred(tok)


def parse_guard_expr(expr: str) -> Node:
    return _Parser(_tokenize(expr)).parse()


def eval_guard_expr(node: Node, predicate_values: Mapping[str, bool]) -> bool:
    if isinstance(node, Pred):
        return bool(predicate_values[node.name])
    if isinstance(node, Not):
        return not eval_guard_expr(node.operand, predicate_values)
    if isinstance(node, And):
        return eval_guard_expr(node.left, predicate_values) and eval_guard_expr(
            node.right, predicate_values
        )
    if isinstance(node, Or):
        return eval_guard_expr(node.left, predicate_values) or eval_guard_expr(
            node.right, predicate_values
        )
    raise GuardExprError(f"unknown node type {type(node)!r}")


def predicate_names(node: Node) -> frozenset[str]:
    if isinstance(node, Pred):
        return frozenset({node.name})
    if isinstance(node, Not):
        return predicate_names(node.operand)
    if isinstance(node, (And, Or)):
        return predicate_names(node.left) | predicate_names(node.right)
    raise GuardExprError(f"unknown node type {type(node)!r}")
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/state_machine/test_guard_expr.py -q`. Expected: 9 passed.

5. **Commit:**

```
git add src/sp0/state_machine/guard_expr.py tests/sp0/state_machine/test_guard_expr.py
git commit -m "SP-0 Phase 03: pure boolean guard-expression parser/evaluator

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3 — InMemoryPredicateRegistry (register / get / evaluate + mechanical purity)

**Files:**
- Create: `src/sp0/state_machine/guards.py`
- Create: `tests/sp0/state_machine/_predicates.py`
- Test: `tests/sp0/state_machine/test_guards.py`

**Interfaces:**
- Consumes: `GuardInputs`, `GuardOutcome`, `GuardPredicate`, `PredicateRegistry` (from `sp0.contracts`); `parse_guard_expr`, `predicate_names`, `eval_guard_expr` (Task 2).
- Produces:
  - `InMemoryPredicateRegistry` — concrete implementation of the `PredicateRegistry` Protocol with methods `register(predicate: GuardPredicate) -> None`, `get(name: str) -> GuardPredicate`, `evaluate(guard_expr: str, inputs: GuardInputs) -> GuardOutcome`.
  - `PredicateRegistrationError(Exception)` (raised on duplicate registration — a load-time error per §4.1).
  - **Purity contract:** `evaluate` passes each predicate **only** the subset of `inputs` named in its `declared_inputs`; a missing declared input raises `KeyError`; `GuardOutcome.resolved_inputs` is exactly the union of consumed declared inputs; all predicates are evaluated (no short-circuit) so the audit is complete and deterministic.

### TDD steps

1. **Write the test-only predicate stubs** — `tests/sp0/state_machine/_predicates.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from sp0.contracts import GuardInputs


@dataclass(frozen=True, slots=True)
class TruthyPredicate:
    """Pure predicate: returns bool(inputs[key]) over its single declared input."""

    name: str
    declared_inputs: tuple[str, ...]
    key: str

    def __call__(self, inputs: GuardInputs) -> bool:
        return bool(inputs[self.key])


@dataclass(frozen=True, slots=True)
class PeekingPredicate:
    """Declares one input but illegally reads another — used to prove the registry
    mechanically blocks reads outside declared_inputs (the access raises KeyError)."""

    name: str
    declared_inputs: tuple[str, ...]
    peek_key: str

    def __call__(self, inputs: GuardInputs) -> bool:
        return bool(inputs[self.peek_key])


def truthy(name: str, key: str) -> TruthyPredicate:
    return TruthyPredicate(name=name, declared_inputs=(key,), key=key)
```

2. **Write the failing test** — `tests/sp0/state_machine/test_guards.py`:

```python
from __future__ import annotations

import pytest

from sp0.state_machine.guard_expr import GuardExprError
from sp0.state_machine.guards import (
    InMemoryPredicateRegistry,
    PredicateRegistrationError,
)
from tests.sp0.state_machine._predicates import PeekingPredicate, truthy


def _registry() -> InMemoryPredicateRegistry:
    reg = InMemoryPredicateRegistry()
    reg.register(truthy("confirmed_contract_exists", "confirmed_contract_ref"))
    reg.register(truthy("catalog_quality_passed", "catalog_quality_result_ref"))
    return reg


def test_register_and_get() -> None:
    reg = _registry()
    assert reg.get("confirmed_contract_exists").name == "confirmed_contract_exists"


def test_duplicate_registration_is_load_error() -> None:
    reg = _registry()
    with pytest.raises(PredicateRegistrationError):
        reg.register(truthy("confirmed_contract_exists", "confirmed_contract_ref"))


def test_get_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        InMemoryPredicateRegistry().get("nope")


def test_evaluate_true_records_outcome() -> None:
    reg = _registry()
    outcome = reg.evaluate(
        "confirmed_contract_exists AND catalog_quality_passed",
        {"confirmed_contract_ref": "doc_1", "catalog_quality_result_ref": "doc_2"},
    )
    assert outcome.passed is True
    assert outcome.per_predicate == {
        "confirmed_contract_exists": True,
        "catalog_quality_passed": True,
    }
    assert outcome.resolved_inputs == {
        "confirmed_contract_ref": "doc_1",
        "catalog_quality_result_ref": "doc_2",
    }


def test_evaluate_false_when_one_predicate_false() -> None:
    reg = _registry()
    outcome = reg.evaluate(
        "confirmed_contract_exists AND catalog_quality_passed",
        {"confirmed_contract_ref": "doc_1", "catalog_quality_result_ref": ""},
    )
    assert outcome.passed is False
    assert outcome.per_predicate["catalog_quality_passed"] is False


def test_resolved_inputs_excludes_undeclared_keys() -> None:
    reg = _registry()
    outcome = reg.evaluate(
        "confirmed_contract_exists",
        {"confirmed_contract_ref": "doc_1", "some_mutable_projection": "LEAK"},
    )
    assert "some_mutable_projection" not in outcome.resolved_inputs
    assert outcome.resolved_inputs == {"confirmed_contract_ref": "doc_1"}


def test_predicate_cannot_read_undeclared_input() -> None:
    reg = InMemoryPredicateRegistry()
    reg.register(PeekingPredicate(name="peeker", declared_inputs=("a",), peek_key="b"))
    with pytest.raises(KeyError):
        reg.evaluate("peeker", {"a": "x", "b": "y"})


def test_missing_declared_input_raises() -> None:
    reg = _registry()
    with pytest.raises(KeyError):
        reg.evaluate("confirmed_contract_exists", {})


def test_unregistered_predicate_in_expr_raises() -> None:
    reg = _registry()
    with pytest.raises(KeyError):
        reg.evaluate("confirmed_contract_exists AND not_registered", {"confirmed_contract_ref": "x"})


def test_malformed_expr_propagates() -> None:
    reg = _registry()
    with pytest.raises(GuardExprError):
        reg.evaluate("confirmed_contract_exists AND", {"confirmed_contract_ref": "x"})
```

3. **Run it, expect FAIL** — `python -m pytest tests/sp0/state_machine/test_guards.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.state_machine.guards'`.

4. **Write minimal implementation** — `src/sp0/state_machine/guards.py`:

```python
from __future__ import annotations

from typing import Any

from sp0.contracts import GuardInputs, GuardOutcome, GuardPredicate
from sp0.state_machine.guard_expr import (
    eval_guard_expr,
    parse_guard_expr,
    predicate_names,
)


class PredicateRegistrationError(Exception):
    """Raised on duplicate predicate registration (load-time error, §4.1)."""


class InMemoryPredicateRegistry:
    """Concrete `PredicateRegistry` (§4.1). Pure: predicates receive only their
    declared inputs, so they cannot read mutable projections or undeclared keys."""

    def __init__(self) -> None:
        self._predicates: dict[str, GuardPredicate] = {}

    def register(self, predicate: GuardPredicate) -> None:
        if predicate.name in self._predicates:
            raise PredicateRegistrationError(
                f"predicate {predicate.name!r} already registered"
            )
        self._predicates[predicate.name] = predicate

    def get(self, name: str) -> GuardPredicate:
        try:
            return self._predicates[name]
        except KeyError:
            raise KeyError(f"predicate {name!r} not registered") from None

    def evaluate(self, guard_expr: str, inputs: GuardInputs) -> GuardOutcome:
        node = parse_guard_expr(guard_expr)
        per_predicate: dict[str, bool] = {}
        resolved: dict[str, Any] = {}
        for name in sorted(predicate_names(node)):
            predicate = self.get(name)
            view: dict[str, Any] = {}
            for key in predicate.declared_inputs:
                if key not in inputs:
                    raise KeyError(
                        f"guard input {key!r} for predicate {name!r} not resolved"
                    )
                view[key] = inputs[key]
            per_predicate[name] = bool(predicate(view))
            resolved.update(view)
        passed = eval_guard_expr(node, per_predicate)
        return GuardOutcome(
            passed=passed,
            resolved_inputs=resolved,
            per_predicate=per_predicate,
        )
```

5. **Run tests, expect PASS** — `python -m pytest tests/sp0/state_machine/test_guards.py -q`. Expected: 10 passed.

6. **Commit:**

```
git add src/sp0/state_machine/guards.py tests/sp0/state_machine/_predicates.py tests/sp0/state_machine/test_guards.py
git commit -m "SP-0 Phase 03: predicate registry with mechanical purity (§4.1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4 — Transition table model, load-time validation, install + load

**Files:**
- Create: `src/sp0/state_machine/transition_table.py`
- Test: `tests/sp0/state_machine/test_transition_table.py`

**Interfaces:**
- Consumes: `parse_guard_expr`, `predicate_names` (Task 2); `PredicateRegistry` (from `sp0.contracts`); `psycopg.types.json.Json`; `conn` fixture (Phase 01).
- Produces:
  - `Transition` (frozen dataclass): `table_version: int`, `from_state: str`, `to_state: str`, `trigger: str`, `guard_expr: str | None`, `guard_inputs: Mapping[str, Any]`, `precedence: int`, `on_success: Mapping[str, str]`, `on_guard_fail: Mapping[str, str] | None`.
  - `TransitionTable` (frozen dataclass): `kind: str`, `table_version: int`, `transitions: tuple[Transition, ...]`; method `matches(from_state, trigger) -> list[Transition]` (highest precedence first); property `states -> frozenset[str]`.
  - `install_transition_table(conn, kind, table_version, transitions, registry) -> None` — validates at registration time then inserts rows. **Validation rejects**: malformed `on_success` (must declare `to` + `emits`); a guarded transition missing `on_guard_fail` (must declare `to` + `emits`); a guard predicate not registered; a `guard_inputs` binding whose ref-set does not equal the predicate's `declared_inputs`; two transitions sharing `(from_state, trigger)` with the same `precedence` (tie).
  - `load_transition_table(conn, kind, table_version) -> TransitionTable`.
  - `TransitionTableError(Exception)`.
  - `kind` is whitelisted to `{"run": "run_transition_table", "feature": "feature_lifecycle_table"}` (never interpolated from arbitrary input).

### TDD steps

1. **Write the failing test** — `tests/sp0/state_machine/test_transition_table.py`:

```python
from __future__ import annotations

import psycopg
import pytest

from sp0.state_machine.guards import InMemoryPredicateRegistry
from sp0.state_machine.transition_table import (
    Transition,
    TransitionTableError,
    install_transition_table,
    load_transition_table,
)
from tests.sp0.state_machine._predicates import truthy


def _registry() -> InMemoryPredicateRegistry:
    reg = InMemoryPredicateRegistry()
    reg.register(truthy("confirmed_contract_exists", "confirmed_contract_ref"))
    reg.register(truthy("catalog_quality_passed", "catalog_quality_result_ref"))
    return reg


def _guarded(precedence: int = 100) -> Transition:
    return Transition(
        table_version=1,
        from_state="CONFIRMED_CONTRACT",
        to_state="SCHEMA_MAPPED",
        trigger="MAPPING_COMPLETED",
        guard_expr="confirmed_contract_exists AND catalog_quality_passed",
        guard_inputs={
            "confirmed_contract_exists": "confirmed_contract_ref",
            "catalog_quality_passed": "catalog_quality_result_ref",
        },
        precedence=precedence,
        on_success={"to": "SCHEMA_MAPPED", "emits": "FEATURE_MAPPED"},
        on_guard_fail={"to": "MAPPING_REVIEW_FAILED", "emits": "GUARD_FAILED"},
    )


def test_install_and_load_round_trip(conn) -> None:
    install_transition_table(conn, "run", 1, [_guarded()], _registry())
    table = load_transition_table(conn, "run", 1)
    assert table.kind == "run"
    assert table.table_version == 1
    assert len(table.transitions) == 1
    t = table.transitions[0]
    assert t.guard_expr == "confirmed_contract_exists AND catalog_quality_passed"
    assert t.on_success == {"to": "SCHEMA_MAPPED", "emits": "FEATURE_MAPPED"}
    assert t.on_guard_fail == {"to": "MAPPING_REVIEW_FAILED", "emits": "GUARD_FAILED"}
    assert "MAPPING_REVIEW_FAILED" in table.states


def test_install_feature_kind(conn) -> None:
    install_transition_table(conn, "feature", 1, [_guarded()], _registry())
    table = load_transition_table(conn, "feature", 1)
    assert table.kind == "feature"
    assert len(table.transitions) == 1


def test_matches_orders_by_precedence_desc(conn) -> None:
    install_transition_table(
        conn, "run", 1, [_guarded(precedence=50), _guarded(precedence=100)], _registry()
    )
    table = load_transition_table(conn, "run", 1)
    ms = table.matches("CONFIRMED_CONTRACT", "MAPPING_COMPLETED")
    assert [m.precedence for m in ms] == [100, 50]


def test_precedence_tie_rejected_at_install(conn) -> None:
    with pytest.raises(TransitionTableError):
        install_transition_table(
            conn, "run", 1, [_guarded(precedence=100), _guarded(precedence=100)], _registry()
        )


def test_unregistered_predicate_rejected(conn) -> None:
    reg = InMemoryPredicateRegistry()  # nothing registered
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [_guarded()], reg)


def test_guard_without_on_guard_fail_rejected(conn) -> None:
    bad = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr="confirmed_contract_exists",
        guard_inputs={"confirmed_contract_exists": "confirmed_contract_ref"},
        precedence=100,
        on_success={"to": "B", "emits": "OK"},
        on_guard_fail=None,
    )
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [bad], _registry())


def test_binding_mismatch_rejected(conn) -> None:
    bad = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr="confirmed_contract_exists",
        guard_inputs={"confirmed_contract_exists": "WRONG_REF"},  # != declared_inputs
        precedence=100,
        on_success={"to": "B", "emits": "OK"},
        on_guard_fail={"to": "F", "emits": "GUARD_FAILED"},
    )
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [bad], _registry())


def test_missing_binding_rejected(conn) -> None:
    bad = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr="confirmed_contract_exists",
        guard_inputs={},  # no binding at all
        precedence=100,
        on_success={"to": "B", "emits": "OK"},
        on_guard_fail={"to": "F", "emits": "GUARD_FAILED"},
    )
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [bad], _registry())


def test_malformed_on_success_rejected(conn) -> None:
    bad = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr=None, guard_inputs={}, precedence=100,
        on_success={"to": "B"},  # missing 'emits'
        on_guard_fail=None,
    )
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "run", 1, [bad], _registry())


def test_unguarded_transition_installs(conn) -> None:
    ok = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr=None, guard_inputs={}, precedence=100,
        on_success={"to": "B", "emits": "MOVED"},
        on_guard_fail=None,
    )
    install_transition_table(conn, "run", 1, [ok], _registry())
    table = load_transition_table(conn, "run", 1)
    assert table.transitions[0].guard_expr is None


def test_unknown_kind_rejected(conn) -> None:
    with pytest.raises(TransitionTableError):
        install_transition_table(conn, "bogus", 1, [_guarded()], _registry())


def test_db_pk_blocks_identical_rows(conn) -> None:
    install_transition_table(conn, "run", 1, [_guarded()], _registry())
    with pytest.raises(psycopg.errors.UniqueViolation):
        install_transition_table(conn, "run", 1, [_guarded()], _registry())
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/state_machine/test_transition_table.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.state_machine.transition_table'`.

3. **Write minimal implementation** — `src/sp0/state_machine/transition_table.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from psycopg.types.json import Json

from sp0.contracts import PredicateRegistry
from sp0.state_machine.guard_expr import parse_guard_expr, predicate_names

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
        hits = [
            t
            for t in self.transitions
            if t.from_state == from_state and t.trigger == trigger
        ]
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
                    raise TransitionTableError(
                        f"guard predicate {name!r} not registered"
                    ) from exc
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
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/state_machine/test_transition_table.py -q`. Expected: 12 passed.

5. **Commit:**

```
git add src/sp0/state_machine/transition_table.py tests/sp0/state_machine/test_transition_table.py
git commit -m "SP-0 Phase 03: transition-table model with load-time validation (§4.1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5 — Transition engine: precedence, no fall-through, symmetric typing, pinning

**Files:**
- Create: `src/sp0/state_machine/engine.py`
- Test: `tests/sp0/state_machine/test_engine.py`

**Interfaces:**
- Consumes: `TransitionTable` (Task 4); `InMemoryPredicateRegistry`/`PredicateRegistry` (Task 3 / `sp0.contracts`); `GuardOutcome`, `GuardInputs` (from `sp0.contracts`).
- Produces:
  - Constants `GUARD_FAILED = "GUARD_FAILED"`, `TRANSITION_REJECTED = "TRANSITION_REJECTED"`.
  - `TransitionResult` (frozen dataclass): `matched: bool`, `passed: bool`, `from_state: str`, `to_state: str`, `trigger: str`, `emitted_event_type: str`, `selected_precedence: int | None`, `guard_outcome: GuardOutcome | None`, `audit_payload: Mapping[str, Any]`.
  - `evaluate_transition(table: TransitionTable, registry: PredicateRegistry, from_state: str, trigger: str, inputs: GuardInputs) -> TransitionResult`. Pure (no I/O). Selects the single highest-`precedence` matching transition; **no fall-through** on guard failure; no match ⇒ `TRANSITION_REJECTED`. The caller (Phase 04/06) appends `emitted_event_type` with `audit_payload` inside the §5.1 transaction. **Replay-against-pinned** = the caller loads the table by the event's stamped `table_version` and passes it here, so the same `(from_state, trigger)` resolves per pinned version.
  - `audit_payload` shapes: success → `{"from_state", "to_state", "trigger", "guard"}` (`guard` is `None` for unguarded, else the guard block); `GUARD_FAILED` → same with a populated `guard` block `{"guard_expr", "passed", "resolved_inputs", "per_predicate"}`; `TRANSITION_REJECTED` → `{"from_state", "trigger", "reason": "no_matching_transition"}`.

### TDD steps

1. **Write the failing test** — `tests/sp0/state_machine/test_engine.py`:

```python
from __future__ import annotations

from sp0.state_machine.engine import (
    GUARD_FAILED,
    TRANSITION_REJECTED,
    evaluate_transition,
)
from sp0.state_machine.guards import InMemoryPredicateRegistry
from sp0.state_machine.transition_table import Transition, TransitionTable
from tests.sp0.state_machine._predicates import truthy

INPUTS = {"confirmed_contract_ref": "doc_1"}


def _registry() -> InMemoryPredicateRegistry:
    reg = InMemoryPredicateRegistry()
    reg.register(truthy("confirmed_contract_exists", "confirmed_contract_ref"))
    # A SECOND, INDEPENDENT predicate over a DIFFERENT input — used by the
    # no-fall-through test's lower-precedence transition so it genuinely WOULD
    # pass on the same inputs that make the higher-precedence guard fail.
    reg.register(truthy("alt_ready", "alt_ready_ref"))
    return reg


def _guarded(table_version: int, to_state: str, emits: str, precedence: int = 100) -> Transition:
    return Transition(
        table_version=table_version,
        from_state="CONFIRMED_CONTRACT",
        to_state=to_state,
        trigger="MAPPING_COMPLETED",
        guard_expr="confirmed_contract_exists",
        guard_inputs={"confirmed_contract_exists": "confirmed_contract_ref"},
        precedence=precedence,
        on_success={"to": to_state, "emits": emits},
        on_guard_fail={"to": "MAPPING_REVIEW_FAILED", "emits": GUARD_FAILED},
    )


def _alt_guarded(to_state: str, emits: str, precedence: int) -> Transition:
    """A lower-precedence transition on the SAME (from_state, trigger) whose guard
    reads a DIFFERENT input. With the no-fall-through inputs below it evaluates True,
    so any regression that fell through to it would change the emitted event/state."""
    return Transition(
        table_version=1,
        from_state="CONFIRMED_CONTRACT",
        to_state=to_state,
        trigger="MAPPING_COMPLETED",
        guard_expr="alt_ready",
        guard_inputs={"alt_ready": "alt_ready_ref"},
        precedence=precedence,
        on_success={"to": to_state, "emits": emits},
        on_guard_fail={"to": "ALT_FAILED", "emits": GUARD_FAILED},
    )


def test_guard_pass_emits_on_success() -> None:
    table = TransitionTable("run", 1, (_guarded(1, "SCHEMA_MAPPED", "FEATURE_MAPPED"),))
    res = evaluate_transition(table, _registry(), "CONFIRMED_CONTRACT", "MAPPING_COMPLETED", INPUTS)
    assert res.matched and res.passed
    assert res.to_state == "SCHEMA_MAPPED"
    assert res.emitted_event_type == "FEATURE_MAPPED"
    assert res.selected_precedence == 100
    assert res.audit_payload["guard"]["passed"] is True
    assert res.audit_payload["guard"]["resolved_inputs"] == {"confirmed_contract_ref": "doc_1"}


def test_guard_fail_emits_guard_failed_no_fallthrough() -> None:
    # The high (prec 100) transition's guard `confirmed_contract_exists` FAILS
    # (confirmed_contract_ref == ""). The low (prec 50) transition's guard
    # `alt_ready` reads a DIFFERENT input (alt_ready_ref == "ready") and WOULD
    # PASS. "No fall-through" (§4.1) means the engine commits to the single
    # highest-precedence match and emits its GUARD_FAILED — it must NOT drop to
    # the passing prec-50 transition. A regression that re-introduced fall-through
    # would instead emit "ALT_EVENT" / "ALT_STATE", so this test now discriminates.
    high = _guarded(1, "SCHEMA_MAPPED", "FEATURE_MAPPED", precedence=100)
    low = _alt_guarded("ALT_STATE", "ALT_EVENT", precedence=50)
    table = TransitionTable("run", 1, (high, low))
    res = evaluate_transition(
        table, _registry(), "CONFIRMED_CONTRACT", "MAPPING_COMPLETED",
        {"confirmed_contract_ref": "", "alt_ready_ref": "ready"},
    )
    assert res.matched and not res.passed
    assert res.selected_precedence == 100
    assert res.emitted_event_type == GUARD_FAILED
    assert res.to_state == "MAPPING_REVIEW_FAILED"
    assert res.emitted_event_type != "ALT_EVENT"   # would-pass prec-50 was NOT chosen
    assert res.to_state != "ALT_STATE"
    assert res.audit_payload["guard"]["passed"] is False


def test_no_matching_transition_is_rejected() -> None:
    table = TransitionTable("run", 1, (_guarded(1, "SCHEMA_MAPPED", "FEATURE_MAPPED"),))
    res = evaluate_transition(table, _registry(), "SOME_OTHER_STATE", "MAPPING_COMPLETED", INPUTS)
    assert not res.matched and not res.passed
    assert res.emitted_event_type == TRANSITION_REJECTED
    assert res.to_state == "SOME_OTHER_STATE"
    assert res.audit_payload["reason"] == "no_matching_transition"


def test_unguarded_transition_passes() -> None:
    t = Transition(
        table_version=1, from_state="A", to_state="B", trigger="T",
        guard_expr=None, guard_inputs={}, precedence=100,
        on_success={"to": "B", "emits": "MOVED"}, on_guard_fail=None,
    )
    res = evaluate_transition(TransitionTable("run", 1, (t,)), _registry(), "A", "T", {})
    assert res.passed and res.to_state == "B" and res.emitted_event_type == "MOVED"
    assert res.audit_payload["guard"] is None


def test_table_version_pinning_resolves_differently() -> None:
    reg = _registry()
    v1 = TransitionTable("run", 1, (_guarded(1, "SCHEMA_MAPPED", "FEATURE_MAPPED"),))
    v2 = TransitionTable("run", 2, (_guarded(2, "OTHER_STATE", "OTHER_EVENT"),))
    r1 = evaluate_transition(v1, reg, "CONFIRMED_CONTRACT", "MAPPING_COMPLETED", INPUTS)
    r2 = evaluate_transition(v2, reg, "CONFIRMED_CONTRACT", "MAPPING_COMPLETED", INPUTS)
    assert r1.to_state == "SCHEMA_MAPPED" and r1.emitted_event_type == "FEATURE_MAPPED"
    assert r2.to_state == "OTHER_STATE" and r2.emitted_event_type == "OTHER_EVENT"
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/state_machine/test_engine.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.state_machine.engine'`.

3. **Write minimal implementation** — `src/sp0/state_machine/engine.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sp0.contracts import GuardInputs, GuardOutcome, PredicateRegistry
from sp0.state_machine.transition_table import TransitionTable

GUARD_FAILED = "GUARD_FAILED"
TRANSITION_REJECTED = "TRANSITION_REJECTED"


@dataclass(frozen=True, slots=True)
class TransitionResult:
    matched: bool
    passed: bool
    from_state: str
    to_state: str
    trigger: str
    emitted_event_type: str
    selected_precedence: int | None
    guard_outcome: GuardOutcome | None
    audit_payload: Mapping[str, Any]


def _guard_block(guard_expr: str, outcome: GuardOutcome) -> dict[str, Any]:
    return {
        "guard_expr": guard_expr,
        "passed": outcome.passed,
        "resolved_inputs": dict(outcome.resolved_inputs),
        "per_predicate": dict(outcome.per_predicate),
    }


def evaluate_transition(
    table: TransitionTable,
    registry: PredicateRegistry,
    from_state: str,
    trigger: str,
    inputs: GuardInputs,
) -> TransitionResult:
    candidates = table.matches(from_state, trigger)
    if not candidates:
        return TransitionResult(
            matched=False,
            passed=False,
            from_state=from_state,
            to_state=from_state,
            trigger=trigger,
            emitted_event_type=TRANSITION_REJECTED,
            selected_precedence=None,
            guard_outcome=None,
            audit_payload={
                "from_state": from_state,
                "trigger": trigger,
                "reason": "no_matching_transition",
            },
        )
    selected = candidates[0]  # highest precedence; ties impossible (validated at install)
    if selected.guard_expr is None:
        to_state = selected.on_success["to"]
        return TransitionResult(
            matched=True,
            passed=True,
            from_state=from_state,
            to_state=to_state,
            trigger=trigger,
            emitted_event_type=selected.on_success["emits"],
            selected_precedence=selected.precedence,
            guard_outcome=None,
            audit_payload={
                "from_state": from_state,
                "to_state": to_state,
                "trigger": trigger,
                "guard": None,
            },
        )
    outcome = registry.evaluate(selected.guard_expr, inputs)
    guard = _guard_block(selected.guard_expr, outcome)
    if outcome.passed:
        to_state = selected.on_success["to"]
        emitted = selected.on_success["emits"]
    else:
        # No fall-through to lower precedence (§4.1): route to on_guard_fail.
        assert selected.on_guard_fail is not None  # guaranteed by install validation
        to_state = selected.on_guard_fail["to"]
        emitted = selected.on_guard_fail["emits"]
    return TransitionResult(
        matched=True,
        passed=outcome.passed,
        from_state=from_state,
        to_state=to_state,
        trigger=trigger,
        emitted_event_type=emitted,
        selected_precedence=selected.precedence,
        guard_outcome=outcome,
        audit_payload={
            "from_state": from_state,
            "to_state": to_state,
            "trigger": trigger,
            "guard": guard,
        },
    )
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/state_machine/test_engine.py -q`. Expected: 5 passed.

5. **Commit:**

```
git add src/sp0/state_machine/engine.py tests/sp0/state_machine/test_engine.py
git commit -m "SP-0 Phase 03: transition engine (precedence, no fall-through, pinning)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6 — Phase-03 event-type registration

**Files:**
- Create: `src/sp0/state_machine/event_types.py`
- Create: `tests/sp0/state_machine/conftest.py`
- Test: `tests/sp0/state_machine/test_event_types.py`

**Interfaces:**
- Consumes: the event registry's `register_schema(type_name, schema_version, json_schema, owner, *, status="active")` and `validate(type_name, schema_version, body)` (Phase 01); `SchemaValidationError` (from `sp0.contracts`); the **`event_registry()` accessor function** (Phase 01, `sp0.events.registry`) returning the singleton `EventSchemaRegistry` — it must be **called** (`event_registry().register_schema(...)`), not used as a bare instance.
- Produces:
  - Event-type name constants: `GUARD_FAILED`, `TRANSITION_REJECTED` (re-exported from `engine`), `WORKFLOW_VERSION_MIGRATED`, `FEATURE_LIFECYCLE_VERSION_MIGRATED`.
  - JSON-schema dicts `_GUARD_FAILED_SCHEMA`, `_TRANSITION_REJECTED_SCHEMA`, `_MIGRATION_SCHEMA` (all `schema_version=1`, `owner="sp0-state-machine"`).
  - `register_state_machine_event_types(registry) -> None` — registers all four Phase-03 event types at version 1. Takes the registry as an explicit argument (so it works against both a real `event_registry()` and a recording fake).
- Also produces the package-level test fixture: `conftest.py` registers the four event types **and** a test-only `SM_TEST_SEED` (v1) schema into the singleton returned by `event_registry()`. **It MUST be function-scoped autouse and depend on Phase 01's `_reset_registry`** — Phase 01's `tests/conftest.py` resets the registry singleton before AND after every test, so a once-per-session registration would be wiped before the first schema-dependent test runs. Depending on `_reset_registry` forces this fixture to run *after* each per-test reset, repopulating the freshly-reset singleton so `append_event` can validate the SM types in Task 7.

### TDD steps

1. **Write the package conftest** — `tests/sp0/state_machine/conftest.py`:

```python
from __future__ import annotations

import pytest

from sp0.events.registry import event_registry
from sp0.state_machine.event_types import register_state_machine_event_types


@pytest.fixture(autouse=True)
def _register_state_machine_event_types(_reset_registry) -> None:
    """Register Phase-03 event types + a seed type into the shared event registry
    so append_event can validate them (Task 7).

    Function-scoped and autouse. It depends on Phase 01's `_reset_registry`
    fixture (defined in the repo-root tests/conftest.py, also function-scoped
    autouse) PURELY to force ordering: pytest sets up `_reset_registry` first
    (running its pre-yield reset_event_registry(), which replaces the singleton
    with a fresh, EMPTY EventSchemaRegistry), and only then runs this fixture,
    which repopulates that fresh singleton. A session-scoped registration here
    would be wiped by `_reset_registry` before every schema-dependent test and
    the SM schemas would be missing. `event_registry()` is the accessor function
    (Phase 01) and must be CALLED to get the live singleton."""
    registry = event_registry()
    register_state_machine_event_types(registry)
    registry.register_schema(
        "SM_TEST_SEED", 1, {"type": "object"}, owner="sp0-state-machine-test"
    )
```

> Why depend on `_reset_registry` instead of just being function-scoped? Two autouse fixtures at the same (function) scope have no guaranteed relative order unless one requests the other. Without the dependency, this fixture could run *before* `_reset_registry`'s pre-yield reset, which would then wipe the SM schemas. Declaring `_reset_registry` as a parameter makes pytest order it strictly after the reset.

2. **Write the failing test** — `tests/sp0/state_machine/test_event_types.py`:

```python
from __future__ import annotations

import pytest

from sp0.contracts import SchemaValidationError
from sp0.events.registry import event_registry
from sp0.state_machine.event_types import (
    FEATURE_LIFECYCLE_VERSION_MIGRATED,
    WORKFLOW_VERSION_MIGRATED,
    register_state_machine_event_types,
)
from sp0.state_machine.engine import GUARD_FAILED, TRANSITION_REJECTED


class _RecordingRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def register_schema(self, type_name, schema_version, json_schema, owner, *, status="active"):
        self.calls.append((type_name, schema_version, owner))


def test_registers_all_four_types_at_v1() -> None:
    rec = _RecordingRegistry()
    register_state_machine_event_types(rec)
    registered = {(t, v) for (t, v, _owner) in rec.calls}
    assert registered == {
        (GUARD_FAILED, 1),
        (TRANSITION_REJECTED, 1),
        (WORKFLOW_VERSION_MIGRATED, 1),
        (FEATURE_LIFECYCLE_VERSION_MIGRATED, 1),
    }
    assert all(owner == "sp0-state-machine" for (_t, _v, owner) in rec.calls)


def test_guard_failed_schema_accepts_engine_payload() -> None:
    # Registered into the shared registry by the autouse conftest fixture (per test).
    event_registry().validate(
        GUARD_FAILED,
        1,
        {
            "from_state": "CONFIRMED_CONTRACT",
            "to_state": "MAPPING_REVIEW_FAILED",
            "trigger": "MAPPING_COMPLETED",
            "guard": {
                "guard_expr": "confirmed_contract_exists",
                "passed": False,
                "resolved_inputs": {"confirmed_contract_ref": "doc_1"},
                "per_predicate": {"confirmed_contract_exists": False},
            },
        },
    )


def test_guard_failed_schema_rejects_missing_guard() -> None:
    with pytest.raises(SchemaValidationError):
        event_registry().validate(
            GUARD_FAILED,
            1,
            {"from_state": "A", "to_state": "B", "trigger": "T"},
        )


def test_transition_rejected_schema_validates() -> None:
    event_registry().validate(
        TRANSITION_REJECTED,
        1,
        {"from_state": "A", "trigger": "T", "reason": "no_matching_transition"},
    )
    with pytest.raises(SchemaValidationError):
        event_registry().validate(TRANSITION_REJECTED, 1, {"from_state": "A"})


def test_migration_schema_validates() -> None:
    event_registry().validate(
        WORKFLOW_VERSION_MIGRATED,
        1,
        {"from_table_version": 1, "to_table_version": 2, "current_state": "DRAFT"},
    )
    with pytest.raises(SchemaValidationError):
        event_registry().validate(WORKFLOW_VERSION_MIGRATED, 1, {"to_table_version": 2})
```

3. **Run it, expect FAIL** — `python -m pytest tests/sp0/state_machine/test_event_types.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.state_machine.event_types'`.

4. **Write minimal implementation** — `src/sp0/state_machine/event_types.py`:

```python
from __future__ import annotations

from typing import Any

from sp0.state_machine.engine import GUARD_FAILED, TRANSITION_REJECTED

WORKFLOW_VERSION_MIGRATED = "WORKFLOW_VERSION_MIGRATED"
FEATURE_LIFECYCLE_VERSION_MIGRATED = "FEATURE_LIFECYCLE_VERSION_MIGRATED"

_OWNER = "sp0-state-machine"

_GUARD_FAILED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from_state", "to_state", "trigger", "guard"],
    "properties": {
        "from_state": {"type": "string"},
        "to_state": {"type": "string"},
        "trigger": {"type": "string"},
        "guard": {
            "type": "object",
            "required": ["guard_expr", "passed", "resolved_inputs", "per_predicate"],
            "properties": {
                "guard_expr": {"type": "string"},
                "passed": {"type": "boolean"},
                "resolved_inputs": {"type": "object"},
                "per_predicate": {"type": "object"},
            },
        },
    },
}

_TRANSITION_REJECTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from_state", "trigger", "reason"],
    "properties": {
        "from_state": {"type": "string"},
        "trigger": {"type": "string"},
        "reason": {"type": "string"},
    },
}

_MIGRATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from_table_version", "to_table_version", "current_state"],
    "properties": {
        "from_table_version": {"type": "integer"},
        "to_table_version": {"type": "integer"},
        "current_state": {"type": "string"},
    },
}


def register_state_machine_event_types(registry: Any) -> None:
    """Register the four Phase-03 event types (version 1) into an event registry."""
    registry.register_schema(GUARD_FAILED, 1, _GUARD_FAILED_SCHEMA, _OWNER)
    registry.register_schema(TRANSITION_REJECTED, 1, _TRANSITION_REJECTED_SCHEMA, _OWNER)
    registry.register_schema(WORKFLOW_VERSION_MIGRATED, 1, _MIGRATION_SCHEMA, _OWNER)
    registry.register_schema(
        FEATURE_LIFECYCLE_VERSION_MIGRATED, 1, _MIGRATION_SCHEMA, _OWNER
    )
```

5. **Run tests, expect PASS** — `python -m pytest tests/sp0/state_machine/test_event_types.py -q`. Expected: 5 passed.

6. **Commit:**

```
git add src/sp0/state_machine/event_types.py tests/sp0/state_machine/conftest.py tests/sp0/state_machine/test_event_types.py
git commit -m "SP-0 Phase 03: register GUARD_FAILED/TRANSITION_REJECTED/*_MIGRATED event types

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7 — Audited table-version migration commands (run + feature)

**Files:**
- Create: `src/sp0/state_machine/migrations.py`
- Test: `tests/sp0/state_machine/test_migrations.py`

**Interfaces:**
- Consumes: `append_event`, `load_stream` (Phase 01, `sp0.events.store`); `NewEvent`, `EventEnvelope`, `IdentityEnvelope`, `ProvenanceEnvelope`, `ConcurrencyError` (from `sp0.contracts`); `load_transition_table` (Task 4); `WORKFLOW_VERSION_MIGRATED`, `FEATURE_LIFECYCLE_VERSION_MIGRATED` (Task 6); the `conn` fixture (Phase 01, `tests/conftest.py`); the autouse per-test event-type fixture (Task 6 conftest, which registers the `*_MIGRATED` and `SM_TEST_SEED` schemas into `event_registry()` so `append_event`'s internal validation passes). Note: `migrations.py` itself does NOT import `event_registry` — `append_event` validates against the singleton internally; the tests only rely on the conftest having populated it.

> **AUTHZ / dual-control boundary (read before use).** `migrate_workflow_version` / `migrate_feature_lifecycle_version` are the §4.4 lifecycle commands `migrate_workflow_version` / `migrate_feature_lifecycle_version`, but the functions defined here are **low-level primitives**: they perform NO authorization, NO `command_idempotency`, and are NOT routed through `execute_command`. They MUST be invoked ONLY by Phase 06's `execute_command` (action vocabulary `migrate_workflow_version` / `migrate_feature_lifecycle_version`), which enforces the `authz_policy` check (platform-admin role, §6.2), command-level idempotency, and the degraded-aggregate block. **No other caller may invoke these functions directly** — doing so bypasses authorization and is an unauthorized-migration hole. Phase 06 is responsible for wiring these into the command catalog; this phase only provides the audited append primitive + validation.
- Produces:
  - `MigrationError(Exception)`.
  - `MigrationResult` (frozen dataclass): `event: EventEnvelope`, `from_table_version: int`, `to_table_version: int`.
  - `migrate_workflow_version(conn, run_id, *, to_table_version, current_state, expected_version, actor, provenance) -> MigrationResult`.
  - `migrate_feature_lifecycle_version(conn, feature_id, *, to_table_version, current_state, expected_version, actor, provenance) -> MigrationResult`.
  - Both: read the current pinned `table_version` from the stream's last event (authoritative old→new mapping); reject downgrades (`to_table_version <= from_version`), unknown target versions (no rows), and states that would be stranded (`current_state` not in the target table's `states`); then append one audited `*_MIGRATED` event stamped with `table_version = to_table_version` and payload `{from_table_version, to_table_version, current_state}`. OCC enforced via `expected_version` (Phase 01 raises `ConcurrencyError`). Earlier events keep their old `table_version` ⇒ replay-against-pinned holds.

### TDD steps

1. **Write the failing test** — `tests/sp0/state_machine/test_migrations.py`:

```python
from __future__ import annotations

import uuid

import pytest

from sp0.contracts import (
    ConcurrencyError,
    IdentityEnvelope,
    NewEvent,
    ProvenanceEnvelope,
)
from sp0.events.store import append_event, load_stream
from sp0.state_machine.guards import InMemoryPredicateRegistry
from sp0.state_machine.migrations import (
    MigrationError,
    migrate_feature_lifecycle_version,
    migrate_workflow_version,
)
from sp0.state_machine.transition_table import Transition, install_transition_table

ACTOR = IdentityEnvelope(
    subject="user:test",
    actor_kind="human",
    authenticated=True,
    auth_method="oidc",
    role_claims=(),
)
PROV = ProvenanceEnvelope(
    artifact_type="APPROVAL_RECORD",
    schema_version=1,
    producing_component="sp0-test@0.0.0",
)


def _draft_transition(table_version: int) -> Transition:
    return Transition(
        table_version=table_version,
        from_state="DRAFT",
        to_state="CONFIRMED_CONTRACT",
        trigger="CONFIRM",
        guard_expr=None,
        guard_inputs={},
        precedence=100,
        on_success={"to": "CONFIRMED_CONTRACT", "emits": "CONTRACT_CONFIRMED"},
        on_guard_fail=None,
    )


def _seed_run(conn, *, table_version: int) -> str:
    run_id = f"run_{uuid.uuid4().hex}"
    append_event(
        conn,
        NewEvent(
            aggregate="run",
            aggregate_id=run_id,
            type="SM_TEST_SEED",
            schema_version=1,
            payload={},
            actor=ACTOR,
            provenance=PROV,
            run_id=run_id,
        ),
        expected_version=0,
        table_version=table_version,
    )
    return run_id


def test_migrate_workflow_version_appends_audited_event(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)

    result = migrate_workflow_version(
        conn, run_id,
        to_table_version=2, current_state="DRAFT", expected_version=1,
        actor=ACTOR, provenance=PROV,
    )

    assert result.from_table_version == 1
    assert result.to_table_version == 2
    assert result.event.type == "WORKFLOW_VERSION_MIGRATED"
    assert result.event.table_version == 2
    assert result.event.payload == {
        "from_table_version": 1,
        "to_table_version": 2,
        "current_state": "DRAFT",
    }


def test_earlier_events_keep_old_table_version(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)
    migrate_workflow_version(
        conn, run_id, to_table_version=2, current_state="DRAFT", expected_version=1,
        actor=ACTOR, provenance=PROV,
    )
    stream = load_stream(conn, "run", run_id)
    assert [e.table_version for e in stream] == [1, 2]


def test_downgrade_rejected(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=2)
    with pytest.raises(MigrationError):
        migrate_workflow_version(
            conn, run_id, to_table_version=1, current_state="DRAFT", expected_version=1,
            actor=ACTOR, provenance=PROV,
        )


def test_unknown_target_version_rejected(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)
    with pytest.raises(MigrationError):
        migrate_workflow_version(
            conn, run_id, to_table_version=2, current_state="DRAFT", expected_version=1,
            actor=ACTOR, provenance=PROV,
        )


def test_stranded_state_rejected(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)
    with pytest.raises(MigrationError):
        migrate_workflow_version(
            conn, run_id, to_table_version=2, current_state="STATE_NOT_IN_V2",
            expected_version=1, actor=ACTOR, provenance=PROV,
        )


def test_occ_conflict_raises(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)
    with pytest.raises(ConcurrencyError):
        migrate_workflow_version(
            conn, run_id, to_table_version=2, current_state="DRAFT",
            expected_version=99, actor=ACTOR, provenance=PROV,
        )


def test_migrate_feature_lifecycle_version(conn) -> None:
    install_transition_table(conn, "feature", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "feature", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    feature_id = f"feat_{uuid.uuid4().hex}"
    append_event(
        conn,
        NewEvent(
            aggregate="feature", aggregate_id=feature_id, type="SM_TEST_SEED",
            schema_version=1, payload={}, actor=ACTOR, provenance=PROV,
            feature_id=feature_id,
        ),
        expected_version=0,
        table_version=1,
    )
    result = migrate_feature_lifecycle_version(
        conn, feature_id, to_table_version=2, current_state="DRAFT", expected_version=1,
        actor=ACTOR, provenance=PROV,
    )
    assert result.event.type == "FEATURE_LIFECYCLE_VERSION_MIGRATED"
    assert result.event.table_version == 2
    assert result.event.feature_id == feature_id
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/state_machine/test_migrations.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.state_machine.migrations'`.

3. **Write minimal implementation** — `src/sp0/state_machine/migrations.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sp0.contracts import (
    EventEnvelope,
    IdentityEnvelope,
    NewEvent,
    ProvenanceEnvelope,
)
from sp0.events.store import append_event, load_stream
from sp0.state_machine.event_types import (
    FEATURE_LIFECYCLE_VERSION_MIGRATED,
    WORKFLOW_VERSION_MIGRATED,
)
from sp0.state_machine.transition_table import load_transition_table


class MigrationError(Exception):
    """Raised when a table-version migration is invalid (downgrade, unknown
    target version, or a state that would be stranded by the new table, §4.2)."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    event: EventEnvelope
    from_table_version: int
    to_table_version: int


def _migrate(
    conn: Any,
    *,
    aggregate: str,
    aggregate_id: str,
    kind: str,
    event_type: str,
    to_table_version: int,
    current_state: str,
    expected_version: int,
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope,
) -> MigrationResult:
    stream = load_stream(conn, aggregate, aggregate_id)
    if not stream:
        raise MigrationError(f"{aggregate} {aggregate_id!r} has no events to migrate")
    from_version = stream[-1].table_version
    if to_table_version <= from_version:
        raise MigrationError(
            f"to_table_version {to_table_version} must be newer than current {from_version}"
        )
    target = load_transition_table(conn, kind, to_table_version)
    if not target.transitions:
        raise MigrationError(
            f"{kind} transition table version {to_table_version} does not exist"
        )
    if current_state not in target.states:
        raise MigrationError(
            f"current_state {current_state!r} not present in {kind} table "
            f"v{to_table_version}; migration would strand the aggregate"
        )
    new_event = NewEvent(
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        type=event_type,
        schema_version=1,
        payload={
            "from_table_version": from_version,
            "to_table_version": to_table_version,
            "current_state": current_state,
        },
        actor=actor,
        provenance=provenance,
        feature_id=aggregate_id if aggregate == "feature" else None,
        run_id=aggregate_id if aggregate == "run" else None,
    )
    event = append_event(
        conn, new_event, expected_version=expected_version, table_version=to_table_version
    )
    return MigrationResult(
        event=event, from_table_version=from_version, to_table_version=to_table_version
    )


def migrate_workflow_version(
    conn: Any,
    run_id: str,
    *,
    to_table_version: int,
    current_state: str,
    expected_version: int,
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope,
) -> MigrationResult:
    return _migrate(
        conn,
        aggregate="run",
        aggregate_id=run_id,
        kind="run",
        event_type=WORKFLOW_VERSION_MIGRATED,
        to_table_version=to_table_version,
        current_state=current_state,
        expected_version=expected_version,
        actor=actor,
        provenance=provenance,
    )


def migrate_feature_lifecycle_version(
    conn: Any,
    feature_id: str,
    *,
    to_table_version: int,
    current_state: str,
    expected_version: int,
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope,
) -> MigrationResult:
    return _migrate(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        kind="feature",
        event_type=FEATURE_LIFECYCLE_VERSION_MIGRATED,
        to_table_version=to_table_version,
        current_state=current_state,
        expected_version=expected_version,
        actor=actor,
        provenance=provenance,
    )
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/state_machine/test_migrations.py -q`. Expected: 7 passed.

5. **Commit:**

```
git add src/sp0/state_machine/migrations.py tests/sp0/state_machine/test_migrations.py
git commit -m "SP-0 Phase 03: audited table-version migration commands (§4.2)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 03 completion check

Run the whole phase suite and confirm green before handing off:

```
python -m pytest tests/sp0/state_machine -q
```

Expected: all tests pass (schema 3 + guard_expr 9 + guards 10 + transition_table 12 + engine 5 + event_types 5 + migrations 7).

**§12 coverage owned by this phase:**
- *Guard-failure auditing* — `GUARD_FAILED`/`TRANSITION_REJECTED` carry `from_state`/`to_state`/`trigger` + the guard block (`resolved_inputs`, `per_predicate`, `passed`); no fall-through; precedence ties rejected at registration (Tasks 4, 5, 6).
- *Both table versions* — in-flight run on the old `run_transition_table` version is unaffected (earlier events keep their stamp); feature-lifecycle versioning + replay-against-pinned works and is pinned (Tasks 1, 4, 5, 7).
- *Guard purity* — predicates mechanically receive only declared inputs and cannot read mutable projections (Task 3).
