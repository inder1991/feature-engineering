"""Task 23 — ``yaml_scalar`` must never let a character CHANGE the value it quotes.

The one job of the function is that the string the catalog meant is the string a YAML reader gets
back. A raw ``\\n`` inside a double-quoted YAML scalar FOLDS to a space, so ``"cust\\nomers"`` reads
back as ``"cust omers"`` — a different table name, silently. Every assertion below is a round-trip
through ``yaml.safe_load`` (the same reader the test suite uses on rendered catalogs), because an
assertion on the escaped text alone would pin a spelling without proving a reader agrees about it.
"""
from __future__ import annotations

import yaml

from featuregen.materialize.render._yaml import yaml_scalar

#: One hostile value per control-character family: newline (folds), tab (illegal in YAML),
#: carriage return (folds), ESC (C0), DEL (0x7f), NUL (C0 floor).
HOSTILE = ("cust\nomers", "a\tb", "c\rd", "e\x1bf", "del\x7fete", "nul\x00led")


def test_control_characters_cannot_change_the_value() -> None:
    for hostile in HOSTILE:
        scalar = yaml_scalar(hostile)
        assert "\n" not in scalar and "\r" not in scalar and "\t" not in scalar, (
            f"{hostile!r} rendered with a RAW control character: {scalar!r}")
        assert yaml.safe_load(scalar) == hostile, (
            f"{hostile!r} round-tripped to a DIFFERENT value through {scalar!r}")


def test_backslash_and_quote_still_round_trip() -> None:
    """The two characters the old implementation escaped must not regress."""
    for value in ('a"b', "a\\b", 'tricky\\"combo', "\\n"):
        assert yaml.safe_load(yaml_scalar(value)) == value


def test_plain_values_are_double_quoted_and_unchanged() -> None:
    """Always double-quoted, so a value that looks like a number/date/boolean stays a string."""
    for value in ("banking", "2026-07-31", "true", "1.5", "customers"):
        assert yaml_scalar(value) == f'"{value}"'
        assert yaml.safe_load(yaml_scalar(value)) == value
