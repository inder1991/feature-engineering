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

It ALSO defeats the positional/alias bypasses a naive keyword-only scan misses (final review M3):

  * a POSITIONAL ``authenticated`` argument — ``IdentityEnvelope(subject, kind, True, ...)`` — is
    flagged unless it is a literal ``False`` (``authenticated`` is the 3rd field, index
    ``_AUTHENTICATED_POS``);
  * a ``*args`` splat (``IdentityEnvelope(*forged)``) cannot be proven safe, so it is flagged;
  * an ALIASED import (``from ... import IdentityEnvelope as IE``) or a local rebinding
    (``X = IdentityEnvelope``) is resolved, so ``IE(...)`` / ``X(...)`` is still recognised as an
    ``IdentityEnvelope`` construction and checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "featuregen"

# Positional index of the ``authenticated`` field in IdentityEnvelope (contracts/envelopes.py):
# subject(0), actor_kind(1), authenticated(2), auth_method(3), role_claims(4), ...
_AUTHENTICATED_POS = 2

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


def _identity_envelope_aliases(tree: ast.AST) -> set[str]:
    """Every local NAME in a module that refers to ``IdentityEnvelope`` — the bare name plus any
    ``from ... import IdentityEnvelope as IE`` alias and any local ``X = IdentityEnvelope`` (or
    ``X = <existing alias>``) rebinding, resolved to a fixpoint. So a construction via an aliased
    import or a rebinding (``IE(...)`` / ``X(...)``) is still caught by ``_identity_envelope_calls``.
    """
    aliases = {"IdentityEnvelope"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "IdentityEnvelope":
                    aliases.add(a.asname or a.name)
    changed = True
    while changed:  # fixpoint so a chain (X = IdentityEnvelope; Y = X) resolves regardless of order
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            val = node.value
            refers = (isinstance(val, ast.Name) and val.id in aliases) or (
                isinstance(val, ast.Attribute) and val.attr == "IdentityEnvelope"
            )
            if not refers:
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id not in aliases:
                    aliases.add(t.id)
                    changed = True
    return aliases


def _identity_envelope_calls(tree: ast.AST, aliases: set[str]) -> list[ast.Call]:
    """Every ``IdentityEnvelope(...)`` call in a module — called by an alias NAME (``IdentityEnvelope``,
    an aliased import, or a local rebinding) OR as an attribute whose attr is ``IdentityEnvelope``
    (``module.IdentityEnvelope(...)``)."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in aliases:
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "IdentityEnvelope":
            calls.append(node)
    return calls


def _mints_non_false_authenticated(call: ast.Call) -> bool:
    """True unless the call pins ``authenticated`` to a literal ``False`` and cannot otherwise
    smuggle a non-``False`` value.

    The ONLY safe non-sanctioned form is an explicit ``authenticated=False`` literal (keyword or
    positional). Anything else that can yield ``authenticated=True`` is flagged:
      * a POSITIONAL argument in the ``authenticated`` slot (index ``_AUTHENTICATED_POS``) that is
        not a literal ``False``;
      * a ``*args`` splat, which can place ANY value positionally — cannot be proven safe;
      * a keyword ``authenticated=`` that is a literal ``True`` or ANY computed expression (a name, a
        ``d["authenticated"]`` subscript, a call, ...);
      * a bare ``**mapping`` that could carry an ``authenticated`` key.
    """
    # A *args splat can drop any value into the positional authenticated slot — cannot prove safe.
    if any(isinstance(a, ast.Starred) for a in call.args):
        return True
    # A positional authenticated argument: flag unless it is a literal False.
    if len(call.args) > _AUTHENTICATED_POS:
        v = call.args[_AUTHENTICATED_POS]
        if not (isinstance(v, ast.Constant) and v.value is False):
            return True
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
        aliases = _identity_envelope_aliases(tree)
        offenders += [
            f"{rel}:{call.lineno}"
            for call in _identity_envelope_calls(tree, aliases)
            if _mints_non_false_authenticated(call)
        ]
    assert offenders == [], (
        "IdentityEnvelope minted with a non-False authenticated= argument "
        f"(literal, positional, computed, aliased, or **-smuggled) outside sanctioned "
        f"trust roots: {offenders}"
    )


def _snippet_flags_mint(code: str) -> bool:
    """Run the guard's AST checker over a code snippet: True if it would flag a non-False
    authenticated IdentityEnvelope construction (positional, keyword, aliased, or splatted)."""
    tree = ast.parse(code)
    aliases = _identity_envelope_aliases(tree)
    return any(_mints_non_false_authenticated(c) for c in _identity_envelope_calls(tree, aliases))


def test_guard_flags_positional_true_authenticated() -> None:
    # M3: a positional authenticated=True bypasses a keyword-only scan; the guard must catch it.
    assert _snippet_flags_mint('IdentityEnvelope("s", "human", True, "pw", ())') is True


def test_guard_allows_positional_false_authenticated() -> None:
    # The one safe positional form: an explicit literal False in the authenticated slot.
    assert _snippet_flags_mint('IdentityEnvelope("s", "human", False, "pw", ())') is False


def test_guard_flags_star_args_splat() -> None:
    # A *args splat can drop a forged value into the authenticated slot — cannot prove safe.
    assert _snippet_flags_mint("IdentityEnvelope(*forged_args)") is True


def test_guard_flags_aliased_import_construction() -> None:
    # M3: an aliased import forges authenticated=True while dodging the bare-name scan.
    code = (
        "from featuregen.contracts.envelopes import IdentityEnvelope as IE\n"
        'IE(subject="s", actor_kind="human", authenticated=True, auth_method="pw", role_claims=())'
    )
    assert _snippet_flags_mint(code) is True


def test_guard_flags_aliased_import_positional_construction() -> None:
    # Alias + positional combined — both bypasses at once.
    code = (
        "from featuregen.contracts.envelopes import IdentityEnvelope as IE\n"
        'IE("s", "human", True, "pw", ())'
    )
    assert _snippet_flags_mint(code) is True


def test_guard_flags_local_rebinding_construction() -> None:
    # M3: a local rebinding (X = IdentityEnvelope) then X(authenticated=True) must still be caught.
    assert _snippet_flags_mint("X = IdentityEnvelope\nX(authenticated=True, subject='s')") is True


def test_guard_still_allows_keyword_false_via_alias() -> None:
    # Aliasing is fine as long as authenticated stays a literal False.
    code = (
        "from featuregen.contracts.envelopes import IdentityEnvelope as IE\n"
        "IE(subject='s', actor_kind='human', authenticated=False, auth_method='pw', role_claims=())"
    )
    assert _snippet_flags_mint(code) is False


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
