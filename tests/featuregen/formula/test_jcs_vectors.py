"""RFC 8785 (JSON Canonicalization Scheme) test vectors for the vendored `_jcs`.

Every vector below is transcribed from RFC 8785 itself, so the vendored
implementation is PROVEN against the spec rather than assumed:

- Section 3.2.2 sample object -> the exact canonical UTF-8 bytes of
  Section 3.2.4 (numbers, string escaping, literals, key sorting, no
  whitespace, UTF-8 output);
- Section 3.2.3 property-sorting object -> keys ordered by UTF-16 code
  units (the emoji surrogate pair sorts BEFORE U+FB33);
- Appendix B ECMAScript-compatible number serialization samples
  (IEEE 754 bit patterns -> minimal ES6 number form), including the
  NaN / Infinity rejections.
"""
from __future__ import annotations

import struct

import pytest

from featuregen.formula._jcs import CanonicalizationError, dumps


def _double(bits_hex: str) -> float:
    """The IEEE 754 double for a 64-bit big-endian hex pattern (Appendix B column 1)."""
    return struct.unpack(">d", bytes.fromhex(bits_hex))[0]


# ---- RFC 8785 section 3.2.2 input -> section 3.2.4 canonical UTF-8 bytes ----

# Parsed form of the RFC's JSON sample object. Its "string" member decodes to
# the 12 code points: U+20AC, "$", U+000F, U+000A, "A", "'", "B", QUOTE,
# BACKSLASH, BACKSLASH, QUOTE, and SOLIDUS.
RFC_SAMPLE_INPUT = {
    "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
    "string": "€$\nA'B\"\\\\\"/",
    "literals": [None, True, False],
}

# The exact hex dump from RFC 8785 section 3.2.4 ("UTF-8 Generation").
RFC_SAMPLE_CANONICAL = bytes.fromhex(
    "7b 22 6c 69 74 65 72 61 6c 73 22 3a 5b 6e 75 6c 6c 2c 74 72"
    "75 65 2c 66 61 6c 73 65 5d 2c 22 6e 75 6d 62 65 72 73 22 3a"
    "5b 33 33 33 33 33 33 33 33 33 2e 33 33 33 33 33 33 33 2c 31"
    "65 2b 33 30 2c 34 2e 35 2c 30 2e 30 30 32 2c 31 65 2d 32 37"
    "5d 2c 22 73 74 72 69 6e 67 22 3a 22 e2 82 ac 24 5c 75 30 30"
    "30 66 5c 6e 41 27 42 5c 22 5c 5c 5c 5c 5c 22 2f 22 7d".replace(" ", "")
)


def test_rfc_sample_object_canonicalizes_to_the_rfc_bytes():
    assert dumps(RFC_SAMPLE_INPUT) == RFC_SAMPLE_CANONICAL


# ---- RFC 8785 section 3.2.3: sorting on UTF-16 code units ----

SORTING_INPUT = {
    "€": "Euro Sign",
    "\r": "Carriage Return",
    "דּ": "Hebrew Letter Dalet With Dagesh",
    "1": "One",
    "\U0001f600": "Emoji: Grinning Face",  # "😀" in the RFC's JSON source
    "": "Control",
    "ö": "Latin Small Letter O With Diaeresis",
}

# Expected key order per the RFC: \r, "1", U+0080, U+00F6, U+20AC, U+1F600
# (surrogate pair D83D DE00 -- BEFORE U+FB33 under UTF-16 ordering), U+FB33.
# Only \r is escaped on output; U+0080 and above are emitted as raw UTF-8.
SORTING_CANONICAL = (
    '{"\\r":"Carriage Return",'
    '"1":"One",'
    '"":"Control",'
    '"ö":"Latin Small Letter O With Diaeresis",'
    '"€":"Euro Sign",'
    '"\U0001f600":"Emoji: Grinning Face",'
    '"דּ":"Hebrew Letter Dalet With Dagesh"}'
).encode("utf-8")


def test_rfc_sorting_vector_orders_keys_by_utf16_code_units():
    assert dumps(SORTING_INPUT) == SORTING_CANONICAL


# ---- RFC 8785 Appendix B: number serialization samples ----

APPENDIX_B_VECTORS = [
    ("0000000000000000", "0"),  # Zero
    ("8000000000000000", "0"),  # Minus zero
    ("0000000000000001", "5e-324"),  # Min pos number
    ("8000000000000001", "-5e-324"),  # Min neg number
    ("7fefffffffffffff", "1.7976931348623157e+308"),  # Max pos number
    ("ffefffffffffffff", "-1.7976931348623157e+308"),  # Max neg number
    ("4340000000000000", "9007199254740992"),  # Max pos int
    ("c340000000000000", "-9007199254740992"),  # Max neg int
    ("4430000000000000", "295147905179352830000"),  # ~2**68
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),  # Round to even
]


@pytest.mark.parametrize(("bits_hex", "expected"), APPENDIX_B_VECTORS)
def test_appendix_b_number_vectors(bits_hex: str, expected: str):
    assert dumps(_double(bits_hex)) == expected.encode("utf-8")


@pytest.mark.parametrize("bits_hex", ["7fffffffffffffff", "7ff0000000000000", "fff0000000000000"])
def test_nan_and_infinity_are_rejected(bits_hex: str):
    with pytest.raises(CanonicalizationError):
        dumps(_double(bits_hex))


# ---- JCS constraints beyond Appendix B ----

def test_int_within_ieee_exact_domain_serializes_minimally():
    assert dumps(9007199254740991) == b"9007199254740991"  # 2**53 - 1
    assert dumps(-9007199254740991) == b"-9007199254740991"


def test_int_beyond_ieee_exact_domain_is_rejected():
    with pytest.raises(CanonicalizationError):
        dumps(2**53)


def test_lone_surrogate_is_rejected():
    with pytest.raises(CanonicalizationError):
        dumps("\ud800")


def test_empty_containers_and_nesting_have_no_whitespace():
    assert dumps({}) == b"{}"
    assert dumps([]) == b"[]"
    assert dumps({"b": [1, {"a": None}], "a": ""}) == b'{"a":"","b":[1,{"a":null}]}'
