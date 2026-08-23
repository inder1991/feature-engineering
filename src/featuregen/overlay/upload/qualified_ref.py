"""Task S1A-5a — the GOVERNED READ-SET REF: a column, and the catalog it lives in.

A single-catalog plan can store bare ``schema.table.column`` refs because the plan itself carries
one ``catalog_source`` that every entry inherits. A governed CROSS-CATALOG plan cannot: its read
set spans catalogs, so each entry has to say which one it belongs to, and the qualified form
``source::schema.table.column`` is the string this repository already uses for exactly that
(``object_ref.normalize_ref`` — the identity every per-object store is keyed by).

**This module delegates; it is not a second grammar.** ``object_ref`` owns what a ref IS — the
``::`` scheme separator, the dotted path, the strip+lower-case fold, the arity. A second parser
here would be a second answer to the same question, and the two would diverge the day one of them
is corrected. So :func:`parse_qualified_ref` calls :func:`~featuregen.overlay.upload.object_ref.
parse_ref` for the shape and :func:`~featuregen.overlay.upload.object_ref.normalize_ref` for the
canonical form, and adds only the three things a GOVERNED read set needs on top of them:

* **a column is required.** ``parse_ref`` accepts a two-part path as a TABLE ref; a table-shaped
  entry in a read set is an unbounded read wearing a bounded one's name.
* **no blank components.** ``normalize_ref`` reproduces a blank source and a blank table verbatim,
  so ``'::public.txns.amount'`` — a ref naming NO catalog — round-trips exactly. A round-trip
  check alone cannot see that; the emptiness check can, and it names which component is missing.
* **canonical form, exactly.** ``'BANK::public.txns.amount'`` denotes the same object under the
  fold, so a lenient parser would accept it and store a spelling that no longer matches the bytes
  the plan was sealed over. The refusal NAMES the canonical form so the defect is actionable.

Every refusal is a ``ValueError`` naming the defect. Callers translate it into their own typed
error — ``semantic_option_decision`` raises ``OptionDecisionIntegrityError``, because a read set
nobody can authenticate is not a read set.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref


@dataclass(frozen=True, slots=True)
class QualifiedRefV1:
    """One governed read-set entry: a column, and the catalog it is read from.

    Frozen and hashable on purpose — it is a set member and a dict key in the folds that decide
    execution authority, and a read set that can be edited after it was authenticated is not one.
    """

    catalog_source: str
    schema_name: str
    table: str
    column: str

    @property
    def canonical(self) -> str:
        """The ``logical_ref`` this ref IS, built by the one normalization law."""
        return normalize_ref(self.catalog_source, self.schema_name, self.table, self.column)


def parse_qualified_ref(raw: str) -> QualifiedRefV1:
    """The governed read-set parser: ``object_ref``'s law, column REQUIRED, round-trip exact.

    Raises:
        ValueError: naming the defect — bad arity (from ``parse_ref`` itself), a table ref where a
            column was required, a blank component, or a non-canonical spelling (whose message
            carries the canonical form). Callers translate to their own typed error.
    """
    source, schema, table, column = parse_ref(raw)      # raises ValueError on bad shape
    if column is None:
        raise ValueError(f"governed read-set ref must name a column: {raw!r}")
    blank = [name for name, value in (("catalog_source", source), ("schema", schema),
                                      ("table", table), ("column", column))
             if not value.strip()]
    if blank:
        raise ValueError(
            f"governed read-set ref has a blank {', '.join(blank)}: {raw!r}")
    ref = QualifiedRefV1(source, schema, table, column)
    if ref.canonical != raw:
        raise ValueError(f"not in canonical form (expected {ref.canonical!r}): {raw!r}")
    return ref


__all__ = ["QualifiedRefV1", "parse_qualified_ref"]
