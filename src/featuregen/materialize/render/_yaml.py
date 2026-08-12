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
    ``cust omers`` — a value that changed with nothing anywhere saying so. The C1 block is covered
    too, and for both of its failure shapes: NEL (0x85) is a YAML 1.1 line break, so a raw one
    FOLDS exactly like ``\\n``, while the rest of C1 makes the reader refuse the whole catalog.
    Everything below 0x20, plus DEL through 0x9f, becomes ``\\xNN``, which double-quoted YAML
    reads back as the same character. (U+2028/U+2029 round-trip unchanged — verified — so the
    range stops at 0x9f.)
    """
    out: list[str] = []
    for ch in str(value):
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'
