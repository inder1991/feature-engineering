"""Grep guard for the in-process authenticated-mint boundary (SP-0.5 BLOCKER #1 hardening).

Python cannot seal a frozen dataclass: any module can, in principle, write
``IdentityEnvelope(authenticated=True)`` directly. So the *enforceable* control is this static
audit. It fails if any module in ``src/featuregen/`` OUTSIDE the sanctioned trust roots:

  * constructs ``authenticated=True`` directly,
  * references the private trust capability (``_TRUST_CAPABILITY``), or
  * calls the sanctioned ``mint_trusted_identity`` factory.

Every authenticated principal must therefore funnel through either a verifier (which proves a
token before minting) or ``mint_trusted_identity`` in exactly the two internal trust roots that
legitimately reconstruct/produce an authenticated actor without a token: the write-once event
store (``events/serde.py``) and the durable timer runtime (``aggregates/activation.py``). The
capability itself lives only inside the ``identity/`` package.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "featuregen"

# The sanctioned trust roots (SP-0.5 BLOCKER #1): the identity package DEFINES the capability +
# factory (and mentions ``authenticated=True`` in its docstrings), and the two internal trust roots
# — the write-once event store and the durable timer runtime — legitimately mint an authenticated
# principal without a token via the factory. Nothing else in the tree may.
_SANCTIONED = ("identity/", "events/serde.py", "aggregates/activation.py")
_AUTH_TRUE_ALLOWED = _SANCTIONED
_FACTORY_ALLOWED = _SANCTIONED
# The private capability object itself must never be NAMED outside the identity package — even the
# two trust roots reach it only indirectly, through ``mint_trusted_identity``.
_CAPABILITY_ALLOWED = ("identity/",)

_AUTH_TRUE = re.compile(r"authenticated\s*=\s*True")


def _py_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _rel(p: Path) -> str:
    return p.relative_to(_SRC).as_posix()


def _is_allowed(rel: str, allowed: tuple[str, ...]) -> bool:
    return any(rel == a or rel.startswith(a) for a in allowed)


def test_no_authenticated_true_outside_sanctioned_modules() -> None:
    offenders = [
        _rel(p)
        for p in _py_files()
        if not _is_allowed(_rel(p), _AUTH_TRUE_ALLOWED) and _AUTH_TRUE.search(p.read_text())
    ]
    assert offenders == [], f"stray authenticated=True construction in: {offenders}"


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
