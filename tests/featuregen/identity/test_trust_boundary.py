"""The auditable trust boundary for authenticated identity minting (SP-0.5 BLOCKER #1).

``identity/_trust.py`` has always DOCUMENTED this boundary and named a guard test that made it
auditable — but no such test existed anywhere in the repo, so the boundary was enforced by prose
only. Reviewers were being pointed at a control that was not running. This is that test.

An ``authenticated=True`` IdentityEnvelope is a principal the whole authorization stack trusts. It
may be produced ONLY by code that either (a) proved a credential, or (b) is a sanctioned internal
trust ROOT reconstructing an already-established principal. Any other module gaining the ability to
mint one is an authorization bypass, so the allowlist below is deliberately explicit: adding a file
to it should be a conscious review decision, not something a refactor does quietly.

Detection is AST-based, so a mention in a comment or docstring does NOT arm the guard — only a real
reference to the capability/factory, or a literal ``authenticated=True`` keyword argument.
"""
from __future__ import annotations

import ast
from pathlib import Path

import featuregen

_SRC = Path(featuregen.__file__).resolve().parent

# Each entry is a module allowed to reach the trust root, with WHY it qualifies.
_TRUST_ROOT_ALLOWLIST = {
    # Defines the capability and the sanctioned factory — the boundary itself.
    "identity/_trust.py",
    # Consumes the capability to decide `authenticated`; the single chokepoint every builder calls.
    "identity/build.py",
    # (a) proves a bearer token's signature/issuer/audience/expiry before minting.
    "identity/verify.py",
    # (a) proves a username/password against the local IAM store before minting.
    "identity/local_session.py",
    # (a) re-resolves a principal from local IAM at worker time. The subject is NOT caller-supplied:
    # it comes from an integrity-verified, sealed work-item record, and roles are re-read from the
    # live tables so a revoked user fails closed.
    "identity/current_principal.py",
    # (b) internal trust root: reconstructs a historically authenticated actor from a write-once event.
    "events/serde.py",
    # (b) internal trust root: the durable timer runtime actor behind auto-expiry, which has no token.
    "aggregates/activation.py",
}


def _trust_root_references(path: Path) -> set[str]:
    """Real code references to the trust root in one module (comments/docstrings excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in {
            "_TRUST_CAPABILITY", "mint_trusted_identity",
        }:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in {
            "_TRUST_CAPABILITY", "mint_trusted_identity",
        }:
            found.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg == "authenticated":
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                found.add("authenticated=True")
    return found


def _modules_reaching_the_trust_root() -> dict[str, set[str]]:
    reaching: dict[str, set[str]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        refs = _trust_root_references(path)
        if refs:
            reaching[path.relative_to(_SRC).as_posix()] = refs
    return reaching


def test_only_allowlisted_modules_can_mint_an_authenticated_principal():
    reaching = _modules_reaching_the_trust_root()

    # Non-vacuous in both directions: the scan must actually find the boundary it claims to guard,
    # otherwise a broken detector would report a clean tree forever.
    assert "identity/_trust.py" in reaching
    assert "identity/verify.py" in reaching
    assert "_TRUST_CAPABILITY" in reaching["identity/verify.py"]

    stray = sorted(set(reaching) - _TRUST_ROOT_ALLOWLIST)
    assert not stray, (
        "TRUST BOUNDARY VIOLATION: module(s) outside the allowlist can mint an authenticated "
        f"principal: {stray}. An authenticated=True envelope must come from code that PROVED a "
        "credential or from a sanctioned internal trust root. If this is genuinely a new root, add "
        "it to _TRUST_ROOT_ALLOWLIST with the reason — deliberately, in review."
    )


def test_allowlist_has_no_dead_entries():
    """A stale allowlist entry silently widens the boundary for a file that may come back later."""
    reaching = _modules_reaching_the_trust_root()
    dead = sorted(_TRUST_ROOT_ALLOWLIST - set(reaching))
    assert not dead, (
        f"allowlist names module(s) that no longer touch the trust root: {dead} — remove them so "
        "the list keeps meaning 'exactly the code that can mint'."
    )
