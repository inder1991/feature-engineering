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

from dataclasses import dataclass, replace

from featuregen.analysis.intent import (
    COMPARISONS,
    UNRESOLVED_CODES,
    IntentCandidates,
    IntentExtraction,
)
from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure


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
    """
    raised = {code for code in extraction.unresolved if code in UNRESOLVED_CODES}
    return tuple(_build(code, candidates)
                 for code in sorted(raised, key=lambda c: _ORDER.get(c, 99)))


_ORDER = {"population": 0, "entity": 1, "measure": 2, "windows": 3, "comparison": 4,
          "dimensions": 5}


def _build(code: str, candidates: IntentCandidates) -> Clarification:
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
