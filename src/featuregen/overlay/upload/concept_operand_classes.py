"""SE-3 — the governed concept→operand-class map: which roles a concept may ever serve.

The second adversarial review found this map simply did not exist: §6.4's "possible operand
classes" was load-bearing (the identifier-is-never-a-measure rule rides on it) with no owner.
It exists now, with deliberate provenance: the map is DERIVED from the reviewed V2 registry's
own authored usage — every ``(operand.concept, operand.operand_class)`` pair some recipe's SME
review stands behind — plus a small authored-extension table for classes a concept may serve
before any recipe uses it that way. A hand-typed parallel table would drift from the registry;
the registry cannot drift from itself.

Two laws are enforced at build time, not documented and hoped:

* an IDENTIFIER concept (registered namespace) never includes ``measure`` — if a future pack
  authors that pair, THIS module fails at import, which is exactly where the argument belongs;
* completeness is structural: every concept referenced by any V2 operand has an entry by
  construction, and the pin test proves it stays that way.

The compiler (:mod:`column_capabilities`) consumes this map and REFUSES a concept absent from
it (``possible_operand_classes = ()`` with a named missing-context marker) — it never guesses
a role from a physical type or a column name.
"""
from __future__ import annotations

from collections import defaultdict

from featuregen.overlay.upload.recipe_contract_v2 import OPERAND_CLASSES

#: Version pin (the taxonomy/versions.py discipline): bump when the derivation rule or the
#: authored extensions change meaning — consumers stamp it into capability provenance.
OPERAND_CLASS_MAP_VERSION = "operand-classes@1"

#: Authored widenings BEYOND current registry usage: {concept: (classes,)}. Empty on purpose at
#: v1 — the registry's own usage is the reviewed truth; a widening lands here WITH the review
#: that justifies it, never as a drive-by.
_AUTHORED_EXTENSIONS: dict[str, tuple[str, ...]] = {}


def _derive() -> dict[str, tuple[str, ...]]:
    from featuregen.overlay.upload.concepts import concept as registered_concept
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    usage: dict[str, set[str]] = defaultdict(set)
    for recipe in V2_RECIPES:
        for operand in recipe.operands:
            usage[operand.concept].add(operand.operand_class)
    for concept_name, classes in _AUTHORED_EXTENSIONS.items():
        usage[concept_name].update(classes)

    mapped: dict[str, tuple[str, ...]] = {}
    for concept_name, classes in usage.items():
        unknown = classes - set(OPERAND_CLASSES)
        if unknown:
            raise ValueError(f"unknown operand classes for {concept_name!r}: {sorted(unknown)}")
        try:
            registered = registered_concept(concept_name)
        except Exception:
            registered = None
        if registered is not None and registered.namespace is not None and "measure" in classes:
            raise ValueError(
                f"identifier concept {concept_name!r} (namespace "
                f"{registered.namespace!r}) may never serve a measure operand — a recipe or "
                "extension authored that pair; the argument belongs here, at import, not in "
                "a binder at runtime")
        mapped[concept_name] = tuple(sorted(classes))
    return mapped


_MAP: dict[str, tuple[str, ...]] = _derive()


def allowed_operand_classes(concept_name: str) -> tuple[str, ...] | None:
    """The closed class set this concept may serve, or ``None`` when the map does not know the
    concept — the caller's cue to refuse with a named marker, never to guess."""
    return _MAP.get(concept_name)


def operand_class_map() -> dict[str, tuple[str, ...]]:
    return dict(_MAP)


__all__ = ["OPERAND_CLASS_MAP_VERSION", "allowed_operand_classes", "operand_class_map"]
