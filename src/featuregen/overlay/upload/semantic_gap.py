"""`OntologyGapSuggestionV1` — "the vocabulary has no word for this" (semantic plan Task 5).

The adjudicator's ONLY channel for saying the registry itself is short a concept. It is a
SUGGESTION and nothing else:

* it NEVER edits :data:`concepts.CONCEPT_REGISTRY` — the registry is code, reviewed by a human, and
  an automatically growing ontology is how a vocabulary stops meaning anything;
* it NEVER becomes field evidence — a gap is the absence of a classification, so writing a
  `concept` proposal from one would assert exactly the thing the model just said it could not say;
* it NEVER becomes a join key, an operand or an executable fact (functional rule 13).

What it DOES do: ride the immutable adjudication `structured_result` and get a subject/current
pointer (migration 1046) so a reviewer can find "every column whose adjudication suggested a
missing concept" without scanning JSON payloads, and so a later re-derivation can supersede or
withdraw the suggestion instead of accumulating duplicates.

Validation here is deliberately about SHAPE and REGISTRY MEMBERSHIP, never about whether the idea
is good — that judgement is the reviewing human's, and pre-filtering it in code would silently
discard the signal the surface exists to collect.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY, UNCLASSIFIED

#: Bounds on one suggestion. A label is a concept NAME (a token), a definition is one or two
#: sentences of rationale, and the alias list is a handful of spellings — anything larger is prose
#: the reviewer would not read and egress the audit would not want.
MAX_LABEL_LEN = 64
MAX_DEFINITION_LEN = 400
MAX_ALIASES = 8
MAX_ALIAS_LEN = 64


class OntologyGapError(ValueError):
    """A suggested gap cannot be represented (blank label, off-registry parent, …)."""


@dataclass(frozen=True, slots=True)
class OntologyGapSuggestionV1:
    """One proposed addition to the concept vocabulary.

    `proposed_label` is normalized to the registry's own naming shape (lower snake case) so a
    reviewer can compare it against real members; it is guaranteed NOT to already be a member (a
    label that exists is not a gap — see :func:`gap_from_output`). `parent_concept` is either a
    REGISTRY member or None: an unregistered parent would make the suggestion unattachable."""

    proposed_label: str
    parent_concept: str | None
    definition: str
    aliases: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.proposed_label:
            raise OntologyGapError("an ontology gap must name a proposed label")
        if self.proposed_label in CONCEPT_REGISTRY or self.proposed_label == UNCLASSIFIED:
            raise OntologyGapError(
                f"{self.proposed_label!r} is already in the vocabulary — that is not a gap")
        if self.parent_concept is not None and self.parent_concept not in CONCEPT_REGISTRY:
            raise OntologyGapError(
                f"parent concept {self.parent_concept!r} is not a registry member")
        if len(self.aliases) > MAX_ALIASES:
            raise OntologyGapError("too many aliases on one gap suggestion")

    def as_dict(self) -> dict:
        return {
            "proposed_label": self.proposed_label,
            "parent_concept": self.parent_concept,
            "definition": self.definition,
            "aliases": list(self.aliases),
        }

    def content_hash(self) -> str:
        """The gap's content identity — RFC 8785 JCS via `materialize_hash` (D1), never an inline
        scheme. Two runs proposing the same label/parent/definition/aliases produce the same hash,
        which is what lets a re-derivation be recognised as the SAME suggestion."""
        return materialize_hash(self.as_dict())


def _token(raw: object, limit: int) -> str:
    """A bounded lower snake-case token. Whitespace and hyphens collapse to underscores so a model
    answering "Settlement Instruction" lands on `settlement_instruction` — the registry's own
    spelling — rather than being rejected for cosmetics."""
    text = str(raw or "").strip().lower()[:limit]
    out = []
    for ch in text:
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/." and out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_")


def gap_from_output(raw: Mapping | None) -> OntologyGapSuggestionV1 | None:
    """Validate a model's optional `ontology_gap` object; ``None`` when it is absent OR unusable.

    DROPS the gap rather than failing the whole adjudication: a malformed suggestion is a lost
    suggestion, while rejecting the enclosing result over it would throw away a perfectly good
    concept selection. Every drop reason is structural — no label, a label the vocabulary already
    has (not a gap), or a parent that is not a registry member (unattachable)."""
    if not isinstance(raw, Mapping):
        return None
    label = _token(raw.get("proposed_label"), MAX_LABEL_LEN)
    if not label or label in CONCEPT_REGISTRY or label == UNCLASSIFIED:
        return None
    parent = _token(raw.get("parent_concept"), MAX_LABEL_LEN) or None
    if parent is not None and parent not in CONCEPT_REGISTRY:
        return None
    aliases: list[str] = []
    for alias in _alias_list(raw.get("aliases")):
        token = _token(alias, MAX_ALIAS_LEN)
        if token and token != label and token not in aliases:
            aliases.append(token)
    try:
        return OntologyGapSuggestionV1(
            proposed_label=label,
            parent_concept=parent,
            definition=str(raw.get("definition") or "").strip()[:MAX_DEFINITION_LEN],
            aliases=tuple(aliases[:MAX_ALIASES]),
        )
    except OntologyGapError:
        return None


def _alias_list(raw: object) -> Sequence[object]:
    if isinstance(raw, str):
        return [part for part in raw.split(",")]
    if isinstance(raw, Sequence):
        return list(raw)
    return ()
