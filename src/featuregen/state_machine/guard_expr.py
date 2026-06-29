from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


class GuardExprError(Exception):
    """Raised when a guard expression is malformed (load-time error, §4.1)."""


@dataclass(frozen=True, slots=True)
class Pred:
    name: str


@dataclass(frozen=True, slots=True)
class Not:
    operand: Node


@dataclass(frozen=True, slots=True)
class And:
    left: Node
    right: Node


@dataclass(frozen=True, slots=True)
class Or:
    left: Node
    right: Node


Node = Pred | Not | And | Or

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
            raise GuardExprError(f"trailing tokens: {self._tokens[self._i :]}")
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
