"""P0 shadow-measurement harness — Task 2: the DETERMINISTIC grounding signal (design §Components/2).

``ground_concept`` cross-verifies an LLM-proposed ``concept`` against hard, already-recorded evidence
for its column — NO LLM call, NO write to any table. It is read-only over ``field_evidence`` (via
:func:`read_active_field_evidence`) and ``graph_node`` (sibling columns of the same table), reasoning
purely over the concept vocabulary (``concepts.py``) plus what earlier Phase-1 producers already wrote.

Three independent checks, each ``pass`` | ``fail`` | ``absent`` (never invented when the deciding
evidence — or concept-side metadata — is simply missing):

* ``type_consistency`` — the concept's implied type-family (derived from its ``group``, the ONLY
  type-shaped field :class:`~featuregen.overlay.upload.concepts.Concept` carries) vs the parser's
  ``semantic_type`` / ``logical_representation`` evidence (:mod:`sample_parser`'s value vocabulary).
* ``path_agreement`` — the proposed concept vs the file-attested ``bian_path`` / ``fibo_path`` /
  ``business_term`` evidence. **Honesty note (task brief):** ``Concept`` carries NO per-concept
  canonical path — ``concepts.py`` has no ``bian_path``/``fibo_path`` field at all — so this is NOT a
  registry lookup. It is a keyword heuristic: does the attested text contain a token of the concept's
  OWN name (e.g. ``monetary_flow`` -> {"monetary", "flow"})? A real per-concept path mapping would be
  a strictly stronger signal; this is the best that exists today without inventing new vocabulary data.
* ``sibling_consistency`` — a MONETARY concept expects a CURRENCY sibling column in the same table (and
  vice versa) — the same structural-name-token convention
  :mod:`featuregen.overlay.upload.semantic_bindings.shortlist` already uses for the identical
  monetary/currency relationship (a currency/measure taxonomy concept OR a structural column-name
  token), re-derived here (not imported) to keep this module import-light and provider-free.

``coverage = present_checks / 3`` (present = non-``absent``); ``conflict = any check == 'fail'``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.contracts import DbConn
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.concepts import concept as lookup_concept
from featuregen.overlay.upload.object_ref import parse_ref

PASS = "pass"
FAIL = "fail"
ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class GroundingV1:
    """The deterministic grounding result for one ``(logical_ref, proposed_concept)`` (design §2)."""

    checks: Mapping[str, str]
    coverage: float
    conflict: bool


# ── type_consistency: concept group -> implied type-family, vs the parser's value vocabulary ───────
# Only groups whose implied family is UNAMBIGUOUS from the taxonomy's own §3 documentation (see
# concepts.py's per-group comment) are mapped — e.g. "currency" mixes text CODES (currency_code) with
# numeric RATES (fx_conversion_rate) and "identifier" mixes numeric_string/text, so both are left
# unmapped (the check reports 'absent' for them) rather than guessing.
_GROUP_TYPE_FAMILY: dict[str, str] = {
    "monetary": "numeric",
    "quantity_risk": "numeric",
    "temporal": "temporal",
    "text": "text",
    "label": "text",
    "categorical": "text",
    "geographic": "text",
}

# sample_parser.py's own value vocabulary (ParsedProfile.semantic_type / .logical_representation),
# mapped to the SAME coarse family space as _GROUP_TYPE_FAMILY. "identifier"/"numeric_string" are a
# family of their own — a pure-digit identifier is NOT a numeric measure (sample_parser's own
# non-negotiable contract, review-fix #9) — so they never match "numeric".
_PARSER_VALUE_FAMILY: dict[tuple[str, str], str] = {
    ("semantic_type", "amount"): "numeric",
    ("semantic_type", "identifier"): "identifier",
    ("semantic_type", "time"): "temporal",
    ("semantic_type", "text"): "text",
    ("logical_representation", "decimal"): "numeric",
    ("logical_representation", "numeric_string"): "identifier",
    ("logical_representation", "time"): "temporal",
    ("logical_representation", "text"): "text",
}

# The parser (or a source/attested declared-type signal, per field_policies.py) writes BOTH facets;
# semantic_type is preferred (it captures MEANING, closer to a concept family) with
# logical_representation (physical shape) as the fallback.
_PARSER_FIELD_ORDER: tuple[str, ...] = ("semantic_type", "logical_representation")


def _parser_type_family(conn: DbConn, logical_ref: str) -> str | None:
    """The coarse type-family implied by the column's active parser-type evidence, or ``None`` when
    no active evidence exists (or its value isn't in the known vocabulary)."""
    for field_name in _PARSER_FIELD_ORDER:
        for ev in read_active_field_evidence(conn, logical_ref, field_name):
            value = str(ev.proposed_value).strip().lower() if ev.proposed_value else ""
            if not value:
                continue
            family = _PARSER_VALUE_FAMILY.get((field_name, value))
            if family is not None:
                return family
    return None


# ── path_agreement: attested bian_path/fibo_path/business_term vs the concept's OWN name tokens ────
_PATH_FIELDS: tuple[str, ...] = ("bian_path", "fibo_path", "business_term")


def _attested_path_texts(conn: DbConn, logical_ref: str) -> list[str]:
    """Every non-blank ACTIVE attested value across bian_path/fibo_path/business_term for this column."""
    texts: list[str] = []
    for field_name in _PATH_FIELDS:
        for ev in read_active_field_evidence(conn, logical_ref, field_name):
            value = ev.proposed_value
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


def _concept_name_tokens(name: str) -> set[str]:
    """The proposed concept's OWN name, split on ``_`` — the only "path-shaped" signal a concept
    carries (concepts.py has no per-concept bian/fibo path field to look up)."""
    return {t for t in (name or "").strip().lower().split("_") if len(t) >= 3}


# ── sibling_consistency: monetary <-> currency, mirroring shortlist.py's structural-name convention ─
_CURRENCY_NAME_TOKENS = frozenset({"currency", "ccy", "curr"})
_MEASURE_NAME_TOKENS = frozenset({
    "amount", "amt", "balance", "bal", "notional", "price", "value", "val", "fee",
    "principal", "cost", "revenue", "charge", "premium", "exposure", "pnl", "proceeds",
})

# Concept groups the sibling rule applies to, and what it expects a sibling to be.
_SIBLING_EXPECTATION: dict[str, str] = {"monetary": "currency", "currency": "monetary"}


def _name_tokens(column_name: str) -> set[str]:
    return {t for t in (column_name or "").lower().replace("-", "_").split("_") if t}


def _sibling_rows(conn: DbConn, source: str, table: str, exclude_column: str,
                  ) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        "SELECT column_name, concept FROM graph_node "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
        "AND lower(column_name) <> lower(%s)",
        (source, table, exclude_column),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _sibling_group(column_name: str, sibling_concept: str | None) -> str | None:
    """The "monetary"/"currency" family a SIBLING column belongs to, or ``None`` when neither its own
    (already-classified) concept nor its structural name says so. Mirrors
    ``semantic_bindings.shortlist.is_currency_column``/``is_measure_column``'s signal order (a
    concept-group hit first, a structural name-token hit second) without importing that module's
    private constants, keeping this pure module dependency-light."""
    if sibling_concept:
        sib = lookup_concept(sibling_concept)
        if sib is not None and sib.group in _SIBLING_EXPECTATION:
            return sib.group
    tokens = _name_tokens(column_name)
    if tokens & _CURRENCY_NAME_TOKENS:
        return "currency"
    if tokens & _MEASURE_NAME_TOKENS:
        return "monetary"
    return None


def _sibling_consistency(conn: DbConn, logical_ref: str, group: str | None) -> str:
    expected = _SIBLING_EXPECTATION.get(group or "")
    if expected is None:
        return ABSENT   # the sibling rule only applies to monetary/currency concepts
    source, _schema, table, column = parse_ref(logical_ref)
    siblings = _sibling_rows(conn, source, table, column or "")
    if not siblings:
        return ABSENT   # no sibling columns exist at all — nothing to check either way
    found = any(_sibling_group(name, sib_concept) == expected for name, sib_concept in siblings)
    return PASS if found else FAIL


def ground_concept(conn: DbConn, logical_ref: str, proposed_concept: str) -> GroundingV1:
    """Cross-verify ``proposed_concept`` for ``logical_ref`` against recorded evidence — PURE, no
    LLM, no writes. See the module docstring for the three checks."""
    c = lookup_concept(proposed_concept)
    group = c.group if c is not None else None

    concept_family = _GROUP_TYPE_FAMILY.get(group) if group else None
    evidence_family = _parser_type_family(conn, logical_ref)
    if concept_family is None or evidence_family is None:
        type_check = ABSENT
    else:
        type_check = PASS if concept_family == evidence_family else FAIL

    attested = _attested_path_texts(conn, logical_ref)
    keywords = _concept_name_tokens(proposed_concept)
    if not attested or not keywords:
        path_check = ABSENT
    else:
        combined = " ".join(attested).lower()
        path_check = PASS if any(k in combined for k in keywords) else FAIL

    sibling_check = _sibling_consistency(conn, logical_ref, group)

    checks: dict[str, str] = {
        "type_consistency": type_check,
        "path_agreement": path_check,
        "sibling_consistency": sibling_check,
    }
    present = sum(1 for v in checks.values() if v != ABSENT)
    coverage = present / 3
    conflict = any(v == FAIL for v in checks.values())
    return GroundingV1(checks=checks, coverage=coverage, conflict=conflict)
