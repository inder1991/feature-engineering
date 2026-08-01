"""The ONE decision about how a value becomes a YAML scalar in a rendered catalog.

Extracted from ``render.project`` when ``render.publish`` began emitting a catalog entry of its own
(§10.3). The alternative — a second copy of the same four-line function — is a second chance to
quote a storage location differently, and a catalog whose entries disagree about escaping is one
whose datasets resolve to different paths for reasons nobody wrote down.

Import-free by construction, so ``project`` and ``publish`` can both depend on it without either
depending on the other.
"""
from __future__ import annotations

__all__ = ["yaml_scalar"]


#: The named escapes first, so a backslash is doubled before anything else could re-read it.
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def yaml_scalar(value: str) -> str:
    """One YAML scalar. Always double-quoted, so a value that looks like a number, a date or a
    boolean stays the string the catalog meant.

    Control characters are ESCAPED, never emitted raw: a raw newline inside a double-quoted YAML
    scalar folds to a space, so ``cust\\nomers`` would silently become the different table name
    ``cust omers`` — a value that changed with nothing anywhere saying so. Anything else below
    0x20 (and DEL) becomes ``\\xNN``, which double-quoted YAML reads back as the same character.
    """
    out: list[str] = []
    for ch in str(value):
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'
