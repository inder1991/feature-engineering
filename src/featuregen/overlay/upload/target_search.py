"""Registry search and near-duplicate detection — spec §7.5 Step 1, and deliberately the step with
no model call in it.

Ranked term overlap between the hypothesis and a label's name plus description. Deliberately not an
embedding or an LLM: this runs on every authoring request, the corpus is small, and a search nobody
can explain is a poor basis for "use this one instead". When the registry outgrows term overlap,
the existing catalog search is the thing to reuse — not a bespoke ranker here.
"""
from __future__ import annotations

import json
import re

from featuregen.overlay.upload.target_store import targets_for_entity

_WORD_RE = re.compile(r"[a-z0-9_]+")

#: Words that match everything and therefore rank nothing.
_STOP = frozenset({
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "is", "are", "will", "which",
    "who", "that", "this", "with", "by", "from", "at", "as", "be", "next", "customer", "customers",
    "predict", "predicts", "predicting", "likely", "more", "days", "within", "over",
})

#: Fields whose difference makes two rules the SAME question asked slightly differently. A
#: different column or shape is a different label; a different window or threshold is a twin.
_TWIN_FIELDS = ("window_days", "threshold")


def _terms(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP and len(w) > 2}


def search_targets(conn, *, entity: str, hypothesis: str, limit: int = 5) -> list[dict]:
    """Labels already registered for this entity, ranked against the hypothesis.

    Entity-scoped and not merely entity-filtered: a label at a different grain is not reusable
    however similar the words, so it must not appear as a reuse candidate at all.
    """
    wanted = _terms(hypothesis)
    if not wanted:
        return []
    scored: list[tuple[int, str, dict]] = []
    for row in targets_for_entity(conn, entity):
        # A label's NAME carries its meaning here (`tgt_npe_90d` -> npe), so both are searched.
        have = _terms(f"{row['name']} {row['description']}")
        overlap = wanted & have
        if overlap:
            hit = dict(row)
            hit["match_terms"] = tuple(sorted(overlap))
            scored.append((len(overlap), row["name"], hit))
    # Sorted by name within a score so a listing is stable across calls.
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [hit for _, _, hit in scored[:limit]]


def near_duplicates(conn, rule) -> list[dict]:
    """Registered labels differing from `rule` ONLY in a twin field.

    Content-addressing cannot catch these — the hashes differ, correctly — so the difference has to
    be stated before a person submits, or the registry fills with `tgt_churned_60d` beside
    `tgt_churned_90d` and nobody can say which one the bank means.
    """
    from featuregen.overlay.upload.target_contract import canonical_target

    # THROUGH JSON, deliberately. `canonical_target` yields TUPLES; the same rule read back from
    # the `rule` jsonb column yields LISTS, and `("Performing",) != ["Performing"]`. Comparing the
    # two directly makes `rest_same` False for every state_change rule, so twin detection silently
    # returns nothing while claiming to prevent exactly that. Normalising both sides through the
    # same round-trip the store performs is what makes the comparison mean anything.
    proposed = json.loads(json.dumps(canonical_target(rule)))
    proposed_head = dict(proposed["header"])
    out: list[dict] = []
    for row in targets_for_entity(conn, rule.header.entity):
        stored = row["rule"]
        if stored.get("shape") != proposed["shape"]:
            continue
        stored_head = dict(stored.get("header") or {})
        differs = tuple(f for f in _TWIN_FIELDS
                        if stored_head.get(f) != proposed_head.get(f))
        if not differs:
            continue
        ignore = _TWIN_FIELDS + ("name",)
        rest_same = (
            {k: v for k, v in stored_head.items() if k not in ignore}
            == {k: v for k, v in proposed_head.items() if k not in ignore}
            and {k: v for k, v in stored.items() if k != "header"}
            == {k: v for k, v in proposed.items() if k != "header"})
        if rest_same:
            hit = dict(row)
            hit["differs_in"] = differs
            out.append(hit)
    return out
