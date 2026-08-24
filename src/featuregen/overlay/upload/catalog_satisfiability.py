"""T5 — catalog satisfiability: refuse with directions, before anything is planned.

The 2026-08-24 quality audit's first finding was not about a card. It was about the CATALOG: the
live AML run named ``cib`` — a customer master with ZERO ``monetary_flow`` columns and one
``event_timestamp`` (a consent date) — while ``ftr`` (126 columns, 53 confirmed concepts, every
transaction semantic the brief needed) sat unplanned. Nothing warned. The run planned anyway,
bound most operands of most recipes, and served 135 cards that could never compute.

T2 stops those cards reaching a lane. This module answers the question T2 structurally cannot: it
holds one assembled set and one catalog name, so it can say "``monetary_flow`` did not bind" but
never "``ftr`` carries it". That claim needs the cross-catalog concept inventory, and the inventory
needs a connection — which exists at the ROUTE, before the run is minted. So the refusal lives
here and fires there, in the same place and for the same reason as
``SEMANTIC_REQUIRES_CATALOG_SOURCE``: an honest refusal before any durable write beats a page of
setup work rendered as a generation result.

──────────────────────────────────────────────────────────────────────────────────────────────
THE FLOOR, and why it is this one
──────────────────────────────────────────────────────────────────────────────────────────────

**A named catalog fails satisfiability when some operand CLASS that a MAJORITY of the eligible
recipes REQUIRE is covered by NOTHING on it** — no read-scoped column carries any concept the
eligible corpus asks for in that class, by name or through the registered concept closure —
**AND another readable catalog carries at least one of those concepts**, so the refusal has a
direction to give.

Five sentences of rationale, because a floor without one is a magic constant:

1. **The unit is a CLASS, not a concept, because a class is what a recipe needs a column to BE** —
   a magnitude to measure, a key to compute per, a clock to place events on. The concept-level
   version of this floor was written first and MEASURED WRONG: under a churn scope, 30 of 44
   eligible recipes require ``account_id`` and a customer-grained catalog carries none — so a
   concept floor refuses a catalog that serves real cards today. Its class ``entity_key`` is
   covered, by ``customer_id``. "Cannot reach the account-grained recipes" is a grain fact the UOA
   fold already owns and states per card; "has no key at all" is a catalog-aiming fact. Only the
   second is a refusal.
2. **Zero coverage is the only "did not bind" nobody on this catalog can repair.** The binder has
   three non-bound conditions and only one is an absence: ``ambiguous`` is adjudicable (several
   columns carry it, a human picks), ``blocked`` is reviewable (one matched, its evidence did not
   clear), ``unresolved`` is neither — there is nothing to choose. A floor built on absence never
   withholds work somebody could actually do here.
3. **A majority is the bar, and it is not a round number chosen for comfort.** Measured across
   every fixture below, the ``status`` class is a GAP on ALL of them — the registry's
   ``booking_status`` / ``account_status`` concepts have no catalog mapping yet, which is a known,
   owner-gated SME item — and it is required by 40–43% of the eligible recipes in every scope
   tried. A floor set anywhere below a majority would refuse every catalog in the estate over a
   gap the platform has already looked at and already decided not to close here.
4. **Strictly more than half, so an exact tie serves rather than refuses.** The fail-open direction
   is deliberate: T2 already guarantees no unbindable candidate becomes a card, so a refusal here
   withholds work rather than preventing junk — and a refusal fired without warrant is the same
   defect as a card served without one.
5. **A refusal must be actionable, or it is not a refusal.** No pre-planning statistic can tell a
   mis-aimed catalog from a genuinely narrow one — measured, the audit's ``cib`` (5 columns, 1 of
   15 eligible recipes structurally servable) and a purpose-built single-recipe fixture (6 columns,
   1 of 17) are the same object by every number this seam can see. What separates them is whether
   there is anywhere to go: ``ftr`` sat beside ``cib`` carrying the transaction semantics, and the
   narrow fixture's estate holds the concept nowhere. So the refusal fires only when the inventory
   answers. When it does not, the run proceeds and T2's ``needs_setup`` lane reports the same
   absence per card, in the binder's own words, at the right granularity — and, decisively, WITHOUT
   withholding the candidates that do compute. Stopping a whole run to deliver information the run
   itself delivers, and killing its working candidates on the way, is strictly worse.

**Measured on the 317-recipe V2 registry (2026-08-24)** — the classes a majority of eligible
recipes REQUIRE, and whether the catalog covers each. ``cib`` is the audit's customer master,
``ftr`` its transaction sibling, ``bank`` the 6-column customer-grained fixture the serving suite
runs on:

==========================  =======  ====================================================
scope / catalog             verdict  majority classes (share of eligible recipes)
==========================  =======  ====================================================
``aml_cft.susp…`` / cib     REFUSE   entity_key 100% ✓, event_timestamp 100% ✓,
                                     dimension 67% ✓, **measure 53% ✗ — and ftr has it**
``aml_cft.susp…`` / ftr     serve    entity_key ✓, event_timestamp ✓, dimension ✓,
                                     measure ✓
``churn`` / bank            serve    entity_key 100% ✓, event_timestamp 57% ✓
``churn`` / ftr             serve    entity_key ✓, event_timestamp ✓
``credit.monitoring`` /     serve    entity_key ✓, **measure 71% ✗ — and no readable
one-recipe fixture                   catalog carries any of the 10 concepts** (sentence 5)
BROADEN (unscoped) / cib    REFUSE   n=317, floor 158: entity_key 100% ✓,
                                     event_timestamp 63% ✓, **measure 55% ✗ (175/317)**
BROADEN (unscoped) / ftr    serve    the same three, all covered
==========================  =======  ====================================================

**BROADEN is governed identically — an owner's ruling (2026-08-25), not an oversight.**
``confirmed_scope.unscoped=true`` reaches the same route path with a scope ``v2_applicability``
fails OPEN on, so the eligible corpus is the whole registry and the floor is 158. The one law does
not care how wide the scope is: a mis-aimed catalog refuses with directions whether the human asked
for one use-case leaf or for everything; clause 5 already protects the nowhere-to-point case; and
on an exploratory gesture "aim at ftr instead" is MORE useful than a page of setup work, not less.

Two measured cautions about that width, both worth knowing before reading a broaden verdict:

* At n=317 the ``measure`` class asks for ~90 different concepts, so ONE column of almost any
  magnitude covers it. The floor at broaden width therefore refuses a catalog carrying no magnitude
  AT ALL — not one that is merely narrow. That is the intended severity, and it is why the refusing
  fixture has to be a customer master with no numeric measure on it.
* Consequently the verdict is sensitive to a single column. Measured: the 4-concept cib fixture
  (``customer_id``, ``event_timestamp``, ``category_code``, ``boolean_flag``) REFUSES a broaden;
  adding one ``customer_risk_rating`` column makes it SERVE, because some recipe in the 317 asks
  for ``customer_risk_rating`` as a ``measure``. Both are honest answers to "can anything here be
  measured?" — but a reader comparing two broaden verdicts must compare the catalogs' concepts, not
  their column counts.

Three honest readings of that table:

* The floor NEVER fires on a catalog that serves the brief, and it fires on the one that could not.
  Under every scope measured, ``status`` (40–43%) and ``policy_input`` (9–20%) sit below the floor
  on every catalog — which is sentence 3, in the data.
* The last row is sentence 5 doing its work, and it is not hypothetical: without it, the
  four-objective coverage journeys — narrow catalogs built one column per operand, each of which
  governs its hero recipe end to end — were refused outright. 12 of their 17 eligible recipes
  genuinely cannot compute there, and saying so is true; withholding the one that can, to say it,
  is not a trade this program makes.
* It does NOT fire on every arrangement where it arguably could: widening the AML scope to
  ``aml_cft`` + descendants drops the ``measure`` share to 45%, so cib passes and 19 of 20 eligible
  recipes go to ``needs_setup`` with no directions attached. That is the accepted cost of sentence
  4, and it is the cost worth paying: a floor that refuses the right catalog is worse than one that
  occasionally fails to refuse the wrong one.

Query budget: ONE statement on the satisfiable path (the named catalog's concept set), TWO when the
floor is breached (plus the cross-catalog inventory, for the breaching concepts only).
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.recipe_planning_lens import v2_applicability
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

#: The typed refusal code, named once. Spelled here rather than in the route for the reason
#: ``_NOT_BINDABLE`` is named in ``semantic_projection``: a code that exists in two spellings drifts.
CATALOG_CANNOT_SATISFY_SCOPE = "CATALOG_CANNOT_SATISFY_SCOPE"


@dataclass(frozen=True, slots=True)
class UnsatisfiableConceptV1:
    """One concept the eligible corpus asks for in an unsatisfiable class, and where it lives.

    ``required_by`` counts eligible RECIPES, not operands: a recipe asking for ``monetary_flow`` in
    two roles is one recipe that cannot compute, not two.

    ``available_in`` is the direction — the OTHER read-scoped catalogs that carry this concept BY
    NAME, with how many columns each has, strongest first. Empty is a fact and not a failure: it
    means nothing this caller can read carries the concept, which is a different (and more
    expensive) remedy than re-aiming the brief.

    The name lookup is deliberately STRICTER than the local coverage test, which follows the
    binder's full retrieval law including the concept closure. Both asymmetries err the same way:
    the local test is generous, so a catalog is never refused for a concept the binder might have
    served; the remote claim is strict, because "``ftr`` carries something an ancestor of this
    reaches" is not a direction an operator can act on."""

    concept: str
    required_by: int
    available_in: tuple[tuple[str, int], ...] = ()

    def to_json(self) -> dict:
        return {"concept": self.concept, "required_by": self.required_by,
                "available_in": [{"catalog_source": src, "columns": n}
                                 for src, n in self.available_in]}

    def phrase(self) -> str:
        """This concept's clause inside the class's sentence — the direction when one exists,
        honest absence when it does not."""
        head = f"{self.concept} ({self.required_by})"
        if not self.available_in:
            return f"{head} — carried by no catalog you can read"
        named = ", ".join(f"{src} ({n} column{'s' if n != 1 else ''})"
                          for src, n in self.available_in)
        return f"{head} — in {named}"


@dataclass(frozen=True, slots=True)
class UnsatisfiableClassV1:
    """One operand CLASS a majority of the eligible recipes require and this catalog cannot serve.

    Every concept in ``concepts`` is uncovered BY CONSTRUCTION: the class is unsatisfiable exactly
    when none of the concepts the corpus asks for in it is covered. So this carries the whole ask,
    not a sample of it, and the counts say which part of the ask matters most.

    ``recipe_ids`` names the eligible recipes that require the class, in authored registry order —
    so the refusal can be audited against the corpus rather than believed."""

    operand_class: str
    required_by: int
    recipe_ids: tuple[str, ...]
    concepts: tuple[UnsatisfiableConceptV1, ...]

    def to_json(self) -> dict:
        return {"operand_class": self.operand_class, "required_by": self.required_by,
                "recipe_ids": list(self.recipe_ids),
                "concepts": [c.to_json() for c in self.concepts],
                "sentence": self.sentence()}

    def sentence(self) -> str:
        return (f"no read-scoped column can serve a {self.operand_class} operand, which "
                f"{self.required_by} of the eligible recipes require: "
                + "; ".join(c.phrase() for c in self.concepts))


@dataclass(frozen=True, slots=True)
class CatalogSatisfiabilityV1:
    """The pre-planning verdict on one (catalog, confirmed scope) pair.

    Deliberately a VALUE, not an exception: the same object answers "serve" and "refuse", the route
    decides what to do with it, and a caller that wants the numbers without the refusal (a probe, a
    future admin view) gets them from the same function the route uses."""

    catalog_source: str
    eligible_recipes: int
    #: The count a class's ``required_by`` must EXCEED — ``eligible_recipes // 2``. Carried rather
    #: than recomputed so the served payload states the bar it was judged against.
    majority_floor: int
    unsatisfiable: tuple[UnsatisfiableClassV1, ...]

    @property
    def satisfied(self) -> bool:
        return not self.unsatisfiable

    def message(self) -> str:
        return (f"{self.catalog_source} cannot serve this scope: "
                + "; ".join(c.sentence() for c in self.unsatisfiable))

    def to_json(self) -> dict:
        return {"code": CATALOG_CANNOT_SATISFY_SCOPE,
                "message": self.message(),
                "catalog_source": self.catalog_source,
                "eligible_recipes": self.eligible_recipes,
                "majority_floor": self.majority_floor,
                "unsatisfiable_classes": [c.to_json() for c in self.unsatisfiable],
                "satisfying_catalog_sources": list(self.satisfying_catalog_sources())}

    def satisfying_catalog_sources(self) -> tuple[str, ...]:
        """Every catalog named as carrying at least one of the unsatisfiable concepts, ordered by
        HOW MANY of them it carries (then by name). The head of this tuple is the re-aim the
        operator is being pointed at — ``ftr`` in the audit's arrangement — and the whole tuple is
        served because a scope whose gaps split across two catalogs has no single answer and must
        not be given one.

        Counted over DISTINCT concepts, not over class×concept rows: a concept the corpus asks for
        in two different classes (``account_id`` is an ``entity_key`` in one recipe and a
        ``dimension`` in another) is one gap this catalog closes, not two, and double-counting it
        would let a catalog answering one popular concept outrank a catalog answering three."""
        counted: dict[str, set[str]] = {}
        for unsatisfiable in self.unsatisfiable:
            for concept in unsatisfiable.concepts:
                for source, _columns in concept.available_in:
                    counted.setdefault(source, set()).add(concept.concept)
        return tuple(source for source, _ in
                     sorted(counted.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def _catalog_concepts(conn, *, catalog_source: str, roles) -> frozenset[str]:
    """The concepts this caller can see on this catalog — the SAME predicate
    ``build_generation_semantic_context`` loads its columns with (``kind='column'``, the catalog,
    ``visible_requires <@ allowed``), projected to the concept. So this set IS the key set of the
    context's ``concept_index``, one aggregate query earlier and without the column bodies.

    ``concept <> ''`` rides beside ``IS NOT NULL`` because the loader's own filter is the truthiness
    test ``if col.concept``, which drops the empty string as well as NULL. Without it a column
    enriched to '' would be a KEY in this set and not in the context's index — a coverage claim the
    binder could not honour, in the one direction that matters (it would suppress a refusal)."""
    rows = conn.execute(
        "SELECT DISTINCT concept FROM graph_node "
        "WHERE kind = 'column' AND catalog_source = %s AND concept IS NOT NULL "
        "AND concept <> '' AND visible_requires <@ %s",
        (catalog_source, allowed_sensitivities(tuple(roles)))).fetchall()
    return frozenset(row[0] for row in rows)


def concept_inventory(conn, *, concepts, exclude_catalog_source: str,
                      roles) -> dict[str, tuple[tuple[str, int], ...]]:
    """The cross-catalog concept inventory: which OTHER read-scoped catalogs carry these concepts.

    One grouped read over ``graph_node`` — the enrichment-written ``concept`` column, which is the
    same fact the generation context indexes and the binder shortlists from, so "``ftr`` carries
    ``monetary_flow``" means exactly "aim there and the context will index it". Read-scoped by the
    caller's roles for the same reason every other catalog read is: a refusal must never name a
    catalog its reader is not allowed to know exists."""
    if not concepts:
        return {}
    rows = conn.execute(
        "SELECT concept, catalog_source, count(*) FROM graph_node "
        "WHERE kind = 'column' AND concept = ANY(%s) AND catalog_source <> %s "
        "AND visible_requires <@ %s "
        "GROUP BY concept, catalog_source",
        (list(concepts), exclude_catalog_source,
         allowed_sensitivities(tuple(roles)))).fetchall()
    by_concept: dict[str, list[tuple[str, int]]] = {}
    for concept, source, columns in rows:
        by_concept.setdefault(concept, []).append((source, int(columns)))
    return {concept: tuple(sorted(found, key=lambda sc: (-sc[1], sc[0])))
            for concept, found in by_concept.items()}


def _covered(concept: str, present: frozenset[str]) -> bool:
    """Could ANY read-scoped column on this catalog be RETRIEVED for an operand asking this?

    The binder's own retrieval law (``recipe_operand_policy.request_shortlists``), restated over
    concept NAMES instead of refs: the authored concept itself, or any enriched concept whose
    registered closure (self + is-a ancestors + namespace mates) reaches it. Retrieval only —
    whether the column then BINDS is eligibility's decision and this seam never pre-empts it.
    Being generous is the point: a catalog is refused only for a class the binder could not even
    have looked at a column for.

    ``request_shortlists``' third widening — the operand's ``alternative_concepts`` — is absent
    here because a RECIPE operand cannot express one: ``OperandSpecV2`` has no such field and
    ``_operand_from_spec`` leaves the request's tuple empty. Alternatives exist only on LLM-intent
    operands, and intents are not part of the eligible-recipe corpus this floor measures. Spelling
    a widening the corpus cannot use would be a claim about nothing."""
    from featuregen.overlay.upload.concepts import concept_path, namespace_mates

    if concept in present:
        return True
    return any(concept in {*concept_path(enriched), *namespace_mates(enriched)}
               for enriched in present)


def assess_catalog_satisfiability(conn, *, catalog_source: str, scope,
                                  roles=()) -> CatalogSatisfiabilityV1:
    """The pre-planning verdict. See the module docstring for the floor and its rationale.

    Costs ONE query when the catalog satisfies the scope (the concept set), TWO when it does not
    (plus the inventory, for the breaching concepts only) — so the happy path, which is every
    request on a correctly-aimed catalog, pays one indexed aggregate. The recipe side reads the
    REGISTRY DEFINITIONS directly rather than projecting each into a planning request: the
    projection re-hashes every definition (``governed_scope_material`` measured 131 ms of pure CPU
    for the 317-recipe corpus) to produce an operand tuple whose ``concept`` / ``operand_class`` /
    ``required`` fields are copied straight off the spec."""
    eligible = sorted(v2_applicability(scope).eligible_ids)
    if not eligible:
        # Nothing was eligible, so nothing is unsatisfiable. An empty scope is a SCOPE problem and
        # this seam must not dress it up as a catalog one.
        return CatalogSatisfiabilityV1(catalog_source=catalog_source, eligible_recipes=0,
                                       majority_floor=0, unsatisfiable=())
    present = _catalog_concepts(conn, catalog_source=catalog_source, roles=roles)
    floor = len(eligible) // 2            # a class must be required by MORE than this

    # class -> the eligible recipes that REQUIRE it; (class, concept) -> the same, per concept.
    # Both deduplicate per recipe (a recipe asking in two roles is one recipe that cannot compute,
    # not two) and stay in authored registry order.
    by_class: dict[str, list[str]] = {}
    by_concept: dict[tuple[str, str], list[str]] = {}
    for recipe_id in eligible:
        definition = v2_recipe_by_id(recipe_id)
        if definition is None:
            continue                      # `v2_applicability` folds over V2_RECIPES, so an
        seen_classes: set[str] = set()     # eligible id the registry does not hold is
        seen_pairs: set[tuple[str, str]] = set()          # unreachable; fail open.
        for operand in definition.operands:
            if not operand.required:
                continue                  # an absent OPTIONAL operand degrades, never blocks
            if operand.operand_class not in seen_classes:
                seen_classes.add(operand.operand_class)
                by_class.setdefault(operand.operand_class, []).append(recipe_id)
            pair = (operand.operand_class, operand.concept)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                by_concept.setdefault(pair, []).append(recipe_id)

    breaching = {
        operand_class: recipe_ids for operand_class, recipe_ids in by_class.items()
        if len(recipe_ids) > floor
        and not any(_covered(concept, present)
                    for (cls, concept) in by_concept if cls == operand_class)}
    asked = sorted({concept for (cls, concept) in by_concept if cls in breaching})
    inventory = concept_inventory(conn, concepts=asked,
                                  exclude_catalog_source=catalog_source, roles=roles)
    # Sentence 5 — a refusal must be ACTIONABLE. A breaching class the estate cannot answer is
    # dropped here rather than refused: the run proceeds and T2's `needs_setup` lane says the same
    # thing per card, without withholding the candidates that do compute.
    breaching = {operand_class: recipe_ids
                 for operand_class, recipe_ids in breaching.items()
                 if any(inventory.get(concept)
                        for (cls, concept) in by_concept if cls == operand_class)}
    unsatisfiable = tuple(
        UnsatisfiableClassV1(
            operand_class=operand_class, required_by=len(recipe_ids),
            recipe_ids=tuple(recipe_ids),
            concepts=tuple(sorted(
                (UnsatisfiableConceptV1(concept=concept, required_by=len(wanting),
                                        available_in=inventory.get(concept, ()))
                 for (cls, concept), wanting in by_concept.items() if cls == operand_class),
                key=lambda c: (-c.required_by, c.concept))))
        for operand_class, recipe_ids in sorted(breaching.items(),
                                                key=lambda kv: (-len(kv[1]), kv[0])))
    return CatalogSatisfiabilityV1(
        catalog_source=catalog_source, eligible_recipes=len(eligible),
        majority_floor=floor, unsatisfiable=unsatisfiable)


__all__ = ["CATALOG_CANNOT_SATISFY_SCOPE", "CatalogSatisfiabilityV1", "UnsatisfiableClassV1",
           "UnsatisfiableConceptV1", "assess_catalog_satisfiability", "concept_inventory"]
