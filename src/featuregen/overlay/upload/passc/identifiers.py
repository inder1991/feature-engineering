"""Pass C — identifier eligibility + concept normalization (pure: no DB, no LLM).

Decides which columns may anchor a join candidate, and what identifier *concept* a column denotes,
from upload metadata alone. Two load-bearing rules baked in by review:

- Negative filters match on WORD BOUNDARIES, never substrings — "Mandate Reference" contains "date"
  and "Corporate Account Number" contains "rate", yet both ARE identifiers; a substring match would
  wrongly suppress them.
- The `number` id-token in a term name only counts with an entity context ("account number",
  "reference number") — a bare "Sequence Number"/"Serial Number" is not a join key by name alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from featuregen.overlay.upload.entity import _is_id_like
from featuregen.overlay.upload.passc.types import DEFAULT_CONFIG, PassCConfig


@dataclass(frozen=True, slots=True)
class ColMeta:
    """One column's upload metadata, as Pass C sees it. Entity is SPLIT: `table_entity` is the
    table's primary business entity; `column_entity` is THIS column's identifier namespace (a
    customer_id column in a transaction table → table_entity="transaction", column_entity="customer")."""
    object_ref: str
    table: str
    column: str
    data_type: str
    term_name: str
    term_type: str
    concept: str
    synonyms: str          # raw alias list, pipe/comma/semicolon-separated (as uploaded)
    bian_leaf: str
    fibo_leaf: str
    table_entity: str
    column_entity: str
    data_domain: str
    is_grain: bool


_WORD_RE = re.compile(r"[^a-z0-9]+")


def _words(text: str) -> list[str]:
    return [w for w in _WORD_RE.split((text or "").lower()) if w]


# Term-name id tokens: whole words only. "number"-ish tokens are deliberately ABSENT here — they
# only count after an entity-context word (below), so "Sequence Number" isn't admitted while
# "Customer Account Number" (FORACID — no id-like column suffix) is.
_ID_TOKENS = frozenset({"identifier", "id", "ref", "reference", "key"})
_NUMBER_TOKENS = frozenset({"number", "num", "no", "nbr"})
_NUMBER_CONTEXTS = frozenset({"account", "reference"})


def _term_has_id_token(term_name: str) -> bool:
    toks = _words(term_name)
    if set(toks) & _ID_TOKENS:
        return True
    return any(prev in _NUMBER_CONTEXTS and tok in _NUMBER_TOKENS
               for prev, tok in zip(toks, toks[1:]))


def _hits_negative(col: ColMeta, cfg: PassCConfig) -> bool:
    """WORD-BOUNDARY negative match over column + term_name + concept — never substring (a substring
    match suppresses real ids: "Mandate Reference" ⊃ "date", "Corporate Account Number" ⊃ "rate").
    The concept slug is also checked verbatim: "free_text" tokenizes to {"free","text"} and would
    otherwise slip past a token-set intersection."""
    tokens = set(_words(col.column)) | set(_words(col.term_name)) | set(_words(col.concept))
    if (col.concept or "").strip().lower() in cfg.negative_concepts:
        return True
    return bool(tokens & cfg.negative_concepts)


def is_join_key_eligible(col: ColMeta, cfg: PassCConfig = DEFAULT_CONFIG) -> bool:
    """May this column anchor a join candidate? Measures never; word-boundary negatives never;
    otherwise the column must LOOK like an id (name-suffix heuristic, shared with entity
    suggestions) or its term name must carry an id token."""
    if (col.term_type or "").strip().lower() == "measure":
        return False
    if _hits_negative(col, cfg):
        return False
    return _is_id_like(col.column, col.data_type) or _term_has_id_token(col.term_name)


# Generic id words stripped from the TAIL of a term name — what remains is the business concept
# ("Customer Information File Identifier" → "customer information file"; "Customer Account Number"
# → "customer account" — so FORACID stays a DIFFERENT concept from CIF_ID).
_GENERIC_ID_TAIL = frozenset({"identifier", "id", "ref", "reference", "key", "code",
                              "number", "num", "no", "nbr"})
_SYNONYM_PLACEHOLDERS = frozenset({"(blank)", "n/a", "na", "none", "-"})


def _canon(text: str) -> str:
    toks = _words(text)
    while toks and toks[-1] in _GENERIC_ID_TAIL:
        toks.pop()
    return " ".join(toks)


def _synonym_canons(synonyms: str) -> list[str]:
    out = set()
    for part in re.split(r"[|;,]", synonyms or ""):
        part = part.strip()
        if not part or part.lower() in _SYNONYM_PLACEHOLDERS:
            continue
        c = _canon(part)
        if c:
            out.add(c)
    return sorted(out)


def _initialism(canon_text: str) -> str:
    return "".join(w[0] for w in canon_text.split())


def normalized_identifier_concept(col: ColMeta) -> str | None:
    """Canonical identifier concept for a column: term_name canonicalized (lowercased, tokenized,
    generic id tail stripped) + synonyms FOLDED IN — an acronym term expands to its multi-word
    synonym (term "CIF" + synonym "Customer Information File" → "customer information file"), and an
    acronym synonym of an expanded term is redundant — so CIF_ID and CIF collapse to one concept.
    None when no concept survives (blank/all-generic term and no usable synonym)."""
    term = _canon(col.term_name)
    syns = _synonym_canons(col.synonyms)
    if not term:
        return syns[0] if syns else None
    if " " not in term:                      # term is a bare acronym — expand via a synonym
        for s in syns:
            if " " in s and _initialism(s) == term:
                return s
    return term
