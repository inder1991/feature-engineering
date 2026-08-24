"""S1C-1 — the reviewed hypothesis corpus: what an SME says a banking question SHOULD produce.

Stage 1C measures governed cross-catalog planning against something. That something cannot be the
platform's own output, and it cannot be a number somebody liked the look of. It is this file: a
versioned set of banking hypotheses, each carrying an SME's declared expectation of which recipes
the question should reach, which intent themes an LLM should cover, which parameter windows the
wording implies, and whether answering it needs reach ACROSS catalogs.

**Three measurement axes, and why an entry may be honestly empty on one of them.**

* ``expected_recipe_ids`` — the registry's answer. An entry with NO recipe ids is not incomplete:
  it is the SME saying *the recipe catalogue cannot answer this*, which is exactly what makes it
  the ground truth for pure LLM discovery. Such an entry must still carry intent themes; an entry
  that expects nothing at all is unmeasurable and refuses at load.
* ``implied_windows_days`` — what the WORDING implies, in days. Empty is again a real answer: a
  hypothesis about card-testing bursts implies a *minutes* window, and the two recipes that serve
  it take ``window_minutes``. Writing a day figure there would fabricate a divergence measurement
  out of a unit mismatch, so the seed leaves it empty and says so in the rationale.
* ``expects_cross_catalog`` — the SME's judgement that answering the question needs more than one
  catalog. Both values must appear in the seed or the axis measures nothing.

**Review status is not editable here.** The vocabulary is closed over ``draft`` and ``reviewed``
because the TYPE must be able to express a reviewed expectation — but promotion is an OPERATOR
act, recorded as ``governed_plan_review_event`` rows (migration 1120), and Stage 1C's report joins
those rows to these entries. The packaged file therefore carries ``draft`` and only ``draft``,
pinned by test. A file that called itself reviewed would be claiming evidence that does not exist.

**Validation is all-or-nothing, and it is loud.** Every entry is checked against the LIVE registry
(``v2_recipe_by_id``) and the LIVE entity vocabulary (``known_entities()``), so a recipe rename
breaks the corpus load rather than quietly shrinking a Stage 1C denominator. Any violation raises
``ValueError`` naming the entry and the field; nothing partially loads.

No database, no provider: this module reads one packaged JSON file and two in-process registries.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

#: The corpus contract. A file under any other version is refused rather than best-effort read:
#: the expectations Stage 1C measures against must be the ones this loader understands.
CORPUS_SCHEMA_VERSION = "hypothesis-corpus-v1"

#: Where the data lives. Read as a package RESOURCE — an installed wheel has no source tree and no
#: predictable working directory, and a cwd-relative read would fail there, not here.
CORPUS_PACKAGE = "featuregen.overlay.upload"
CORPUS_RESOURCE_NAME = "hypothesis_corpus_v1.json"

#: The closed domain vocabulary. Seven, because Stage 1C reports resolution rate PER DOMAIN and a
#: domain with one hypothesis cannot carry a rate — the seed holds at least two of each.
BANKING_DOMAINS: frozenset[str] = frozenset(
    {"retail", "cib", "payments", "cards", "customer", "accounts", "servicing"})

#: The closed review vocabulary. See the module docstring: the FILE carries ``draft``.
REVIEW_STATUSES: frozenset[str] = frozenset({"draft", "reviewed"})

_DOCUMENT_KEYS: frozenset[str] = frozenset({"schema_version", "entries"})

_REQUIRED_FIELDS: tuple[str, ...] = (
    "corpus_id",
    "hypothesis",
    "banking_domain",
    "expected_recipe_ids",
    "expected_intent_themes",
    "implied_windows_days",
    "expects_cross_catalog",
    "expected_target_entity",
    "review_status",
)
_OPTIONAL_FIELDS: tuple[str, ...] = ("rationale",)
_ENTRY_KEYS: frozenset[str] = frozenset(_REQUIRED_FIELDS) | frozenset(_OPTIONAL_FIELDS)


@dataclass(frozen=True, slots=True)
class HypothesisExpectationV1:
    """One SME-declared expectation for one banking hypothesis.

    ``corpus_id`` is the identity and the join key onto review events; ``hypothesis`` is DISPLAY
    prose and never identity — rewording a hypothesis for clarity must not orphan its reviews."""

    corpus_id: str
    hypothesis: str
    banking_domain: str
    expected_recipe_ids: tuple[str, ...]
    expected_intent_themes: tuple[str, ...]
    implied_windows_days: tuple[int, ...]
    expects_cross_catalog: bool
    expected_target_entity: str
    review_status: str
    rationale: str = ""


@lru_cache(maxsize=1)
def load_hypothesis_corpus() -> tuple[HypothesisExpectationV1, ...]:
    """Every entry of the packaged corpus, typed and validated, or ``ValueError``.

    Cached: the corpus is immutable committed data and the registry cross-check is not free. The
    returned tuple of frozen dataclasses is safe to share. ``load_hypothesis_corpus.cache_clear()``
    exists for tests that move the process root."""
    raw = resources.files(CORPUS_PACKAGE).joinpath(CORPUS_RESOURCE_NAME).read_text(
        encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - a corrupt committed file
        raise ValueError(f"hypothesis corpus {CORPUS_RESOURCE_NAME!r} is not valid JSON: "
                         f"{exc}") from exc
    return parse_hypothesis_corpus(document)


def parse_hypothesis_corpus(document: Any) -> tuple[HypothesisExpectationV1, ...]:
    """Validate a corpus document and return its typed entries.

    Separate from :func:`load_hypothesis_corpus` so the rules can be exercised against authored
    payloads — the refusal table — without a file on disk."""
    if not isinstance(document, Mapping):
        raise ValueError("hypothesis corpus: the document must be a JSON object with "
                         f"{sorted(_DOCUMENT_KEYS)}, got {type(document).__name__}")
    # Version FIRST: a v2 corpus will legitimately carry fields v1 has never heard of, and
    # "unknown field 'x'" would be a misleading way to say "this is not a v1 corpus".
    version = document.get("schema_version")
    if version != CORPUS_SCHEMA_VERSION:
        raise ValueError(f"hypothesis corpus: field 'schema_version' is {version!r}, expected "
                         f"{CORPUS_SCHEMA_VERSION!r} — a corpus under another version is refused, "
                         "never best-effort read")
    unknown = sorted(set(document) - _DOCUMENT_KEYS)
    if unknown:
        raise ValueError(f"hypothesis corpus: unknown document field(s) {unknown} — the corpus "
                         f"document is closed over {sorted(_DOCUMENT_KEYS)}")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("hypothesis corpus: field 'entries' must be a list, got "
                         f"{type(raw_entries).__name__}")
    if not raw_entries:
        raise ValueError("hypothesis corpus: field 'entries' is empty — an empty corpus measures "
                         "nothing and must not load as if it did")

    entities = known_entities()
    entries: list[HypothesisExpectationV1] = []
    seen: dict[str, int] = {}
    for position, raw in enumerate(raw_entries):
        entry = _parse_entry(raw, position=position, entities=entities)
        if entry.corpus_id in seen:
            raise ValueError(
                f"hypothesis corpus entry {entry.corpus_id!r} (index {position}): duplicate "
                f"'corpus_id' — already used at index {seen[entry.corpus_id]}; the corpus_id is "
                "the identity a review event joins onto and must be unique")
        seen[entry.corpus_id] = position
        entries.append(entry)
    return tuple(entries)


def _parse_entry(raw: Any, *, position: int,
                 entities: frozenset[str]) -> HypothesisExpectationV1:
    if not isinstance(raw, Mapping):
        raise ValueError(f"hypothesis corpus entry at index {position}: must be a JSON object, "
                         f"got {type(raw).__name__}")

    # The corpus_id first: every later message names the entry, so it has to be trustworthy.
    corpus_id = raw.get("corpus_id")
    if not isinstance(corpus_id, str) or not corpus_id.strip():
        raise ValueError(f"hypothesis corpus entry at index {position}: field 'corpus_id' must be "
                         f"non-blank text, got {corpus_id!r}")
    where = f"hypothesis corpus entry {corpus_id!r} (index {position})"

    unknown = sorted(set(raw) - _ENTRY_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {unknown} — the entry schema is closed over "
                         f"{sorted(_ENTRY_KEYS)}; a field nothing reads is an expectation nothing "
                         "measures")
    missing = sorted(field for field in _REQUIRED_FIELDS if field not in raw)
    if missing:
        raise ValueError(f"{where}: missing required field(s) {missing}")

    hypothesis = _text(raw["hypothesis"], where=where, field="hypothesis")
    rationale = raw.get("rationale", "")
    if not isinstance(rationale, str):
        raise ValueError(f"{where}: field 'rationale' must be text, got "
                         f"{type(rationale).__name__}")

    banking_domain = _closed(raw["banking_domain"], BANKING_DOMAINS,
                             where=where, field="banking_domain")
    review_status = _closed(raw["review_status"], REVIEW_STATUSES,
                            where=where, field="review_status")
    target_entity = _closed(raw["expected_target_entity"], entities,
                            where=where, field="expected_target_entity",
                            note="the vocabulary is known_entities() — every distinct concept "
                                 "entity_link; an entity outside it can never be planned for")

    recipe_ids = _string_tuple(raw["expected_recipe_ids"], where=where,
                               field="expected_recipe_ids")
    for recipe_id in recipe_ids:
        if v2_recipe_by_id(recipe_id) is None:
            raise ValueError(f"{where}: field 'expected_recipe_ids' names {recipe_id!r}, which is "
                             "not in the V2 recipe registry — an expectation about a recipe that "
                             "does not exist can never be met")

    intent_themes = _string_tuple(raw["expected_intent_themes"], where=where,
                                  field="expected_intent_themes")
    if not recipe_ids and not intent_themes:
        raise ValueError(f"{where}: both 'expected_recipe_ids' and 'expected_intent_themes' are "
                         "empty — an entry that expects nothing cannot be measured against "
                         "anything; a pure-discovery entry must still declare its intent themes")

    windows = _window_tuple(raw["implied_windows_days"], where=where,
                            field="implied_windows_days")

    expects_cross_catalog = raw["expects_cross_catalog"]
    if not isinstance(expects_cross_catalog, bool):
        raise ValueError(f"{where}: field 'expects_cross_catalog' must be a boolean, got "
                         f"{expects_cross_catalog!r} — cross-catalog reach is a declared "
                         "expectation, never an inferred truthiness")

    return HypothesisExpectationV1(
        corpus_id=corpus_id,
        hypothesis=hypothesis,
        banking_domain=banking_domain,
        expected_recipe_ids=recipe_ids,
        expected_intent_themes=intent_themes,
        implied_windows_days=windows,
        expects_cross_catalog=expects_cross_catalog,
        expected_target_entity=target_entity,
        review_status=review_status,
        rationale=rationale,
    )


def _text(value: Any, *, where: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: field {field!r} must be non-blank text, got {value!r}")
    return value


def _closed(value: Any, vocabulary: frozenset[str], *, where: str, field: str,
            note: str = "") -> str:
    if not isinstance(value, str) or value not in vocabulary:
        suffix = f" — {note}" if note else ""
        raise ValueError(f"{where}: field {field!r} is {value!r}, which is not one of "
                         f"{sorted(vocabulary)}{suffix}")
    return value


def _string_tuple(value: Any, *, where: str, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where}: field {field!r} must be a list, got {type(value).__name__}")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where}: field {field!r} contains {item!r} — every member must be "
                             "non-blank text")
        if item in items:
            raise ValueError(f"{where}: field {field!r} repeats {item!r} — a repeated expectation "
                             "would double-count in every Stage 1C rate")
        items.append(item)
    return tuple(items)


def _window_tuple(value: Any, *, where: str, field: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where}: field {field!r} must be a list, got {type(value).__name__}")
    windows: list[int] = []
    for item in value:
        # `bool` is a subclass of `int`; True is not a window length.
        if isinstance(item, bool) or not isinstance(item, int):
            raise ValueError(f"{where}: field {field!r} contains {item!r} — every implied window "
                             "must be an integer number of days")
        if item <= 0:
            raise ValueError(f"{where}: field {field!r} contains {item} — an implied window must "
                             "be a positive number of days")
        if item in windows:
            raise ValueError(f"{where}: field {field!r} repeats {item} — a repeated window would "
                             "double-count in the parameter-divergence measurement")
        windows.append(item)
    return tuple(windows)
