"""A question becomes a candidate :class:`AnalysisPlanV1` — the first step of Release 4.

This is deliberately NOT text-to-SQL, and the difference is the whole design. A model handed column
names and asked for SQL produces confident wrong answers on a bank catalog: it picks the raw event
timestamp when the data is not available until two days later, sums amounts across currencies, or
joins two customer ids that were never sanctioned as the same namespace. Every one of those is
invisible in the output — the number simply looks reasonable.

So the model's job here is narrow: choose, from a bounded set of catalog objects it is GIVEN, which
ones the question is about. It never writes SQL, never invents a ref, and never decides a business
policy.

**A ref it was not offered is rejected, not repaired into.** ``validate_intent`` refuses any ref
outside the supplied candidate set, which makes a hallucinated column a bounded repair with a
specific complaint rather than a plausible plan that grounds cleanly against nothing. This is the
structural version of the ``is_known_concept`` check the enrichment path uses on the concept
vocabulary.

**Abstention is a first-class answer.** ``unresolved`` carries what the model could not determine
from the question, and it is the input to Release 4's clarification step. A model forced to fill
every field invents a dimension or picks an arbitrary window, and the result is a confident answer to
a question nobody asked. Anything the model abstains on stays absent from the plan.

**What this does NOT decide.** Eligibility, attribution, physical bindings and partition values are
all outside the plan contract by construction — see :mod:`featuregen.analysis.execution` for why each
is a refusal rather than a default. The model is never asked for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure, Window
from featuregen.contracts import SchemaValidationError
from featuregen.intake.llm import (
    STATUS_OK,
    STATUS_REPAIRED,
    STATUS_RETRIED,
    LLMClient,
    LLMRequest,
    drive_structured_call,
)

TASK = "analysis.intent"
PROMPT_ID = "analysis_intent_v1"
PROMPT_VERSION = 1
SCHEMA_ID = "analysis_intent"
SCHEMA_VERSION = 1

#: The measure operations the plan contract knows. Closed, and checked here rather than downstream, so
#: an unknown op is a repair with a named complaint instead of a refusal three layers later.
MEASURE_OPS: frozenset[str] = frozenset({"count", "count_distinct", "sum", "avg", "min", "max"})
COMPARISONS: frozenset[str] = frozenset({"", "decrease", "increase", "change"})
CALENDAR_UNITS: frozenset[str] = frozenset({"month", "day"})

#: What the model may say it could not determine. Closed so an abstention is actionable — a free-text
#: "not sure" cannot be routed to a clarification question.
UNRESOLVED_CODES: frozenset[str] = frozenset({
    "entity",          # who the question is about
    "measure",         # what is being counted or aggregated
    "windows",         # which periods
    "dimensions",      # how to split the answer
    "comparison",      # whether it is a period-over-period question at all
    # WHICH TABLE IS THE POPULATION. Deliberately absent from the output schema: the model is never
    # asked, and `extract_intent` raises this unconditionally for a comparison. See `plan.py` —
    # choosing among look-alike population tables is inference, and a model doing it is inference by
    # something with less standing than the catalog.
    "population",
})

#: Every wire object is CLOSED (`additionalProperties: false`) and every slot declared. A previous
#: schema in this codebase left objects open and a model omitted a required-but-unenforced field,
#: which produced 100% ungrounded output that looked structurally fine.
INTENT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entity", "entity_ref", "base_table_ref", "measure", "windows", "dimensions",
                 "comparison", "unresolved"],
    "properties": {
        "entity": {"type": "string"},
        "entity_ref": {"type": "string"},
        "base_table_ref": {"type": "string"},
        "measure": {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", "logical_ref"],
            "properties": {
                "op": {"type": "string", "enum": sorted(MEASURE_OPS)},
                "logical_ref": {"type": "string"},
            },
        },
        "windows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "anchor_ref", "calendar_unit", "calendar_length",
                             "calendar_offset"],
                "properties": {
                    "label": {"type": "string"},
                    "anchor_ref": {"type": "string"},
                    "calendar_unit": {"type": "string", "enum": sorted(CALENDAR_UNITS)},
                    "calendar_length": {"type": "integer"},
                    "calendar_offset": {"type": "integer"},
                },
            },
        },
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["logical_ref"],
                "properties": {"logical_ref": {"type": "string"}},
            },
        },
        "comparison": {"type": "string", "enum": sorted(COMPARISONS)},
        "unresolved": {"type": "array", "items": {"type": "string",
                                                 "enum": sorted(UNRESOLVED_CODES)}},
    },
}


@dataclass(frozen=True, slots=True)
class IntentCandidates:
    """The bounded set of catalog objects the model may choose from.

    Supplied by the caller — bounded semantic retrieval is a separate concern, and keeping it out of
    here is what lets this module be tested without a catalog. What matters for correctness is that
    the set is CLOSED: anything outside it is rejected.
    """

    column_refs: frozenset[str]
    table_refs: frozenset[str]
    #: Optional human labels, purely to help the model choose. Never used for validation.
    labels: dict[str, str] = field(default_factory=dict)
    #: The governed structural roles, carried so a CLARIFICATION can offer the right few columns
    #: rather than all of them — "which column identifies the customer?" should list identifiers, not
    #: sixty descriptive fields. Retrieval already reads `is_grain`/`is_as_of` to decide what to
    #: offer, and used to discard the distinction on the way out. Advisory only: validation still
    #: turns on `column_refs` alone, so a mislabelled role can narrow a question but can never widen
    #: what may be named.
    grain_refs: frozenset[str] = frozenset()
    as_of_refs: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class IntentExtraction:
    """A candidate plan plus what the model abstained on.

    ``plan`` is a CANDIDATE: expressible, and not yet checked against the catalog's governed facts.
    :func:`featuregen.analysis.grounding.ground_analysis_plan` is what decides whether it is
    answerable and what is doubtful about it.
    """

    plan: AnalysisPlanV1
    unresolved: tuple[str, ...] = ()
    status: str = STATUS_OK
    provider_calls: int = 1

    @property
    def needs_clarification(self) -> bool:
        return bool(self.unresolved)


def validate_intent(output: dict, candidates: IntentCandidates) -> None:
    """Raise :class:`SchemaValidationError` when the model chose something it was not offered.

    Driving this through the repair loop rather than refusing outright is deliberate: the complaint
    names the offending ref, so a second attempt can correct it. What must never happen is a ref
    passing through — it would ground against nothing and read as a catalog gap rather than a model
    error.
    """
    def _require_column(ref: str, what: str) -> None:
        if ref and ref not in candidates.column_refs:
            raise SchemaValidationError(
                f"{what} {ref!r} is not one of the columns offered for this question")

    _require_column(str(output.get("entity_ref", "")), "entity_ref")
    table = str(output.get("base_table_ref", ""))
    if table and table not in candidates.table_refs:
        raise SchemaValidationError(
            f"base_table_ref {table!r} is not one of the tables offered for this question")

    measure = output.get("measure") or {}
    op = str(measure.get("op", ""))
    if op not in MEASURE_OPS:
        raise SchemaValidationError(f"measure op {op!r} is not one of {sorted(MEASURE_OPS)}")
    # count(*) needs no column; every other op aggregates one, and an op with no ref is not a measure.
    measure_ref = str(measure.get("logical_ref", ""))
    if op != "count" and not measure_ref:
        raise SchemaValidationError(f"measure op {op!r} needs a column to aggregate")
    _require_column(measure_ref, "measure logical_ref")

    for window in output.get("windows") or ():
        _require_column(str(window.get("anchor_ref", "")), "window anchor_ref")
        if not str(window.get("label", "")).strip():
            raise SchemaValidationError(
                "every window needs a label: partition values are matched to windows by label, and "
                "position would swap two periods and invert the answer")
    for dimension in output.get("dimensions") or ():
        _require_column(str(dimension.get("logical_ref", "")), "dimension logical_ref")

    comparison = str(output.get("comparison", ""))
    if comparison not in COMPARISONS:
        raise SchemaValidationError(f"comparison {comparison!r} is not one of {sorted(COMPARISONS)}")

    for code in output.get("unresolved") or ():
        if code not in UNRESOLVED_CODES:
            raise SchemaValidationError(
                f"unresolved code {code!r} is not actionable; use one of {sorted(UNRESOLVED_CODES)}")


def _plan_from(output: dict, question: str) -> AnalysisPlanV1:
    """Build the plan, honouring abstention: anything in ``unresolved`` stays ABSENT rather than
    being filled with a guess the caller cannot distinguish from a real answer."""
    unresolved = set(output.get("unresolved") or ())
    measure = output.get("measure") or {}

    windows: tuple[Window, ...] = ()
    if "windows" not in unresolved:
        windows = tuple(
            Window(anchor_ref=str(w["anchor_ref"]), label=str(w["label"]),
                   length_days=0, offset_days=0,
                   calendar_unit=str(w["calendar_unit"]),
                   calendar_length=int(w["calendar_length"]),
                   calendar_offset=int(w["calendar_offset"]))
            for w in output.get("windows") or ())

    dimensions: tuple[Dimension, ...] = ()
    if "dimensions" not in unresolved:
        dimensions = tuple(
            Dimension(logical_ref=str(d["logical_ref"]))
            for d in output.get("dimensions") or ())

    return AnalysisPlanV1(
        question=question,
        entity=str(output.get("entity", "")),
        entity_ref=str(output.get("entity_ref", "")),
        base_table_ref=str(output.get("base_table_ref", "")),
        measure=Measure(op=str(measure.get("op", "count")),
                        logical_ref=str(measure.get("logical_ref", ""))),
        windows=windows,
        dimensions=dimensions,
        comparison="" if "comparison" in unresolved else str(output.get("comparison", "")),
    )


def extract_intent(client: LLMClient, question: str, candidates: IntentCandidates, *,
                   model: str | None = None) -> IntentExtraction:
    """Turn a question into a candidate plan, or fail into clarification.

    The plan is NOT grounded here. Grounding is a separate pass that reads the catalog's governed
    facts, and keeping the two apart is what stops a model's confidence from being mistaken for
    evidence.
    """
    inputs = {
        "question": question,
        # Metadata only — refs and labels, never a data value (§9.4). The catalog objects are what
        # the model chooses among, so they must egress; no sample or profile does.
        "catalog_metadata": {
            "column_refs": sorted(candidates.column_refs),
            "table_refs": sorted(candidates.table_refs),
            "labels": dict(sorted(candidates.labels.items())),
        },
        "instruction":
            "Choose, from the offered catalog objects only, which ones this question is about. Never "
            "name a ref that is not offered. Express periods as whole calendar units. If the "
            "question does not determine something, list it in `unresolved` and leave it empty "
            "rather than guessing.",
    }
    request = LLMRequest(
        task=TASK, prompt_id=PROMPT_ID, prompt_version=PROMPT_VERSION, inputs=inputs,
        output_schema_id=SCHEMA_ID, output_schema_version=SCHEMA_VERSION,
        generation_settings={"model": model} if model else {},
        output_schema=INTENT_SCHEMA,
        # The offered vocabulary is a large static prefix across questions against one catalog.
        cacheable_metadata_keys=("column_refs", "table_refs", "labels"),
    )

    outcome = drive_structured_call(
        client, request, lambda out: validate_intent(dict(out), candidates))

    if outcome.status not in (STATUS_OK, STATUS_REPAIRED, STATUS_RETRIED):
        # Fail into clarification rather than returning a half-plan: the caller must be able to tell
        # "the model could not express this" from "the model expressed it and the catalog disagreed".
        raise IntentUnavailable(outcome.status, outcome.validation_result.get("reason", ""))

    output = dict(outcome.output)
    unresolved = list(output.get("unresolved") or ())
    # A period-over-period question needs a population spine, and nothing in this pipeline may choose
    # it. Raised here rather than left to the model's judgement so it cannot be quietly omitted.
    if str(output.get("comparison", "")).strip() and "population" not in unresolved:
        unresolved.append("population")
    return IntentExtraction(
        plan=_plan_from(output, question),
        unresolved=tuple(unresolved),
        status=outcome.status,
        provider_calls=outcome.provider_calls,
    )


class IntentUnavailable(RuntimeError):
    """The model could not produce an expressible plan within its budgets.

    Deliberately NOT an empty plan: a caller handed one cannot distinguish a question the model could
    not read from a question whose answer is genuinely nothing.
    """

    def __init__(self, status: str, reason: str = "") -> None:
        super().__init__(f"{status}: {reason or 'no reason reported'}")
        self.status = status
        self.reason = reason
