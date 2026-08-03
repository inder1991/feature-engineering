"""Turn an abstention into a question worth asking — and an answer back into a plan.

:mod:`featuregen.analysis.intent` deliberately leaves a field EMPTY when the question does not
determine it, and records a code in ``unresolved``. That is only useful if something turns the code
into a question a person can actually answer, and the answer back into a plan. Without this half, an
honest abstention and a broken extraction look identical to the caller: an empty field either way.

**Deterministic, not generated.** The roadmap lists clarification wording among the places an LLM
adds value, and it may later — but the OPTIONS must not be generated, and the wording does not need
to be. A model asked "which column identifies the customer?" can invent a plausible column, and the
user would have no way to know. Here the options come from the same bounded candidate set intent was
given, so a clarification cannot introduce a ref that was never offered. Wording is a template.

**An answer is validated exactly as the model's output was.** :func:`apply_answer` re-checks every
chosen ref against the candidates. A human answering a clarification is a caller like any other —
trusting a UI to have offered only legitimate options would put the guarantee in the layer least able
to keep it.

**One question per unresolved code, and only for codes that were raised.** Asking about something the
model resolved confidently would invite a user to second-guess a good answer, and asking everything
at once is how a clarification step becomes a form nobody fills in.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from featuregen.analysis.intent import (
    COMPARISONS,
    UNRESOLVED_CODES,
    IntentCandidates,
    IntentExtraction,
)
from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import (
    SELECTION_AUTHORITY_INSUFFICIENT,
    SELECTION_BINDING_MISSING,
    SELECTION_POPULATION_UNDECLARED,
    SELECTION_REFUSAL_CODES,
    SELECTION_SOURCE_AMBIGUOUS,
    TEMPORAL_HISTORICAL_CURRENT_ONLY,
    TEMPORAL_MODEL_UNKNOWN,
    TEMPORAL_SCD_OVERLAP,
    TEMPORAL_SNAPSHOT_TIE,
    CandidateDisposition,
)

if TYPE_CHECKING:                       # import weight — the `source_selection` precedent
    from featuregen.overlay.upload.source_selector import SelectionRefusalV1


class ClarificationError(ValueError):
    """An answer that cannot be applied. Carries the code so a caller can re-ask precisely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Option:
    """One thing the user may choose. ``value`` is a catalog ref or a vocabulary token — never free
    text, because the answer is applied to a typed plan."""

    value: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class Clarification:
    code: str
    question: str
    options: tuple[Option, ...]
    allows_multiple: bool = False
    #: True when the question can be skipped — the plan is still expressible without an answer.
    #: Dimensions are optional; an entity is not.
    optional: bool = False


def _options(refs: frozenset[str], candidates: IntentCandidates) -> tuple[Option, ...]:
    return tuple(Option(value=ref, label=candidates.labels.get(ref, ""))
                 for ref in sorted(refs))


def clarifications_for(extraction: IntentExtraction,
                       candidates: IntentCandidates) -> tuple[Clarification, ...]:
    """One question per raised abstention, in the order they must be answered.

    Ordering is not cosmetic: the entity decides which table the rest of the question is about, so it
    is asked first. A user who picks dimensions before an entity can be asked to pick again.

    The MODEL's half only. ``/analysis/plan`` calls :func:`clarifications_for_codes` directly
    because it has a second source of questions — the selector's refusals — and merging them into
    ONE sorted call is what keeps the population outranking every row question. This stays the
    entry point for a caller holding an extraction and nothing else.
    """
    return clarifications_for_codes(extraction.unresolved, candidates)


def clarifications_for_codes(codes: tuple[str, ...] | frozenset[str],
                             candidates: IntentCandidates,
                             *, refusals: Iterable[SelectionRefusalV1] = ()
                             ) -> tuple[Clarification, ...]:
    """The SAME rendering, driven by codes rather than by a model extraction (Release-B Task 8).

    A selection refusal is raised by the SELECTOR, not by the model, so it never appears in an
    ``IntentExtraction`` — and the wire schema's enum deliberately cannot carry it (the D5 wire-enum
    split: a model must not be able to assert an overlap or a binding refusal). Routing it through
    this function rather than through a second renderer is what keeps ONE set of question wordings
    and ONE total order: the population still outranks every row question, whichever half of the
    system noticed the gap.

    ``refusals`` are the REFUSAL PAYLOADS behind those codes, when the caller has them. One closed
    code can describe two situations a person must be told apart — ``SELECTION_SOURCE_AMBIGUOUS``
    covers both "several datasets are equally eligible" and "none is eligible at all" — and the
    payload's own candidate dispositions are what say which. Optional, because the vocabulary is
    the contract: a caller with only codes still gets a question, and it is the one the code's
    common case describes.
    """
    raised = {code for code in codes if code in UNRESOLVED_CODES}
    # First refusal per code: two refusals sharing a code are two instances of one question here,
    # and the question is about the code, not about one subject.
    by_code: dict[str, SelectionRefusalV1] = {}
    for refusal in refusals:
        by_code.setdefault(refusal.code, refusal)
    return tuple(_build(code, candidates, refusal=by_code.get(code))
                 for code in sorted(raised, key=_clarification_rank))


# Asked in the order they must be answered. The Release-B SELECTION refusals sit at the FRONT,
# ahead of every intent question — which dataset serves the need, and which of its rows, decide what
# the rest of the question can even mean — with ONE exception, and it is the reason these ranks were
# renumbered: the POPULATION outranks even them. Which table the answer is per decides what every
# later answer is about, and asking which column breaks a snapshot tie before that is settled invites
# a user to refine an answer that is about to change underneath them.
#
# EVERY RANK IS DISTINCT, and that is load-bearing rather than tidy. `clarifications_for` sorts a
# SET, whose iteration order for strings is decided by PYTHONHASHSEED — so any two codes sharing a
# rank swap places between processes, and the question order is not reproducible for one user twice.
# Three codes used to sit at rank 0, `population` among them, which is exactly the pair the doctrine
# above says must never invert. `_clarification_rank` carries the code NAME as a second key so a
# rank added later without reading this comment still cannot reintroduce the coin flip.
_ORDER = {
    SELECTION_POPULATION_UNDECLARED: -6,
    SELECTION_SOURCE_AMBIGUOUS: -5,
    SELECTION_AUTHORITY_INSUFFICIENT: -4,
    SELECTION_BINDING_MISSING: -3,
    TEMPORAL_MODEL_UNKNOWN: -2,
    TEMPORAL_HISTORICAL_CURRENT_ONLY: -1,
    "population": 0,
    # Both are ROW questions about a source the population has already settled, so they follow it —
    # and they stay ahead of the remaining intent questions, which are about the shape of the answer.
    TEMPORAL_SNAPSHOT_TIE: 1,
    TEMPORAL_SCD_OVERLAP: 2,
    "entity": 3, "measure": 4, "windows": 5, "comparison": 6,
    "dimensions": 7,
}


def _clarification_rank(code: str) -> tuple[int, str]:
    """The sort key for one question: declared rank first, then the code name.

    Total by construction. An unranked code sorts last (99) and, among unranked codes, alphabetically
    — never by hash order."""
    return (_ORDER.get(code, 99), code)


def _build(code: str, candidates: IntentCandidates,
           refusal: SelectionRefusalV1 | None = None) -> Clarification:
    if code in SELECTION_REFUSAL_CODES:
        return _selection_clarification(code, candidates, refusal)
    if code == "population":
        # Asked FIRST and never optional. `spine.py`: the declaration chooses the source, governed
        # facts may only validate it — because look-alike population tables are indistinguishable to
        # the catalog and a wrong choice silently shrinks the answer. Offering the human a list is
        # not the system inferring; the options are governed identifiers from the already-bounded
        # retrieval set, and picking one declares BOTH the table and its key.
        return Clarification(
            code=code,
            question="Which table holds the population this question is about — every member, "
                     "including those with no activity in either period?",
            options=_options(candidates.grain_refs or candidates.column_refs, candidates))
    if code == "entity":
        # Identifiers only. Offering all sixty columns turns a decidable question into a search.
        refs = candidates.grain_refs or candidates.column_refs
        return Clarification(
            code=code, question="Which column identifies the thing this question is about?",
            options=_options(refs, candidates))
    if code == "windows":
        refs = candidates.as_of_refs or candidates.column_refs
        return Clarification(
            code=code, question="Which column should the time periods be measured on?",
            options=_options(refs, candidates))
    if code == "dimensions":
        return Clarification(
            code=code, question="Which attributes should the answer be split by?",
            options=_options(candidates.column_refs, candidates),
            allows_multiple=True,
            # A plan with no dimensions is a perfectly good plan — one overall number.
            optional=True)
    if code == "measure":
        return Clarification(
            code=code, question="What should be counted or aggregated?",
            options=_options(candidates.column_refs, candidates), optional=True)
    if code == "comparison":
        return Clarification(
            code=code, question="Is this question about a change between two periods?",
            options=tuple(Option(value=c, label=c or "no comparison")
                          for c in sorted(COMPARISONS)),
            optional=True)
    raise ClarificationError(code, f"no clarification is defined for {code!r}")


def apply_answer(plan: AnalysisPlanV1, code: str, chosen: tuple[str, ...],
                 candidates: IntentCandidates) -> AnalysisPlanV1:
    """Fold one answer into the plan, refusing anything that was never offered.

    Re-validating here rather than trusting the caller is the point: a clarification UI is the layer
    LEAST able to guarantee that what came back is what it offered.
    """
    if code not in UNRESOLVED_CODES:
        raise ClarificationError(code, f"{code!r} is not an answerable abstention")

    # A SELECTION refusal is answered by the source/row SELECTOR, not by folding a value into
    # `AnalysisPlanV1` — the plan contract has no per-need source slot, and inventing one here would
    # be a second, ungoverned place where a source gets chosen. `SELECTION_POPULATION_UNDECLARED` is
    # the one exception, and only because it means exactly what the existing `population` abstention
    # means: it folds through the SAME branch below rather than growing a parallel one.
    if code == SELECTION_POPULATION_UNDECLARED:
        code = "population"
    elif code in SELECTION_REFUSAL_CODES:
        raise ClarificationError(
            code,
            f"{code} is answered when the plan selects its sources and rows, not by editing the "
            "plan: the answer is a serving/temporal policy declaration or a different requested "
            "dataset, and the selector applies it")

    if code == "comparison":
        (value,) = chosen or ("",)
        if value not in COMPARISONS:
            raise ClarificationError(code, f"{value!r} is not one of {sorted(COMPARISONS)}")
        return replace(plan, comparison=value)

    unknown = [ref for ref in chosen if ref not in candidates.column_refs]
    if unknown:
        raise ClarificationError(
            code, f"{unknown[0]!r} was not offered for this question; an answer cannot introduce a "
                  "catalog object the plan was never grounded against")

    if code == "population":
        if len(chosen) != 1:
            raise ClarificationError(
                code, "exactly one table is the population; two would mean two different answers")
        ref = chosen[0]
        # The table comes from the ref by string, never by looking one up — deriving it is safe,
        # inferring it is the thing the doctrine forbids.
        source, _, rest = ref.partition("::")
        table = ".".join(rest.split(".")[:-1])
        return replace(plan, population_table_ref=f"{source}::{table}", population_key_ref=ref)
    if code == "entity":
        if len(chosen) != 1:
            raise ClarificationError(code, "exactly one column identifies the entity")
        return replace(plan, entity_ref=chosen[0])
    if code == "dimensions":
        return replace(plan, dimensions=tuple(Dimension(logical_ref=r) for r in chosen))
    if code == "measure":
        if len(chosen) != 1:
            raise ClarificationError(code, "exactly one column is aggregated")
        return replace(plan, measure=Measure(op=plan.measure.op, logical_ref=chosen[0]))
    # `windows` picks the ANCHOR; the period itself comes from the question or from the caller's
    # calendar, so this re-anchors the windows the model proposed rather than inventing any.
    if not plan.windows:
        raise ClarificationError(
            code, "there are no windows to anchor: the period itself must come from the question")
    if len(chosen) != 1:
        raise ClarificationError(code, "exactly one column anchors the periods")
    return replace(plan, windows=tuple(replace(w, anchor_ref=chosen[0]) for w in plan.windows))


# ── Release-B selection refusals rendered as questions (plan rule 10) ────────────────────────────
#
# "Every typed refusal becomes a data-agent clarification when interactive." These render the eight
# closed `SELECTION_REFUSAL_CODES`. The options stay bounded by the same candidate set intent was
# given, or by a CLOSED server vocabulary — a clarification may never introduce a ref the plan was
# not grounded against, and that rule does not relax because the refusal came from the selector
# rather than from the model.
#
# NO-BLOCKED FRAMING: none of these is worded as a failure. Each says what is undecided and what
# deciding it would look like, because an unreviewed or undeclared value is "nobody has decided
# yet", not "this is broken".

_STORAGE_MODEL_OPTIONS: tuple[Option, ...] = tuple(
    Option(value=m.value, label=m.value.replace("_", " "))
    for m in TemporalStorageModel if m is not TemporalStorageModel.UNKNOWN)


def _eligible_candidates(refusal: SelectionRefusalV1 | None) -> int:
    """How many considered candidates COULD have served — tied, eligible or selected.

    A REJECTED candidate is one the selector looked at and ruled out (no binding, no profile, not
    in the policy). Counting those as choices is what made the zero-eligible refusal offer a list
    of datasets that had each already failed."""
    if refusal is None:
        return -1                       # unknown: the caller passed codes only
    usable = {CandidateDisposition.TIED, CandidateDisposition.ELIGIBLE,
              CandidateDisposition.SELECTED}
    return sum(1 for c in refusal.considered_candidates if c.disposition in usable)


def _selection_clarification(code: str, candidates: IntentCandidates,
                             refusal: SelectionRefusalV1 | None = None) -> Clarification:
    tables = _options(candidates.table_refs, candidates)
    if code == SELECTION_POPULATION_UNDECLARED:
        # The same decision the `population` abstention asks for, reached from the selector instead
        # of from the model — so it asks the SAME question, in the same words, with the SAME
        # options. Only the code differs, and it must: the answer is routed back by code, and
        # `apply_answer` folds this one through the `population` branch.
        return replace(_build("population", candidates), code=code)
    if code == SELECTION_SOURCE_AMBIGUOUS:
        # ONE code, TWO situations — `source_selector` reuses this spelling for "nothing is
        # eligible" because the vocabulary is closed at eight and the GAP and ACTION
        # (DATASET_SOURCE_UNRESOLVED / DECLARE_SERVING_POLICY) are right for both. The QUESTION is
        # not: asking "which of these equally eligible datasets?" when none is eligible offers a
        # list of candidates that each already failed, and tells the reader something untrue about
        # what the system found. The payload's own dispositions say which situation this is.
        if _eligible_candidates(refusal) == 0:
            return Clarification(
                code=code,
                question="No dataset is eligible to serve this need — nothing considered has both "
                         "an assembled profile and a current physical address. Declare a serving "
                         "policy naming the dataset that should serve it, or name the dataset in "
                         "the request.",
                # No options, deliberately: every candidate here was REJECTED, and offering a
                # rejected dataset as a choice invites an answer the selector will refuse again.
                options=())
        return Clarification(
            code=code,
            question="More than one dataset is equally eligible to serve this. Which one should "
                     "be used?",
            options=tables)
    if code == SELECTION_AUTHORITY_INSUFFICIENT:
        return Clarification(
            code=code,
            question="No dataset here is declared authoritative enough to answer this in "
                     "production. Which copy should serve it — or should this run in sandbox?",
            options=tables)
    if code == SELECTION_BINDING_MISSING:
        return Clarification(
            code=code,
            question="This dataset has no configured physical address yet, so it can be described "
                     "but not read. Which dataset should be used instead?",
            options=tables)
    if code == TEMPORAL_MODEL_UNKNOWN:
        return Clarification(
            code=code,
            question="Nobody has recorded how this dataset stores history. How does it?",
            options=_STORAGE_MODEL_OPTIONS)
    if code == TEMPORAL_HISTORICAL_CURRENT_ONLY:
        return Clarification(
            code=code,
            question="This dataset keeps only today's values, so it cannot answer a question about "
                     "an earlier date. Which dataset holds the history?",
            options=tables)
    if code == TEMPORAL_SNAPSHOT_TIE:
        return Clarification(
            code=code,
            question="Two snapshots are equally close to the cutoff and nothing says which wins. "
                     "Which column should break the tie?",
            options=_options(candidates.as_of_refs or candidates.column_refs, candidates))
    if code == TEMPORAL_SCD_OVERLAP:
        # Deliberately answerable by NOBODY here: overlapping history rows are a DATA defect, not a
        # decision waiting on a person — see the `REFUSAL_TO_GAP` rationale in `data_agent.learning`
        # (which excludes ATTRIBUTION_OVERLAPPING_RECORDS for the same reason). It is still SHOWN,
        # because a user staring at a refused question deserves to know why, and it names the fix
        # instead of offering a choice that would paper over bad data.
        return Clarification(
            code=code,
            question="This dataset has history rows whose validity periods overlap, so more than "
                     "one row is 'the' value at some dates. That is a data-quality fix in the "
                     "source, not a choice — the answer cannot be trusted until it is corrected.",
            options=(),
            optional=True)
    raise ClarificationError(code, f"no clarification is defined for {code!r}")
