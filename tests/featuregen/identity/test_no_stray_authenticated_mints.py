"""Grep guard for the in-process authenticated-mint boundary (SP-0.5 BLOCKER #1 hardening).

Python cannot seal a frozen dataclass: any module can, in principle, write
``IdentityEnvelope(authenticated=True)`` directly. So the *enforceable* control is this static
audit. It fails if any module in ``src/featuregen/`` OUTSIDE the sanctioned trust roots:

  * constructs an ``IdentityEnvelope`` with a non-``False`` ``authenticated=`` argument — whether a
    literal (``authenticated=True``) OR a computed value (``authenticated=d["authenticated"]``,
    ``authenticated=x``), OR a bare ``**`` unpacking that could smuggle ``authenticated=True`` — ,
  * references the private trust capability (``_TRUST_CAPABILITY``), or
  * calls the sanctioned ``mint_trusted_identity`` factory.

Every authenticated principal must therefore funnel through either a verifier (which proves a
token before minting) or ``mint_trusted_identity`` in exactly the two internal trust roots that
legitimately reconstruct/produce an authenticated actor without a token: the write-once event
store (``events/serde.py``) and the durable timer runtime (``aggregates/activation.py``). The
capability itself lives only inside the ``identity/`` package.

The ``authenticated=`` scan is AST-based (not a text regex) so it (a) sees the whole call even when
it spans many lines, (b) catches *computed* authenticated arguments — the exact class of bypass a
literal ``authenticated=True`` regex misses — and (c) never trips on docstrings or comments that
merely mention ``authenticated=True``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "featuregen"

# The sanctioned trust roots (SP-0.5 BLOCKER #1): the identity package DEFINES the capability +
# factory (and mentions ``authenticated=True`` in its docstrings), and the two internal trust roots
# — the write-once event store and the durable timer runtime — legitimately mint an authenticated
# principal without a token via the factory. Nothing else in the tree may.
_SANCTIONED = ("identity/", "events/serde.py", "aggregates/activation.py")
_AUTH_MINT_ALLOWED = _SANCTIONED
_FACTORY_ALLOWED = _SANCTIONED
# The private capability object itself must never be NAMED outside the identity package — even the
# two trust roots reach it only indirectly, through ``mint_trusted_identity``.
_CAPABILITY_ALLOWED = ("identity/",)


def _py_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _rel(p: Path) -> str:
    return p.relative_to(_SRC).as_posix()


def _is_allowed(rel: str, allowed: tuple[str, ...]) -> bool:
    return any(rel == a or rel.startswith(a) for a in allowed)


def _identity_envelope_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``IdentityEnvelope(...)`` call in a module, whether called by bare name or attribute."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "IdentityEnvelope":
            calls.append(node)
    return calls


def _mints_non_false_authenticated(call: ast.Call) -> bool:
    """True unless the call pins ``authenticated`` to a literal ``False`` and never ``**``-unpacks.

    The ONLY safe non-sanctioned form is an explicit ``authenticated=False`` literal. Anything else
    that can yield ``authenticated=True`` — a literal ``True``, ANY computed expression (a name, a
    ``d["authenticated"]`` subscript, a call, ...), or a bare ``**mapping`` that could carry an
    ``authenticated`` key — is flagged.
    """
    star_unpack = False
    for kw in call.keywords:
        if kw.arg == "authenticated":
            v = kw.value
            return not (isinstance(v, ast.Constant) and v.value is False)
        if kw.arg is None:  # ``**mapping`` — could smuggle authenticated=True
            star_unpack = True
    return star_unpack


def test_no_non_false_authenticated_mint_outside_sanctioned_modules() -> None:
    offenders: list[str] = []
    for p in _py_files():
        rel = _rel(p)
        if _is_allowed(rel, _AUTH_MINT_ALLOWED):
            continue
        tree = ast.parse(p.read_text())
        offenders += [
            f"{rel}:{call.lineno}"
            for call in _identity_envelope_calls(tree)
            if _mints_non_false_authenticated(call)
        ]
    assert offenders == [], (
        "IdentityEnvelope minted with a non-False authenticated= argument "
        f"(literal, computed, or **-smuggled) outside sanctioned trust roots: {offenders}"
    )


def test_private_capability_not_referenced_outside_identity() -> None:
    offenders = [
        _rel(p)
        for p in _py_files()
        if not _is_allowed(_rel(p), _CAPABILITY_ALLOWED) and "_TRUST_CAPABILITY" in p.read_text()
    ]
    assert offenders == [], f"private trust capability leaked to: {offenders}"


def test_mint_trusted_identity_only_in_sanctioned_trust_roots() -> None:
    offenders = [
        _rel(p)
        for p in _py_files()
        if not _is_allowed(_rel(p), _FACTORY_ALLOWED) and "mint_trusted_identity" in p.read_text()
    ]
    assert offenders == [], (
        f"mint_trusted_identity called outside sanctioned trust roots: {offenders}"
    )
