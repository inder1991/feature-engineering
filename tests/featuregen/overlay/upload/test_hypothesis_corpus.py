"""S1C-1 — the reviewed hypothesis corpus: the refusal table, the seed sweep, the packaging proof.

Three things are under test and they are deliberately different in kind:

1. **The refusal table.** A corpus that is wrong in ANY entry must fail loudly at load, naming the
   entry and the field. Stage 1C measures generation against these expectations; an expectation
   that silently dropped (or silently half-loaded) would make a metric read green for a reason
   nobody could see. Every row below is a mutation of ONE valid entry, so the message the test
   demands is the message an author actually gets.

2. **The seed sweep.** The shipped seed is not sampled — every recipe id it names is resolved in
   the live registry and every target entity is checked against ``known_entities()``, so a
   registry rename breaks this test rather than Stage 1C's numbers. The coverage quotas are
   asserted as FLOORS so a future edit that guts a measurement axis (cross-catalog reach,
   parameter divergence, pure LLM discovery) fails here.

3. **The packaging proof.** The corpus is runtime data. It is read as a package RESOURCE, not
   from the current directory — proved by loading with the process rooted somewhere else — and
   ``pyproject.toml`` must declare it as package-data or an installed wheel ships a corpus of
   zero entries while every loader call still "succeeds".
"""
from __future__ import annotations

import tomllib
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from featuregen.overlay.upload.hypothesis_corpus import (
    BANKING_DOMAINS,
    CORPUS_PACKAGE,
    CORPUS_RESOURCE_NAME,
    CORPUS_SCHEMA_VERSION,
    REVIEW_STATUSES,
    HypothesisExpectationV1,
    load_hypothesis_corpus,
    parse_hypothesis_corpus,
)
from featuregen.overlay.upload.recipe_contract_v2 import RecipeDefinitionV2
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

# ══ fixtures — one VALID entry, mutated per refusal row ══════════════════════════════════════════


def _entry(**overrides: Any) -> dict[str, Any]:
    """A valid entry built from live registry facts, so a refusal row fails for its OWN reason."""
    base: dict[str, Any] = {
        "corpus_id": "probe_entry",
        "hypothesis": "Customers whose salary stops arriving are leaving.",
        "banking_domain": "retail",
        "expected_recipe_ids": ["salary_credit_amount", "salary_regularity"],
        "expected_intent_themes": ["salary anchoring loss"],
        "implied_windows_days": [90],
        "expects_cross_catalog": False,
        "expected_target_entity": "customer",
        "review_status": "draft",
        "rationale": "probe",
    }
    base.update(overrides)
    return base


def _document(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": CORPUS_SCHEMA_VERSION, "entries": [deepcopy(e) for e in entries]}


def _without(field: str) -> dict[str, Any]:
    entry = _entry()
    del entry[field]
    return entry


# ══ 1. the refusal table ═════════════════════════════════════════════════════════════════════════

# (label, the mutated entry, substrings the message MUST carry — the entry and the field)
REFUSALS: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
    ("unknown domain",
     _entry(banking_domain="wholesale"),
     ("probe_entry", "banking_domain", "wholesale")),
    ("nonexistent recipe id",
     _entry(expected_recipe_ids=["salary_credit_amount", "no_such_recipe_at_all"]),
     ("probe_entry", "expected_recipe_ids", "no_such_recipe_at_all")),
    ("recipe id repeated inside one entry",
     _entry(expected_recipe_ids=["salary_regularity", "salary_regularity"]),
     ("probe_entry", "expected_recipe_ids", "salary_regularity")),
    ("unknown target entity",
     _entry(expected_target_entity="spacecraft"),
     ("probe_entry", "expected_target_entity", "spacecraft")),
    ("zero window",
     _entry(implied_windows_days=[0]),
     ("probe_entry", "implied_windows_days", "0")),
    ("negative window",
     _entry(implied_windows_days=[90, -30]),
     ("probe_entry", "implied_windows_days", "-30")),
    ("boolean masquerading as a window",
     _entry(implied_windows_days=[True]),
     ("probe_entry", "implied_windows_days")),
    ("non-integer window",
     _entry(implied_windows_days=["90"]),
     ("probe_entry", "implied_windows_days")),
    ("window repeated",
     _entry(implied_windows_days=[90, 90]),
     ("probe_entry", "implied_windows_days")),
    ("unknown review status",
     _entry(review_status="approved"),
     ("probe_entry", "review_status", "approved")),
    ("cross-catalog reach is not a boolean",
     _entry(expects_cross_catalog="yes"),
     ("probe_entry", "expects_cross_catalog")),
    ("blank corpus id",
     _entry(corpus_id="   "),
     ("corpus_id",)),
    ("blank hypothesis text",
     _entry(hypothesis=""),
     ("probe_entry", "hypothesis")),
    ("blank intent theme",
     _entry(expected_intent_themes=["  "]),
     ("probe_entry", "expected_intent_themes")),
    ("an entry that expects nothing at all",
     _entry(expected_recipe_ids=[], expected_intent_themes=[]),
     ("probe_entry", "expected_recipe_ids", "expected_intent_themes")),
    ("an unknown field",
     _entry(expected_precision=0.9),
     ("probe_entry", "expected_precision")),
    ("a missing field",
     _without("expects_cross_catalog"),
     ("probe_entry", "expects_cross_catalog")),
    ("rationale is not text",
     _entry(rationale=7),
     ("probe_entry", "rationale")),
    ("recipe ids given as bare text rather than a list",
     _entry(expected_recipe_ids="salary_regularity"),
     ("probe_entry", "expected_recipe_ids", "must be a list")),
    ("windows given as a bare integer rather than a list",
     _entry(implied_windows_days=90),
     ("probe_entry", "implied_windows_days", "must be a list")),
)


@pytest.mark.parametrize(
    ("entry", "expected_substrings"),
    [pytest.param(entry, expected, id=label) for label, entry, expected in REFUSALS],
)
def test_a_malformed_entry_refuses_naming_the_entry_and_the_field(
        entry: dict[str, Any], expected_substrings: tuple[str, ...]) -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_hypothesis_corpus(_document(entry))
    message = str(excinfo.value)
    for fragment in expected_substrings:
        assert fragment in message, f"{fragment!r} missing from refusal: {message}"


def test_the_valid_probe_entry_really_is_valid() -> None:
    """The control leg: without it every refusal row above could be passing for the wrong reason."""
    (parsed,) = parse_hypothesis_corpus(_document(_entry()))
    assert parsed.corpus_id == "probe_entry"


def test_a_repeated_corpus_id_refuses_naming_it() -> None:
    document = _document(_entry(), _entry(hypothesis="A different sentence, the same key."))
    with pytest.raises(ValueError) as excinfo:
        parse_hypothesis_corpus(document)
    assert "probe_entry" in str(excinfo.value)
    assert "duplicate" in str(excinfo.value).lower()


def test_a_corpus_under_another_schema_version_refuses() -> None:
    document = _document(_entry())
    document["schema_version"] = "hypothesis-corpus-v2"
    with pytest.raises(ValueError) as excinfo:
        parse_hypothesis_corpus(document)
    assert CORPUS_SCHEMA_VERSION in str(excinfo.value)
    assert "hypothesis-corpus-v2" in str(excinfo.value)


@pytest.mark.parametrize(
    ("document", "expected_substring"),
    [
        pytest.param(["an", "array"], "must be a JSON object", id="not a mapping"),
        # Deliberately NON-empty: an empty mapping would also trip the empty-corpus rule, and the
        # row would then pass without the type check existing at all.
        pytest.param({"schema_version": CORPUS_SCHEMA_VERSION, "entries": {"a": 1}},
                     "must be a list", id="entries is not a list"),
        pytest.param({"schema_version": CORPUS_SCHEMA_VERSION, "entries": ["salary_regularity"]},
                     "index 0", id="an entry is not a mapping"),
        pytest.param({"schema_version": CORPUS_SCHEMA_VERSION, "entries": []},
                     "empty", id="an empty corpus"),
        pytest.param({"schema_version": CORPUS_SCHEMA_VERSION, "entries": [_entry()],
                      "signed_off_by": "me"},
                     "signed_off_by", id="an unknown document key"),
    ],
)
def test_a_malformed_document_refuses(document: Any, expected_substring: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        parse_hypothesis_corpus(document)
    assert expected_substring in str(excinfo.value)


def test_validation_is_all_or_nothing() -> None:
    """A corpus whose SECOND entry is bad loads NOTHING — never a silently truncated corpus."""
    document = _document(_entry(corpus_id="good_entry"),
                         _entry(corpus_id="bad_entry", banking_domain="wholesale"))
    with pytest.raises(ValueError) as excinfo:
        parse_hypothesis_corpus(document)
    assert "bad_entry" in str(excinfo.value)


# ══ 2. the seed sweep ════════════════════════════════════════════════════════════════════════════


def _primary_window_days(recipe: RecipeDefinitionV2) -> int | None:
    """``ParameterSpecV2.allowed_values[0]`` of the recipe's day-window parameter, or None.

    None is a real answer: a minute-scale recipe (``window_minutes``) and a windowless one both
    have no day window to diverge FROM, so neither can evidence a parameter divergence."""
    for parameter in recipe.parameters:
        if parameter.name == "window" and parameter.allowed_values:
            value = parameter.allowed_values[0]
            return int(value) if isinstance(value, int) else None
    return None


@pytest.fixture(scope="module")
def seed() -> tuple[HypothesisExpectationV1, ...]:
    return load_hypothesis_corpus()


def test_the_seed_loads_typed_and_frozen(seed: tuple[HypothesisExpectationV1, ...]) -> None:
    assert seed, "the shipped corpus is empty"
    for entry in seed:
        assert isinstance(entry, HypothesisExpectationV1)
        assert isinstance(entry.expected_recipe_ids, tuple)
        assert isinstance(entry.implied_windows_days, tuple)
        with pytest.raises((AttributeError, TypeError)):
            entry.hypothesis = "mutated"  # type: ignore[misc]


def test_the_loader_is_stable_across_calls(seed: tuple[HypothesisExpectationV1, ...]) -> None:
    assert load_hypothesis_corpus() == seed


def test_every_expected_recipe_id_resolves_in_the_registry(
        seed: tuple[HypothesisExpectationV1, ...]) -> None:
    unresolved = [(entry.corpus_id, recipe_id)
                  for entry in seed
                  for recipe_id in entry.expected_recipe_ids
                  if v2_recipe_by_id(recipe_id) is None]
    assert unresolved == []


def test_every_expected_target_entity_is_known(
        seed: tuple[HypothesisExpectationV1, ...]) -> None:
    entities = known_entities()
    unknown = [(entry.corpus_id, entry.expected_target_entity)
               for entry in seed
               if entry.expected_target_entity not in entities]
    assert unknown == []


def test_the_shipped_corpus_carries_draft_only(
        seed: tuple[HypothesisExpectationV1, ...]) -> None:
    """Review is an OPERATOR act recorded as ``governed_plan_review_event`` rows. A file that
    called itself reviewed would be claiming evidence that does not exist."""
    assert {entry.review_status for entry in seed} == {"draft"}


def test_the_seed_covers_all_seven_domains_at_least_twice(
        seed: tuple[HypothesisExpectationV1, ...]) -> None:
    assert len(seed) >= 14
    per_domain = {domain: [e.corpus_id for e in seed if e.banking_domain == domain]
                  for domain in BANKING_DOMAINS}
    thin = {domain: ids for domain, ids in per_domain.items() if len(ids) < 2}
    assert thin == {}, f"domains with fewer than two hypotheses: {thin}"


def test_the_seed_covers_the_cross_catalog_axis(
        seed: tuple[HypothesisExpectationV1, ...]) -> None:
    reaching = [e.corpus_id for e in seed if e.expects_cross_catalog]
    assert len(reaching) >= 4, reaching
    # Both sides present, or the axis measures nothing.
    assert any(not e.expects_cross_catalog for e in seed)


def test_the_seed_covers_the_parameter_divergence_axis(
        seed: tuple[HypothesisExpectationV1, ...]) -> None:
    """A divergence entry implies a window the named recipes do NOT default to — the ground truth
    S1C-3's chooser accuracy is measured against."""
    diverging = []
    for entry in seed:
        if not entry.implied_windows_days:
            continue
        primaries = {window for window in
                     (_primary_window_days(v2_recipe_by_id(rid))  # type: ignore[arg-type]
                      for rid in entry.expected_recipe_ids)
                     if window is not None}
        if primaries and not primaries.issubset(set(entry.implied_windows_days)):
            diverging.append(entry.corpus_id)
    assert len(diverging) >= 5, diverging


def test_the_seed_covers_pure_llm_discovery(
        seed: tuple[HypothesisExpectationV1, ...]) -> None:
    """Entries the registry cannot answer at all: the measurement of whether the LLM reaches
    beyond the catalogue of recipes."""
    discovery = [e.corpus_id for e in seed
                 if not e.expected_recipe_ids and e.expected_intent_themes]
    assert len(discovery) >= 2, discovery


def test_every_seed_entry_carries_a_rationale(
        seed: tuple[HypothesisExpectationV1, ...]) -> None:
    """The rationale is what an SME reviews. An entry without one cannot be promoted."""
    silent = [e.corpus_id for e in seed if not e.rationale.strip()]
    assert silent == []


def test_seed_vocabularies_are_the_closed_ones() -> None:
    assert BANKING_DOMAINS == frozenset(
        {"retail", "cib", "payments", "cards", "customer", "accounts", "servicing"})
    assert REVIEW_STATUSES == frozenset({"draft", "reviewed"})


# ══ 3. the packaging proof ═══════════════════════════════════════════════════════════════════════


def test_the_corpus_is_read_as_a_package_resource(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Loaded with the process rooted elsewhere: a cwd-relative read would find nothing here."""
    assert resources.files(CORPUS_PACKAGE).joinpath(CORPUS_RESOURCE_NAME).is_file()
    load_hypothesis_corpus.cache_clear()
    monkeypatch.chdir(tmp_path)
    try:
        assert load_hypothesis_corpus()
    finally:
        load_hypothesis_corpus.cache_clear()


def test_pyproject_ships_the_corpus_in_the_wheel() -> None:
    """Without the package-data entry an installed release carries no corpus file at all — and
    the failure would surface as a missing resource in Stage 1C, far from its cause."""
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package_data = config["tool"]["setuptools"]["package-data"]
    patterns = package_data.get(CORPUS_PACKAGE, [])
    assert any(CORPUS_RESOURCE_NAME == pattern or pattern.endswith(".json")
               for pattern in patterns), package_data
