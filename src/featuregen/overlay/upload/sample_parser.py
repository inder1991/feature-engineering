"""Deterministic sample-value parser — the representation / semantic / computational split (Task 5).

An FTR glossary description embeds a profiling hint in prose, e.g.::

    "... The sample profile is NUMERIC, with representative values such as
     3708484836801; 3708446902413; 3708454004701, which supports interpretation ..."

This module extracts that hint deterministically (regex only — no DB, no LLM) into a
:class:`ParsedProfile`, which later becomes PARSER evidence (``logical_representation@parser:supported``,
``semantic_type@parser:supported``) in Task 10.

**The non-negotiable contract (review-fix #9): a parser-supported type must NOT certify numeric
computation for an identifier-like value.** A fixed-length all-digit account number is an *identifier*,
not a decimal measure — it comes back with ``computational_type=None`` so nothing downstream sums or
averages it. Only a value carrying an actual decimal point (``1250.00``) earns
``computational_type="decimal"``.

Classification is driven by the extracted VALUES, not by the FTR profile *token*: the token is
ambiguous (FTR reuses ``NUMERIC_SPECIAL`` for both a time ``15:07:08`` and a dash-ref
``25-345129408-1-151``; ``NUMERIC`` for both fixed-length ids and epoch seconds), so the value shape
is the deciding signal. The token is used only as a coarse fallback when a description names a profile
but lists no values — and even then only ``logical_representation`` is inferred (never the semantics),
with a ``diagnostic`` recording the gap. When neither a phrase nor values are present, every type is
``None`` and a ``diagnostic`` says so — the parser NEVER returns a silent or guessed type.

Pure module: depends only on the standard library.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The "sample profile is <TOKEN>" hint (FTR: NUMERIC, NUMERIC_SPECIAL, ALPHA_SPECIAL, ALPHA_NUMERIC).
_TOKEN_RE = re.compile(r"sample\s+profile\s+is\s+([A-Za-z][A-Za-z_]*)", re.IGNORECASE)

# The "[representative ]values such as A; B; C" list. Non-greedy up to the FTR trailing ", which ..."
# clause or end-of-string. Values are ';'-separated (never ','), so a comma reliably ends the list.
_VALUES_RE = re.compile(
    r"(?:representative\s+)?values\s+such\s+as\s+(.+?)(?:,\s+which\b|$)",
    re.IGNORECASE | re.DOTALL,
)

# Value-shape probes (applied to a single extracted, cleaned value).
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")                       # 15:07:08 / 10:01
_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)\.\d+$")        # 1250.00 / 1,250.00 / 9.99
_INT_RE = re.compile(r"^\d+$")                                             # 3708484836801 / 84848368


@dataclass(frozen=True, slots=True)
class ParsedProfile:
    """The deterministic reading of a description's sample-profile hint.

    ``logical_representation`` — the physical shape of the value (``numeric_string``, ``decimal``,
    ``time``, ``text``), or ``None`` when unknown.
    ``semantic_type`` — what the value MEANS (``identifier``, ``amount``, ``time``, ``text``), or
    ``None`` when the parser cannot responsibly assert it.
    ``computational_type`` — set to ``"decimal"`` ONLY for values with a real decimal point; ``None``
    for everything identifier-like, so no aggregation is ever certified for an account number.
    ``sample_values`` — the extracted representative values, verbatim (order preserved).
    ``diagnostic`` — a human-readable reason set whenever the parser withheld a type (no phrase/values,
    a token but no values, or ambiguous integers); ``None`` on a clean classification.
    """

    logical_representation: str | None
    semantic_type: str | None
    computational_type: str | None
    sample_values: tuple[str, ...]
    diagnostic: str | None


# The remainder of the FTR ", which ..." interpretation clause. ``_VALUES_RE``'s match ENDS just after
# the word "which", so `strip_sample_values` consumes what follows (prose, up to the sentence period)
# too — else a dangling "supports interpretation ..." fragment is left behind. Prose only (no data
# values after "which"), so bounding by the next '.' is safe here.
_INTERP_TAIL_RE = re.compile(r"[^.]*\.?")

# A leading article ("The "/"the ") that introduces the excised sample-profile sentence — consumed so
# the excision leaves "<meaning>. <rest>" rather than "<meaning>. The . <rest>".
_LEAD_ARTICLE_RE = re.compile(r"\bthe\s+$", re.IGNORECASE)


def strip_sample_values(description: str) -> str:
    """Excise the embedded "[the ]sample profile is X ... representative values such as A; B; C[, which
    ...]" clause from a glossary ``description``, leaving the surrounding business meaning intact.

    **Data-leak backstop (whole-branch review CRITICAL).** An FTR glossary business definition EMBEDS
    raw customer sample VALUES in prose (account numbers, times like ``15:07:08``, decimals ``1250.00``,
    short codes). If that definition egresses verbatim as ``business_definition`` those values reach the
    external LLM — and the deterministic PII backstop (``intake.redaction``) only catches PAN-like runs,
    missing the rest. This removes EXACTLY the span :func:`parse_sample_profile` reads (same anchors), so
    the concept classifier still sees the business prose but never a raw value.

    Fail-safe: a description with no representative-values clause is returned unchanged (nothing to
    strip); ``None``/empty returns ``""``.
    """
    text = description or ""
    values_m = _VALUES_RE.search(text)
    if values_m is None:
        return text   # no representative-values clause -> only business prose, nothing to strip
    token_m = _TOKEN_RE.search(text)
    start = values_m.start()
    if token_m is not None and token_m.start() < start:
        start = token_m.start()   # extend back over the "sample profile is X, with ..." lead-in
    # Drop a leading article ("The ") introducing the excised sentence, so no "The ." stub remains.
    lead = _LEAD_ARTICLE_RE.search(text, 0, start)
    if lead is not None:
        start = lead.start()
    end = values_m.end()          # ``_VALUES_RE`` ends just after "which" (or at end-of-string)
    if values_m.group(0).rstrip().lower().endswith("which"):
        # It stopped at the ", which" boundary — consume the rest of that interpretation clause
        # (prose, up to and including the next sentence period).
        tail = _INTERP_TAIL_RE.match(text, end)
        if tail is not None:
            end = tail.end()
    excised = (text[:start].rstrip() + " " + text[end:].lstrip()).strip()
    return re.sub(r"\s+([.,;])", r"\1", excised)   # tidy any space left before punctuation


def parse_sample_profile(description: str) -> ParsedProfile:
    """Parse a glossary ``description`` into a :class:`ParsedProfile` (see the module docstring)."""
    text = description or ""
    token = _extract_token(text)
    values = _extract_values(text)
    logical, semantic, computational, diagnostic = _classify(values, token)
    return ParsedProfile(
        logical_representation=logical,
        semantic_type=semantic,
        computational_type=computational,
        sample_values=values,
        diagnostic=diagnostic,
    )


def _extract_token(text: str) -> str | None:
    """The FTR profile token (``NUMERIC``, ``ALPHA_SPECIAL``, ...) from a "sample profile is X" phrase."""
    m = _TOKEN_RE.search(text)
    return m.group(1) if m else None


def _extract_values(text: str) -> tuple[str, ...]:
    """The representative values, split on ``;`` and cleaned; ``()`` when the phrase is absent."""
    m = _VALUES_RE.search(text)
    if not m:
        return ()
    return tuple(v for v in (_clean_value(p) for p in m.group(1).split(";")) if v)


def _clean_value(value: str) -> str:
    """Strip surrounding whitespace and a trailing sentence period / ellipsis. A trailing run of dots
    is only ever a terminator here (a decimal keeps its digits after the point), so this never mangles
    ``1250.00`` while it does turn ``9.99.`` → ``9.99`` and ``413 ...`` → ``413``."""
    return value.strip().rstrip(" .…")


def _classify(
    values: tuple[str, ...], token: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return ``(logical_representation, semantic_type, computational_type, diagnostic)`` for a set of
    extracted values (value shape decides), falling back to the token when there are no values."""
    if not values:
        return _classify_from_token(token)

    # All values look like a clock time (HH:MM[:SS]) → a time-of-day field.
    if all(_TIME_RE.match(v) for v in values):
        return "time", "time", None, None

    is_decimal = [_DECIMAL_RE.match(v) is not None for v in values]
    is_integer = [_INT_RE.match(v) is not None for v in values]

    # Every value is numeric (an integer or a pointed decimal).
    if all(d or i for d, i in zip(is_decimal, is_integer, strict=True)):
        if any(is_decimal):
            # A real decimal point is present → a genuine measure; safe to certify computation.
            return "decimal", "amount", "decimal", None
        # Pure integers: identifier-like. NEVER computational (review-fix #9). A uniform length is the
        # positive identifier signal; varying length is withheld (could be a count or a code).
        if len({len(v) for v in values}) == 1:
            return "numeric_string", "identifier", None, None
        return (
            "numeric_string", None, None,
            "all-integer sample values of varying length; not certified as an identifier or for "
            "numeric aggregation (could be a count or a code)",
        )

    # Anything else — names, alphanumeric codes, dash/slash-separated references — is text.
    return "text", "text", None, None


def _classify_from_token(
    token: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Fallback when a description carries no representative values. With no values the parser cannot
    assert semantics or computation, so it infers only a coarse ``logical_representation`` from the
    token (if any) and always records a ``diagnostic``. No token at all → every type ``None``."""
    if token is None:
        return None, None, None, (
            "no sample-profile phrase or representative values found in the description"
        )
    upper = token.upper()
    if upper.startswith("NUMERIC"):
        logical: str | None = "numeric_string"
    elif upper.startswith("ALPHA"):
        logical = "text"
    else:
        logical = None
    return logical, None, None, (
        f"sample-profile token '{token}' present but no representative values to classify; "
        "logical_representation inferred from the token, semantics/computation withheld"
    )
