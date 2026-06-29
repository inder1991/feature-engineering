from __future__ import annotations

import pytest

from featuregen.state_machine.guard_expr import (
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
