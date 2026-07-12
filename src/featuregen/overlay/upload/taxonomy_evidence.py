"""Concept-registry taxonomy derivation — behaviour DERIVED from the concept, at the concept's strength.

Once a column's concept is known (``concepts.py``), its behavioural fields are DERIVED from the
registry rather than independently hallucinated: ``monetary_stock`` is semi-additive, ``as_of_date``
carries the ``as_of`` PIT role, ``outcome_label`` is a leakage anchor. This module turns a concept +
the strength at which that concept was asserted into a list of ``(field_name, value, strength)``
evidence triples for the reasoning layer to resolve (Task 8).

STRENGTH PROPAGATION (spec §3.2) is the load-bearing invariant. A derivation can never be more
certain than the concept it was derived from: every emitted triple carries EXACTLY the input
``concept_strength``. A derivation from an ``llm/proposed`` concept therefore yields ``proposed``
derivations (which Task-8 policy blocks from gating), and a derivation from a human-``confirmed``
concept yields ``confirmed`` ones — the derivation code never upgrades strength.

The four behavioural fields split into two kinds:
  * ``additivity`` — an aggregation-semantics field with an explicit not-applicable sentinel ``"n/a"``.
    Derived ONLY when the concept declares a real aggregation rule (skip the ``n/a`` default), so a
    non-aggregating concept (identifier, categorical, date) emits no additivity behaviour.
  * ``temporal_role`` / ``sensitivity_floor`` / ``leakage_anchor`` — safety & lineage fields that are
    ALWAYS a meaningful assertion (every column has SOME PIT role incl. ``"none"``, SOME sensitivity
    floor incl. ``"public"``, and either IS or ISN'T a leakage anchor). Always derived so resolution
    gets a complete, strength-tagged safety picture.

``sensitivity`` is emitted under the DISTINCT name ``"sensitivity_floor"`` (review #8): it is a FLOOR
fed to ``safety_floor.apply_sensitivity_floor`` at resolution — a most-restrictive lower bound — NOT
an operational sensitivity classification. Keeping the name distinct stops Task 8 from mistaking a
derived floor for a measured classification.
"""
from __future__ import annotations

from featuregen.overlay.evidence import AssertionStrength
from featuregen.overlay.upload.concepts import concept as lookup_concept

# The additivity value the registry uses for "aggregation does not apply to this concept" (an
# identifier, a coded category, a bare date). Nothing to derive when a concept carries it.
_ADDITIVITY_NOT_APPLICABLE = "n/a"


def derive_concept_evidence(
    concept: str, concept_strength: AssertionStrength
) -> list[tuple[str, object, AssertionStrength]]:
    """Derive the behavioural evidence a concept implies, at the concept's own strength.

    Returns ``(field_name, value, strength)`` triples for the behavioural fields the registry defines
    for ``concept``. STRENGTH PROPAGATION: every triple's ``strength`` is EXACTLY ``concept_strength``
    — a derivation is never more certain than the concept it came from.

    Fields:
      * ``additivity`` — the concept's aggregation rule, emitted only when it is applicable (skip the
        ``"n/a"`` not-applicable sentinel).
      * ``temporal_role`` — the concept's ``pit_role`` (point-in-time role), always emitted.
      * ``sensitivity_floor`` — the concept's ``sensitivity`` as a FLOOR (review #8: fed to
        ``apply_sensitivity_floor``, never treated as an operational classification), always emitted.
      * ``leakage_anchor`` — whether the concept IS a leakage anchor (a target / target-defining
        column features must never be built from), always emitted.

    An unknown / ``UNCLASSIFIED`` concept (not in the registry) derives nothing → ``[]``.
    """
    record = lookup_concept(concept)
    if record is None:
        return []

    triples: list[tuple[str, object, AssertionStrength]] = []
    # additivity: only when the concept declares a real aggregation rule (skip the "n/a" default).
    if record.additivity != _ADDITIVITY_NOT_APPLICABLE:
        triples.append(("additivity", record.additivity, concept_strength))
    # Safety & lineage behaviour — always a meaningful assertion, so always derived.
    triples.append(("temporal_role", record.pit_role, concept_strength))
    triples.append(("sensitivity_floor", record.sensitivity, concept_strength))
    triples.append(("leakage_anchor", record.leakage_anchor, concept_strength))
    return triples
