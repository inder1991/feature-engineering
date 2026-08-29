"""The intake ticket — one mandatory read of the hypothesis, one cached structured answer.

The #2 spec's engine (router plan, owner-decided 2026-08-10): every new hypothesis gets ONE
extraction filling the full ticket — target column, label window, target type, business domain —
because the safety machinery downstream is only as good as this reading. The veto needs the target;
the near-label critic needs the window; the menu ordering needs the domain. An exact-name match
alone would silently drop the other three fields — the hole the owner caught in the staged design.

Discipline, in one line each:

* SELECTION, never generation — the model picks the target FROM the shortlist we send (ref +
  concept + one-line summary per column, all pre-classified egress keys) or abstains; a name not in
  the catalog is treated as abstain, never trusted.
* A literally-typed column PINS — code matches it before any model runs, the model cannot override
  it, and a prose-vs-name disagreement surfaces as a `contradiction` for the confirm screen.
* Content-keyed replay through `structured_result` — the key covers the hypothesis, the shortlist
  content, the use-case vocabulary and the prompt version (the second-review correction: "cached by
  hypothesis text" hashed one input of four).
* Failure degrades, never blocks — no client / fault / ceiling yields a ticket with the pinned
  target (if any) and honest abstains everywhere else.
* OUTCOME OR PROXY, never silently either (T7, 2026-08-24). A proposal COMMITS only when the
  target's concept is outcome-family; anything else abstains and hands back two catalog-derived
  lists — the nearest proxies, and any label the catalog actually holds — each entry labelled with
  the concept it really carries. Neither list ever changes the target: reporting is not choosing.
* WHAT THE REGISTRY WARRANTS, AND NO MORE (T7 fix round). "Uncommittable" and "a proxy for the
  outcome" are different claims. Only ``near_label`` earns the second; ``standard`` is the
  registry's positive denial and an unregistered concept is silence, so both are gated without
  ever being called proxies. See :func:`is_outcome_family` and :func:`asserts_label_adjacency`.
* WINDOW OR ABSENCE, never a contradiction (T7). The goal text's stated horizon is extracted
  deterministically and cross-checked against the model's number; a disagreement is a typed
  refusal that accepts NO window, so the near-label critic abstains for a stated reason. A
  DEGRADED ticket still reads the goal — but never manufactures a contradiction with a reading
  that never happened.
* The human confirmation gate (B2) consumes this ticket: the ticket is a DRAFT reading,
  `llm/proposed` in spirit, until a person signs the target — and the signed reading lands on
  `contract_intent` via :func:`record_target_reading` (migration 1059), where the existing
  server-side leakage path already reads. Model drafts live in `structured_result`; human decisions
  live with the intent they govern.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.concepts import concept as _concept_record
from featuregen.overlay.upload.recipe_contract_v2 import LEAKAGE_CLASSES
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)

logger = logging.getLogger(__name__)

INTAKE_TICKET_RESULT_TYPE = "intake_ticket"
INTAKE_TICKET_RESULT_VERSION = 1

INTAKE_TICKET_TASK = "overlay.contract.intake_ticket"
INTAKE_TICKET_PROMPT_ID = "intake_ticket"
INTAKE_TICKET_PROMPT_VERSION = 5   # v2: + runner_up_refs (the Change-it menu)
#                                    v3: candidates carry semantic_terms + declared_type
#                                    v4: a typed column name rides in as a HINT, not an override
#                                    v5: the PREDICTION GOAL reaches the read (horizon lives there)
INTAKE_TICKET_SCHEMA_ID = "intake_ticket"
INTAKE_TICKET_RUN_ID = "intake-ticket"

_TARGET_TYPES = ("binary_classification", "regression", "multiclass", "abstain")

_INSTRUCTION = (
    "Read the analyst's objective and produce the intake ticket. `target_ref`: the ONE candidate "
    "ref that is the prediction target — copy it EXACTLY from the candidates list, or \"\" if no "
    "candidate is the target or you cannot tell. Never invent a ref. `target_window_days`: the "
    "label window in days when the text states one (\"churn = no activity in 90 days\" -> 90); 0 "
    "when not stated. `target_type`: what kind of prediction this is, or \"abstain\". "
    "`business_domain`: every vocabulary entry that matches the question's business area — copy "
    "tokens exactly from the vocabulary list; empty if none fit. `confidence`: \"high\" only when "
    "the text names its target unambiguously; \"abstain\" when you are guessing. "
    "`runner_up_refs`: up to three OTHER candidate refs that could also plausibly be the target, "
    "best first — copied EXACTLY from the candidates list, never the chosen target itself; [] "
    "when nothing else comes close.\n\n"
    "Each candidate carries `semantic_terms` — its glossary term, business subdomain path and "
    "synonyms — and `declared_type`, its column type. Use both: the subdomain path says which "
    "part of the bank a column belongs to, and a prediction target is normally a short flag or "
    "code (e.g. varchar(1)), not a long name or identifier. Neither field overrides what the "
    "summary says the column MEANS."
)

_WORD_RE = re.compile(r"[a-z0-9_]+")


def _instruction_for(pin: str | None) -> str:
    """The task instruction, plus — when the objective contained a candidate's exact name — the
    fact that it did.

    A HINT, not a directive. The match cannot separate a deliberate reference from an English word
    that happens to equal a column name, so the model is told what was SEEN and asked to weigh it,
    rather than having the answer decided for it by a string comparison.

    Deterministic against the cache key: ``pin`` is a pure function of the hypothesis and the
    shortlist, and both are hashed into the key, so the instruction can never vary behind a key
    that says it did not.
    """
    if pin is None:
        return _INSTRUCTION
    return (f"{_INSTRUCTION}\n\nNote: the objective contains the exact name of candidate "
            f"`{pin}`. That is often a deliberate reference to the target — but it can also be an "
            "ordinary word that happens to match a column name (\"customers who close their "
            "account\" against a column called `close`). Weigh it as evidence; it is not an "
            "instruction, and you may pick a different candidate or abstain.")


# ══ T7 (a) — the outcome family, DERIVED from what the registry already declares ═════════════════
#
# NO NEW TAXONOMY. Two behaviour fields the concept registry has carried since it was authored
# answer "is this column the label?", and both are already load-bearing elsewhere:
#
#   * ``Concept.leakage_anchor`` — its own comment reads "True for outcome_label + the
#     target-defining flags (§3.10/§3.7)", and ``templates._safe_to_bind`` refuses to build a
#     feature FROM any of them because "reading the target = leakage". Eight concepts today
#     (``outcome_label`` and its four children ``lapsed``/``surrendered``/``settlement_fail``/
#     ``redeemed``, plus ``delinquency_flag``/``default_flag``/``fraud_flag``). THAT set — the
#     columns the platform already treats as being the answer — is the OUTCOME family, and nothing
#     else is.
#   * ``Concept.near_label`` — "funnel-tail signals that BORDER the label". Thirteen concepts,
#     ``restriction_status`` among them, whose own description says these are "AML/fraud
#     CONSEQUENCES, so a financial-crime model trained on them reads its own answer back".
#
# The three class NAMES are ``recipe_contract_v2.LEAKAGE_CLASSES`` — the vocabulary the recipe
# contract already publishes for exactly this three-way split. Looked up by key rather than
# re-spelled, so dropping one there is a loud ImportError-time KeyError here, never silent drift.
_LEAKAGE_CLASS = {name: name for name in LEAKAGE_CLASSES}
OUTCOME_CLASS = _LEAKAGE_CLASS["outcome"]
NEAR_LABEL_CLASS = _LEAKAGE_CLASS["near_label"]
STANDARD_CLASS = _LEAKAGE_CLASS["standard"]

#: The bound on each candidate's ``semantic_terms``. The field carries the glossary term, the
#: business subdomain path and the synonyms — the discriminating part is the front of it, and the
#: tail is repeated synonym padding, so a head-truncation keeps the signal and drops the bulk.
_MAX_SEMANTIC_TERMS = 200

#: How many proxies an abstention hands back. The answer is a shortlist for a person to read, not
#: the catalog again. Applies to each list independently.
_CANDIDATE_LIMIT = 5


def target_leakage_class(concept_name: str | None) -> str | None:
    """Which :data:`LEAKAGE_CLASSES` member ``concept_name`` belongs to, or None.

    None means the column carries NO REGISTERED CONCEPT, and absence is not an assertion (the
    ``concepts.is_descriptive`` precedent, stated there in the same words). An unclassified column
    is therefore never called a proxy — but it is never committable either, because nothing
    certifies it as the label.
    """
    record = _concept_record(concept_name or "")
    if record is None:
        return None
    if record.leakage_anchor:
        return OUTCOME_CLASS
    if record.near_label:
        return NEAR_LABEL_CLASS
    return STANDARD_CLASS


def is_outcome_family(leakage_class: str | None) -> bool:
    """Does the registry CERTIFY this class as the label itself? The STRONGEST warrant, and the
    one the wording leans on. It is no longer the commit question on its own — see
    :func:`licenses_commit`, which admits ``near_label`` beside it. Everything else, including
    "nothing recorded", answers False."""
    return leakage_class == OUTCOME_CLASS


def asserts_label_adjacency(leakage_class: str | None) -> bool:
    """Does the registry POSITIVELY assert this class borders the label — i.e. is it a PROXY?

    Only ``near_label`` does. ``standard`` is the opposite claim (the registry looked and
    declassified the concept), and an unregistered concept makes no claim at all. Both of those are
    still uncommittable, and both are still gated at confirm — but for LACK OF CERTIFICATION, which
    is a different sentence from "this borders the answer". Conflating the two let the refusal tell
    338 of the registry's 359 concepts they were proxies for an outcome nobody measured them
    against.
    """
    return leakage_class == NEAR_LABEL_CLASS


def licenses_commit(leakage_class: str | None) -> bool:
    """Is this class ANSWER-SHAPED — i.e. may a proposal commit onto it? Outcome and near_label
    both, and nothing else.

    The two classes make the SAME structural claim. ``leakage_anchor`` reads "True for
    outcome_label + the target-defining flags" and ``templates._safe_to_bind`` refuses to build a
    feature from one because "reading the target = leakage"; ``near_label`` reads "funnel-tail
    signals that BORDER the label" and its concepts say a model trained on them "reads its own
    answer back". Both therefore say: this column is the answer, keep it out of the INPUTS — which
    is precisely the warrant for using it as the OUTPUT. The claims differ in STRENGTH, and
    strength belongs in what the screen SAYS (:func:`asserts_label_adjacency` still separates
    them), never in a veto.

    Gating the commit on ``outcome`` alone made the rule unsatisfiable on the deployed catalogs:
    ``cib`` and ``ftr`` carry 237 columns and ZERO leakage_anchor concepts between them, so every
    target on every hypothesis abstained and every confirmation met the same acknowledgment. All
    three real confirmations acknowledged and proceeded. A gate that fires on every request is not
    read, and an unread gate protects nobody — while the columns it was refusing
    (``cust_perf_nonperf_flg``, ``cust_susp_flg``) are the only genuine labels those catalogs hold.

    ``standard`` and an unregistered concept still do NOT license a commit, and the confirm gate
    still asks for the acknowledgment there — where it is now rare enough to be read.
    """
    return is_outcome_family(leakage_class) or asserts_label_adjacency(leakage_class)


@dataclass(frozen=True, slots=True)
class TargetCandidateV1:
    """One row of the abstention answer — used for BOTH lists, because both answer the same
    question about a different column: a ref, the concept it ACTUALLY carries, and what that
    concept makes it. ``concept`` is "" when the column carries none — the same honest absence
    ``leakage_class = None`` states."""

    ref: str
    concept: str
    leakage_class: str | None


# ══ T7 (b) — the stated horizon, extracted deterministically ═════════════════════════════════════
#
# CONSERVATIVE BY CONSTRUCTION: three literal patterns, digits only, and an extraction failure is
# NO CLAIM rather than a guess. "Churn = 90 days of inactivity" is a DEFINITION of the event and
# matches none of them; "in the next 90 days" is a horizon and matches the first.
_HORIZON_PATTERNS = (
    re.compile(r"\bnext\s+(\d{1,5})\s+(day|week|month)s?\b"),
    re.compile(r"\bwithin\s+(\d{1,5})\s+(day|week|month)s?\b"),
    re.compile(r"\b(\d{1,5})[-\s](day|week|month)\s+window\b"),
)

#: Units with an EXACT day count. A month has none — 28, 29, 30 and 31 are all months — so a month
#: horizon states that a horizon exists without stating a number this code may compare against.
#: Converting it would manufacture the precise false confidence this task exists to remove.
_EXACT_DAYS = {"day": 1, "week": 7}

#: How ``target_window_days`` got its value — or why it has none.
#: ``stated`` the value (or its absence) comes from the GOAL TEXT's own horizon — because the
#: model's reading agreed with it, or because there was no model reading to disagree (a degraded
#: ticket still reads the goal: :func:`stated_horizon` is pure code). A month horizon lands here
#: too, with no number, since it states a horizon this code may not count.
#: ``model_only`` the goal states no countable horizon and the number is the model's alone;
#: ``unstated`` nobody stated one, which is honest absence; ``contradicted`` the goal and a real
#: model reading disagree, so no window is accepted and
#: :attr:`IntakeTicketV1.window_refusal` says which numbers disagreed.
WINDOW_SOURCES = ("stated", "model_only", "unstated", "contradicted")

#: The one typed refusal code this seam raises.
WINDOW_CONTRADICTS_GOAL = "WINDOW_CONTRADICTS_GOAL"


@dataclass(frozen=True, slots=True)
class StatedHorizonV1:
    """A horizon the goal text states. ``days`` is None for a month horizon — stated, not
    countable — and ``text`` is always the objective's own words, for the refusal to quote."""

    text: str
    days: int | None


@dataclass(frozen=True, slots=True)
class WindowRefusalV1:
    """The typed refusal: both numbers, named. ``ticket_days`` is the model's RAW answer, so a
    reading of 0 against a stated 90 days says "0", not "None"."""

    code: str
    stated_text: str
    stated_days: int | None
    ticket_days: int
    detail: str


def stated_horizon(goal: str) -> StatedHorizonV1 | None:
    """The horizon the goal text states, or None when it states none — or states two.

    Two different horizons in one objective is an ambiguity, not a horizon: this returns None and
    the ticket makes no claim, exactly as it does when nothing matched at all.
    """
    found: set[tuple[int, str]] = set()
    lowered = goal.lower()
    for pattern in _HORIZON_PATTERNS:
        for count, unit in pattern.findall(lowered):
            found.add((int(count), unit))
    if len(found) != 1:
        return None
    ((count, unit),) = found
    per_day = _EXACT_DAYS.get(unit)
    return StatedHorizonV1(f"{count} {unit if count == 1 else unit + 's'}",
                           None if per_day is None else count * per_day)


def _resolve_window(raw: object, goal: str) -> tuple[int | None, str, WindowRefusalV1 | None]:
    """Cross-check the model's window against the goal's stated horizon.

    Nothing cross-checked these two on the 2026-08-24 AML run: the objective said "in the next 90
    days", the ticket said 0, and the near-label critic downstream then abstained on every
    candidate without anyone learning why. The four outcomes are :data:`WINDOW_SOURCES`.
    """
    model_days = raw if isinstance(raw, int) and raw >= 0 else None
    accepted = model_days if (model_days or 0) > 0 else None
    horizon = stated_horizon(goal)
    if horizon is None:
        return accepted, ("model_only" if accepted is not None else "unstated"), None
    if accepted is not None:
        if horizon.days is None:
            # A month horizon and a number: a horizon IS stated, but no exact day count exists to
            # compare it with. Neither confirmed nor contradicted — the number stands on the model.
            return accepted, "model_only", None
        if accepted == horizon.days:
            return accepted, "stated", None
    # Either the two numbers disagree, or the objective states a horizon and the reading carries
    # none at all — the run's own 0-against-90. The second arm needs no day count, so a MONTH
    # horizon catches it too, quoting the objective's words instead of an invented number.
    ticket_days = model_days if model_days is not None else 0
    return None, "contradicted", WindowRefusalV1(
        code=WINDOW_CONTRADICTS_GOAL, stated_text=horizon.text, stated_days=horizon.days,
        ticket_days=ticket_days,
        detail=(f"the objective states a horizon of {horizon.text}; the intake reading returned "
                f"target_window_days={ticket_days}. The two disagree, so no label window is "
                f"accepted — state the horizon on the confirm screen."))


@dataclass(frozen=True, slots=True)
class IntakeTicketV1:
    """The structured reading of one hypothesis. `target_column` is a validated graph ref or None;
    `pinned` means code matched a literally-typed name (the model could not have overridden it);
    `contradiction` carries the confirm screen's warning when the model's prose reading disagreed
    with a pinned name. Every field the model could not honestly fill is None/abstain/() — absence
    is the signal, never a guess."""

    target_column: str | None
    target_window_days: int | None
    target_type: str                       # one of _TARGET_TYPES
    business_domain: tuple[str, ...]       # ⊆ the recipe use-case/family vocabulary
    confidence: str                        # "high" | "medium" | "abstain"
    pinned: bool
    contradiction: str | None
    # The Change-it menu (prompt v2): the model's ranked next-best readings, ⊆ the shortlist and
    # never the chosen target — one-click corrections on the confirm screen. () on v1 replays,
    # degraded tickets, and honest nothing-else-comes-close answers alike.
    runners_up: tuple[str, ...] = ()

    # ── T7 (a): outcome or proxy, said out loud ──────────────────────────────────────────────────
    #: The concept ``target_column`` ACTUALLY carries — "" when it carries none, or when there is
    #: no target. Never the concept the summary prose implied.
    target_concept: str = ""
    #: ``target_concept``'s :data:`LEAKAGE_CLASSES` member; None when unregistered (nothing said).
    target_leakage_class: str | None = None
    #: True only where the registry ASSERTS label-adjacency (:func:`asserts_label_adjacency`) —
    #: never merely "not certified as the label". A ``standard`` target is uncommittable and still
    #: gated at confirm, but the registry declassified it, so calling it a proxy for the outcome
    #: would be a correlation claim nobody made; an unregistered one makes no claim in either
    #: direction. Both answer False here and both still require the acknowledgment.
    target_is_proxy: bool = False
    #: The abstention answer as DATA: the nearest proxies, ranked, each labelled with its real
    #: concept. Populated whenever the target is not outcome-family (including when there is no
    #: target at all); () when the target IS the label, because there is nothing to fall back to.
    proxy_candidates: tuple[TargetCandidateV1, ...] = ()
    #: Every outcome-family column the CATALOG holds, ref-sorted — the other half of an honest
    #: abstention. Abstaining while the label sits in the same table and goes unmentioned is its
    #: own silence. This REPORTS what exists; it never substitutes a target (see
    #: :func:`_proxy_candidates`). () when the target already IS the label.
    outcome_candidates: tuple[TargetCandidateV1, ...] = ()

    # ── T7 (b): a window, or a stated absence, or a named disagreement ───────────────────────────
    #: One of :data:`WINDOW_SOURCES`.
    window_source: str = "unstated"
    #: Present only when ``window_source == "contradicted"``; ``target_window_days`` is then None.
    window_refusal: WindowRefusalV1 | None = None


def _proxy_candidates(target: str | None, runners: Sequence[str],
                      concepts_by_ref: dict[str, str]) -> tuple[TargetCandidateV1, ...]:
    """The ranked proxies behind an abstention.

    TWO SOURCES, deliberately. The model's own ranking (target, then runners-up) is the only
    relevance judgment anyone made, so it leads. But the list ALSO sweeps in every near-label
    column the REGISTRY recognises in this catalog, whether or not the model ranked it — the
    registry's warrant that a column borders the label does not depend on the model having noticed
    it, and on the run that motivated this the model ranked exactly one of the two.

    Order: near-label first (the registry says they BORDER the label, so they are the nearest
    honest thing), then the rest; the sort is stable, so within each class the model's ranking
    survives and the registry sweep follows it in ref order. Outcome-family columns are excluded
    on purpose — a label is not a proxy for itself; they ride
    :attr:`IntakeTicketV1.outcome_candidates` instead.

    NO SUBSTITUTION. Neither list ever changes ``target_column``; the module's first discipline is
    SELECTION, never generation, and code choosing a target the model did not pick would break it.
    Reporting what the catalog contains is not choosing.
    """
    ordered = [ref for ref in (target, *runners) if ref]
    ordered += sorted(ref for ref, name in concepts_by_ref.items()
                      if asserts_label_adjacency(target_leakage_class(name)))
    ranked: list[TargetCandidateV1] = []
    seen: set[str] = set()
    for ref in ordered:
        if ref in seen or ref not in concepts_by_ref:
            continue
        name = concepts_by_ref[ref]
        klass = target_leakage_class(name)
        if is_outcome_family(klass):
            continue
        seen.add(ref)
        ranked.append(TargetCandidateV1(ref=ref, concept=name, leakage_class=klass))
    ranked.sort(key=lambda c: 0 if asserts_label_adjacency(c.leakage_class) else 1)
    return tuple(ranked[:_CANDIDATE_LIMIT])


def _outcome_candidates(concepts_by_ref: dict[str, str]) -> tuple[TargetCandidateV1, ...]:
    """Every outcome-family column this catalog actually holds, ref-sorted.

    Catalog-derived, not model-derived: the point is exactly to name a label the model did NOT
    pick. Ref order because there is no relevance judgment to preserve here — the registry
    certified each of them equally.
    """
    return tuple(
        TargetCandidateV1(ref=ref, concept=name, leakage_class=OUTCOME_CLASS)
        for ref, name in sorted(concepts_by_ref.items())
        if is_outcome_family(target_leakage_class(name))
    )[:_CANDIDATE_LIMIT]


def _use_case_vocabulary() -> tuple[str, ...]:
    """The closed relevance vocabulary: every `use_cases` token ∪ every `family` across the recipe
    registry. Deferred import — the registry is heavy and this module must import light."""
    from featuregen.overlay.upload.templates import ALL_TEMPLATES

    vocab: set[str] = set()
    for template in ALL_TEMPLATES:
        vocab.update(template.use_cases)
        vocab.add(template.family)
    return tuple(sorted(vocab))


def _shortlist(conn, catalog_source: str | None, roles: Iterable[str]) -> list[dict]:
    """The shelf photo per READ-SCOPED column: ref + concept + one-line summary + the glossary
    ``semantic_terms`` + the ``declared_type``. At today's catalog sizes the shortlist is the whole
    catalog; at scale a search-derived subset takes its place (stage 3 of the spec) — same shape,
    fewer rows.

    THE LAST TWO ARE SIGNAL, NOT VOLUME, and were chosen by measuring the live catalog rather than
    by adding everything available:

    * ``semantic_terms`` (237/237 columns populated) carries the glossary term, the business
      SUBDOMAIN PATH and the synonyms. The path is what separates `cust_susp_flg`
      ("Risk and Compliance - Regulations and Compliance") from `cust_status_flg`
      ("Party Lifecycle Management") — the AML-vs-churn distinction the picker is asked to make,
      and the one thing it was never shown. The synonyms also let a person's plain English
      ("suspended", "dormant") match a column named `cust_susp_flg`.
    * ``declared_type`` (235/237) is close to a label detector on its own: labels are
      ``varchar(1)`` or a short code, identifiers and names are long. The feature-building path
      next door already reads this field from this table; only the picker was missing it.

    ``definition`` and ``domain`` were measured and DELIBERATELY LEFT OUT. On the live catalog
    `definition` returns the same templated sentence for different columns ("Status or indicator
    used to classify customer condition, eligibility, servicing state...") — it would blur two
    columns the picker exists to separate — and `domain` is the constant "Customer" across all of
    `cib`, so it discriminates nothing. More context is not the goal; more SIGNAL is.
    """
    from featuregen.intake.redaction import redact_free_text as _scan
    from featuregen.overlay.upload.read_scope import allowed_sensitivities

    rows = conn.execute(
        "SELECT catalog_source, object_ref, concept, ai_summary, semantic_terms, declared_type "
        "FROM graph_node "
        "WHERE kind = 'column' AND (%(src)s::text IS NULL OR catalog_source = %(src)s) "
        "  AND visible_requires <@ %(allowed)s ORDER BY catalog_source, object_ref",
        {"src": catalog_source, "allowed": allowed_sensitivities(roles)}).fetchall()

    def _prose(text: str | None, *, limit: int | None = None) -> str:
        # The `candidates` egress class makes the CALLER the owning scanner (tie_break's
        # precedent): summaries are prose and get the PII scan; a fail-closed None blanks the
        # field rather than the payload. Runs BEFORE hashing, so the key sees what egresses.
        # `semantic_terms` is glossary prose from the same enrichment, so it takes the SAME scan —
        # then the bound, so truncation can never split a span the scanner already cleared.
        if not text:
            return ""
        scanned = _scan(text).text or ""
        return scanned if limit is None else scanned[:limit]

    return [{"ref": r[1], "concept": r[2] or "", "ai_summary": _prose(r[3]),
             "semantic_terms": _prose(r[4], limit=_MAX_SEMANTIC_TERMS),
             # structural, not prose: a type name egresses as-is, like the concept beside it.
             "declared_type": r[5] or ""}
            for r in rows]


def _exact_pin(hypothesis: str, shortlist: Sequence[dict]) -> str | None:
    """Stage 1, pure code: a literally-typed column name pins the target. Case/underscore-normalised
    word match; a name colliding across catalogs does NOT pin (the spec's collision rule — a pin
    that guesses between catalogs is not a pin)."""
    words = set(_WORD_RE.findall(hypothesis.lower()))
    matches: dict[str, set[str]] = {}
    for entry in shortlist:
        column_name = entry["ref"].rsplit(".", 1)[-1].lower()
        if column_name in words:
            matches.setdefault(column_name, set()).add(entry["ref"])
    pinned = {name: refs for name, refs in matches.items() if len(refs) == 1}
    if len(pinned) != 1:
        return None            # nothing typed, ambiguous across catalogs, or several names typed
    (refs,) = pinned.values()
    (ref,) = refs
    return ref


def _input_hash(*, hypothesis: str, objective: str, shortlist: Sequence[dict],
                vocabulary: Sequence[str]) -> str:
    """ALL FIVE inputs — a vocabulary rename, a column re-enrichment or a DIFFERENT GOAL must
    re-ask, not serve a stale ticket. The goal joined the inputs and had to join the key in the
    same change: an input outside the key is a wrong answer served from cache, which is the exact
    defect the second review caught when the key hashed one input of four."""
    return canonical_hash({
        "version": "intake-ticket-input-v1",
        "prompt_id": INTAKE_TICKET_PROMPT_ID,
        "prompt_version": INTAKE_TICKET_PROMPT_VERSION,
        "hypothesis": hypothesis,
        "objective": objective,
        "shortlist": list(shortlist),
        "vocabulary": list(vocabulary),
    })


def _degraded(pin: str | None, concepts_by_ref: dict[str, str] | None = None, *,
              goal: str = "") -> IntakeTicketV1:
    """The no-client / fault / ceiling ticket.

    EVERYTHING PURE CODE CAN STILL ANSWER, IT ANSWERS. Two reads never needed the provider: the
    registry lookup that decides outcome-vs-proxy, and :func:`stated_horizon` over the goal text.
    A degraded ticket that reported ``window_source: "unstated"`` against an objective plainly
    saying "in the next 90 days" was making the same kind of false claim this task exists to
    remove — in the opposite direction.

    And never a REFUSAL: a contradiction needs two readings, and here the model produced none. The
    goal's horizon simply stands, uncontested — with no number when the unit is months, per
    :data:`_EXACT_DAYS`.
    """
    concepts_by_ref = concepts_by_ref or {}
    name = concepts_by_ref.get(pin or "", "")
    klass = target_leakage_class(name)
    committable = licenses_commit(klass)
    horizon = stated_horizon(goal)
    window = horizon.days if horizon is not None else None
    window_source = "stated" if horizon is not None else "unstated"
    return IntakeTicketV1(target_column=pin, target_window_days=window, target_type="abstain",
                          business_domain=(), confidence="abstain", pinned=pin is not None,
                          contradiction=None, runners_up=(),
                          target_concept=name, target_leakage_class=klass,
                          target_is_proxy=pin is not None and asserts_label_adjacency(klass),
                          proxy_candidates=(() if committable else
                                            _proxy_candidates(pin, (), concepts_by_ref)),
                          outcome_candidates=(() if committable else
                                              _outcome_candidates(concepts_by_ref)),
                          window_source=window_source)


def _ticket_from_output(output: dict, *, pin: str | None, goal: str,
                        concepts_by_ref: dict[str, str], vocabulary: set[str]) -> IntakeTicketV1:
    """Validate the model's reading into a ticket. Everything is checked against a closed set: an
    off-shortlist target is ABSTAIN (never trusted — the veto must not guard an empty room);
    off-vocabulary domains are dropped; a pinned name always wins, with a disagreement kept as the
    confirm screen's contradiction warning.

    T7 adds two checks the 2026-08-24 AML run had neither of: the target's concept decides whether
    a COMMIT is even available (:func:`target_leakage_class`), and the model's window is
    cross-checked against the goal text's own stated horizon (:func:`_resolve_window`).
    """
    shortlist_refs = set(concepts_by_ref)
    raw_target = output.get("target_ref")
    model_target = raw_target if (isinstance(raw_target, str)
                                  and raw_target in shortlist_refs) else None
    invented = bool(raw_target) and model_target is None
    contradiction = None
    # THE MODEL DECIDES; THE PIN IS EVIDENCE. Matching a word in a sentence against a column name
    # cannot separate a deliberate reference from a coincidence — "customers will close their
    # account" is indistinguishable from naming a column called `close`. That weak signal used to
    # beat the model outright. The model holds the same sentence, the same shortlist and (v3) the
    # glossary terms and types, so it is the better reader; the pin now rides in as a hint and
    # survives as the FALLBACK when the model chose nothing (and, via `_degraded`, when there is no
    # model at all — the case the exact-name match was really built for).
    target = model_target if model_target is not None else pin
    if pin is not None and model_target is not None and model_target != pin:
        # Two readings differing is INFORMATION, so it is still shown — the wording is unchanged,
        # and it now describes a disagreement the model won rather than one it lost.
        contradiction = (f"you named {pin.rsplit('.', 1)[-1]}; the description reads as "
                        f"{model_target.rsplit('.', 1)[-1]}")
    window, window_source, window_refusal = _resolve_window(
        output.get("target_window_days"), goal)
    target_type = output.get("target_type")
    target_type = target_type if target_type in _TARGET_TYPES else "abstain"
    domains = output.get("business_domain")
    domains = tuple(d for d in domains if d in vocabulary) if isinstance(domains, list) else ()
    confidence = output.get("confidence")
    if invented or model_target is None:
        # No reading of its own -> no band of its own, pin or not: a target carried in by the name
        # match is SHOWN, never asserted.
        confidence = "abstain"
    elif confidence not in ("high", "medium", "abstain"):
        confidence = "abstain"
    # Runners-up: SELECTION discipline again — ⊆ the shortlist, never the target, order kept,
    # capped on read (the schema cannot carry maxItems). Absent on v1 replays -> ().
    raw_runners = output.get("runner_up_refs")
    runners = tuple(dict.fromkeys(
        r for r in raw_runners
        if isinstance(r, str) and r in shortlist_refs and r != target
    ))[:3] if isinstance(raw_runners, list) else ()
    # ABSTAIN-BY-DEFAULT. A proposal may COMMIT only onto an ANSWER-SHAPED concept (outcome or
    # near_label — see `licenses_commit`); everything else abstains and hands back the ranked
    # proxies, and the labels the catalog does hold.
    #
    # The PIN no longer exempts anything. The old exemption reasoned that a literally-typed name is
    # not the platform proposing, so there is nothing to be unconfident about — sound where the
    # typing was deliberate, which a bare word match cannot establish. The class check now applies
    # to every target the same way, typed or inferred.
    concept_name = concepts_by_ref.get(target or "", "")
    klass = target_leakage_class(concept_name)
    committable = licenses_commit(klass)
    if not committable:
        confidence = "abstain"
    ticket = IntakeTicketV1(
        target_column=target, target_window_days=window,
        target_type=target_type, business_domain=domains,
        confidence=confidence, pinned=pin is not None,
        contradiction=contradiction, runners_up=runners,
        target_concept=concept_name, target_leakage_class=klass,
        target_is_proxy=target is not None and asserts_label_adjacency(klass),
        proxy_candidates=(() if committable else
                          _proxy_candidates(target, runners, concepts_by_ref)),
        outcome_candidates=() if committable else _outcome_candidates(concepts_by_ref),
        window_source=window_source, window_refusal=window_refusal)
    # The closed vocabulary, actually closed. It was published and validated by nothing, which is
    # how a token like "unstated" reaches a consumer that has never heard of it.
    assert ticket.window_source in WINDOW_SOURCES, ticket.window_source
    return ticket


_PROVENANCES = ("human_confirmed", "user_typed", "exploring")


def record_target_reading(conn, *, intent_id: str, provenance: str, target_ref: str | None = None,
                          target_window_days: int | None = None, target_type: str | None = None,
                          business_domain: Sequence[str] = (),
                          confirmed_by: str | None = None) -> bool:
    """Persist the SIGNED reading onto the intent it governs (migration 1059 — the storage
    decision's human half; the model's draft stays in ``structured_result``). Provenance is closed:
    ``human_confirmed`` (fuzzy path, a person clicked), ``user_typed`` (the person literally named
    the column — human-origin by construction, recorded without a click), ``exploring`` (an explicit
    no-target declaration; ``target_ref`` is forced NULL so the veto downstream honestly has nothing
    to guard and near-label withholding can say why). Returns False when the intent does not exist —
    the caller owns the 404."""
    if provenance not in _PROVENANCES:
        raise ValueError(f"unknown target provenance: {provenance!r}")
    if provenance == "exploring":
        target_ref = None
    row = conn.execute(
        "UPDATE contract_intent SET target_ref = %s, target_window_days = %s, target_type = %s, "
        "business_domain = %s::jsonb, target_provenance = %s, target_confirmed_by = %s, "
        "target_confirmed_at = now() WHERE intent_id = %s RETURNING intent_id",
        (target_ref, target_window_days, target_type,
         json.dumps(sorted(business_domain)), provenance, confirmed_by, intent_id)).fetchone()
    return row is not None


def target_reading(conn, intent_id: str) -> dict | None:
    """The recorded reading, for consumers beyond the leakage gate (which keeps its own
    ``intent_target_ref``): the near-label critic reads the window, the menu ordering reads the
    domain. None when the intent is unknown; NULL fields mean "not declared", never a default."""
    row = conn.execute(
        "SELECT target_ref, target_window_days, target_type, business_domain, target_provenance, "
        "target_confirmed_by FROM contract_intent WHERE intent_id = %s", (intent_id,)).fetchone()
    if row is None:
        return None
    return {"target_ref": row[0], "target_window_days": row[1], "target_type": row[2],
            "business_domain": tuple(row[3] or ()), "target_provenance": row[4],
            "target_confirmed_by": row[5]}


def signed_reading_for(conn, *, hypothesis: str, actor_json: str) -> dict | None:
    """The SIGNED reading for this (hypothesis, actor) — the consumers' join point: the near-label
    critic reads ``target_window_days``, the use-case ordering reads ``business_domain``. Looked up
    by the QUESTION and its AUTHOR, deliberately mode-blind: the intake route always mints
    hypothesis-mode intents, while a definition-carrying considered-set request runs in
    ``definition`` mode — same hypothesis, same person, same signed reading (a mode-filtered
    lookup silently lost the signature on exactly that path; review fix 2026-08-10). NOT by
    intent_id: the legacy considered-set path mints a fresh intent per call, while the signed
    reading lives on the earliest deduped row. Only a reading a HUMAN stood behind counts
    (provenance recorded); None = not declared — every consumer must degrade (abstain / today's
    order) on it, never guess."""
    row = conn.execute(
        "SELECT target_ref, target_window_days, target_type, business_domain, target_provenance "
        "FROM contract_intent "
        "WHERE hypothesis = %s AND actor = %s::jsonb "
        "AND target_provenance IS NOT NULL ORDER BY created_at ASC LIMIT 1",
        (hypothesis, actor_json)).fetchone()
    if row is None:
        return None
    return {"target_ref": row[0], "target_window_days": row[1], "target_type": row[2],
            "business_domain": tuple(row[3] or ()), "target_provenance": row[4]}


def signed_label_window(conn, *, hypothesis: str, actor_json: str) -> int | None:
    """The near-label critic's one data input — a thin view over :func:`signed_reading_for`."""
    reading = signed_reading_for(conn, hypothesis=hypothesis, actor_json=actor_json)
    return reading["target_window_days"] if reading else None


def is_readable_column(conn, ref: str, *, roles: Iterable[str],
                       catalog_source: str | None = None) -> bool:
    """Membership check for a human-supplied target: a READ-SCOPED column node with this ref exists.
    The confirm route validates against the catalog, not against the ticket — the human may correct
    to any real column, and a column the confirmer cannot see cannot be their target."""
    from featuregen.overlay.upload.read_scope import allowed_sensitivities

    return conn.execute(
        "SELECT 1 FROM graph_node WHERE kind = 'column' AND object_ref = %(ref)s "
        "AND (%(src)s::text IS NULL OR catalog_source = %(src)s) "
        "AND visible_requires <@ %(allowed)s LIMIT 1",
        {"ref": ref, "src": catalog_source,
         "allowed": allowed_sensitivities(roles)}).fetchone() is not None


def extract_intake_ticket(conn, client, *, hypothesis: str, objective: str = "",
                          catalog_source: str | None = None,
                          roles: Iterable[str] = (), actor=None,
                          call_ledger=None) -> tuple[IntakeTicketV1, str]:
    """The mandatory read. Returns ``(ticket, reason)`` with closed reasons: ``replayed`` (cached),
    ``extracted`` (fresh, stored), ``unavailable`` (no client / fault — degraded ticket, pinned
    target survives), ``call_ceiling``. The model output is STORED verbatim (including honest
    abstains); validation runs on every read as well as on first extraction, so a shortlist change
    re-validates a replayed ticket too — and since T7's outcome/proxy labelling and window
    cross-check are both part of that validation, a ticket recorded BEFORE this rule existed is
    re-judged under it on its next read, with no re-dispatch and no stored-output rewrite."""
    # THE HORIZON LIVES IN THE GOAL. The screen collects a "Prediction goal" beside the hypothesis
    # and that is where people write "in the next 90 days" — but only the hypothesis reached this
    # read, so the horizon was invisible, the window came back `unstated`, and the near-label
    # critic (which needs a signed window) abstained on every candidate. Both texts are scanned for
    # the horizon: `stated_horizon` returns None on TWO DIFFERENT numbers, so a hypothesis and a
    # goal that disagree stay an honest absence rather than a coin toss.
    goal_text = "\n\n".join(t for t in (hypothesis, objective) if t.strip())
    shortlist = _shortlist(conn, catalog_source, roles)
    # The concept each candidate ACTUALLY carries, by ref — the outcome/proxy answer's whole
    # input, and already on the shelf photo the model was shown.
    concepts_by_ref = {e["ref"]: e.get("concept") or "" for e in shortlist}
    vocabulary = _use_case_vocabulary()
    pin = _exact_pin(hypothesis, shortlist)
    key = _input_hash(hypothesis=hypothesis, objective=objective, shortlist=shortlist,
                      vocabulary=vocabulary)

    stored = find_structured_result(
        conn, result_type=INTAKE_TICKET_RESULT_TYPE,
        result_version=INTAKE_TICKET_RESULT_VERSION, input_content_hash=key)
    if stored is not None:
        return _ticket_from_output(dict(stored.output), pin=pin, goal=goal_text,
                                   concepts_by_ref=concepts_by_ref,
                                   vocabulary=set(vocabulary)), "replayed"
    if client is None:
        return _degraded(pin, concepts_by_ref, goal=goal_text), "unavailable"
    if call_ledger is not None and not call_ledger.charge():
        return _degraded(pin, concepts_by_ref, goal=goal_text), "call_ceiling"

    from featuregen.overlay.upload.contract.intake import redact_free_text
    from featuregen.overlay.upload.enrich_llm import drive_audited_structured_call

    # Both free-text fields are redacted with the SAME discipline before either can egress; the
    # goal rides the existing `objective` key rather than a new one, so no egress class changes.
    redacted = redact_free_text(hypothesis, label="hypothesis")
    if objective.strip():
        redacted = f"{redacted}\n\nPREDICTION GOAL: {redact_free_text(objective, label='objective')}"
    try:
        call = drive_audited_structured_call(
            conn, client, task=INTAKE_TICKET_TASK,
            prompt_id=f"{INTAKE_TICKET_PROMPT_ID}_v{INTAKE_TICKET_PROMPT_VERSION}",
            schema_id=INTAKE_TICKET_SCHEMA_ID,
            # Every key pre-classified on the egress seam: `objective` is the round-trip prose
            # class the generation call's own hypothesis rides; `candidates` is the
            # structural-with-owned-scanning class (summaries are already-graded enrichment
            # output); `vocabulary` is the classifier's own closed-list class.
            catalog_metadata={"objective": redacted, "candidates": shortlist,
                              "vocabulary": list(vocabulary)},
            instruction=_instruction_for(pin), actor=actor, run_id=INTAKE_TICKET_RUN_ID,
            record_egress_block=True)
    except Exception:  # noqa: BLE001 — mandatory to ATTEMPT, never load-bearing
        logger.warning("intake-ticket extraction failed; degrading to the pinned/abstain ticket",
                       exc_info=True)
        return _degraded(pin, concepts_by_ref, goal=goal_text), "unavailable"
    if call.output is None:
        return _degraded(pin, concepts_by_ref, goal=goal_text), "unavailable"
    ticket = _ticket_from_output(dict(call.output), pin=pin, goal=goal_text,
                                 concepts_by_ref=concepts_by_ref, vocabulary=set(vocabulary))
    record_structured_result(
        conn, result_type=INTAKE_TICKET_RESULT_TYPE,
        result_version=INTAKE_TICKET_RESULT_VERSION, input_content_hash=key,
        output=dict(call.output), producer_kind="llm_call",
        producer_ref=call.llm_call_ref or "intake_ticket:unrecorded")
    return ticket, "extracted"
