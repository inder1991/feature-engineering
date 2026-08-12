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

import os
from dataclasses import dataclass, field

from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure, Window
from featuregen.contracts import AttestedSchemaValidationError
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.intake.llm import (
    STATUS_OK,
    STATUS_REPAIRED,
    STATUS_RETRIED,
    LLMClient,
    LLMRequest,
    compute_input_hash,
    drive_structured_call,
    mint_id,
    record_llm_call_durable,
)
from featuregen.intake.llm_claude import ClaudeConfig
from featuregen.intake.redaction import (
    assert_llm_safe,
    build_llm_inputs,
    redact_free_text,
)

# The CLOSED Release-B refusal vocabulary, imported rather than re-spelled: one set, one spelling,
# one owner (`overlay.upload.source_selection`). That module is deliberately import-light — it
# defers `semantic_context` to call time — so this stays a cheap import for the analysis package,
# unlike the `enrich_llm` dependency the helper above exists to avoid.
from featuregen.overlay.upload.source_selection import SELECTION_REFUSAL_CODES


def _generation_settings() -> dict:
    """Provider + model + the pinned knobs, from the SAME env that configures the client.

    Deliberately NOT imported from `overlay.upload.enrich_llm`, which has an identical helper: this
    package must not depend on the overlay enrichment STAGE for five lines. Importing it pulled that
    module's whole graph — and its import-time registrations — into the analysis package, which
    reordered a registry another test asserts on. Five duplicated lines beat a dependency that
    reaches across two subsystems to fetch them.
    """
    provider = os.environ.get("FEATUREGEN_LLM_PROVIDER", "fake")
    if provider == "anthropic":
        cfg = ClaudeConfig.from_env()
        return {"provider": "anthropic", "model": cfg.model, "max_tokens": cfg.max_tokens,
                "thinking": cfg.thinking, "effort": cfg.effort}
    return {"provider": "fake", "model": "test"}


TASK = "analysis.intent"
PROMPT_ID = "analysis_intent_v1"
#: The INPUT contract's version, stamped on the immutable llm_call record.
#:   1 — offered refs + labels only (the shape this module shipped with).
#:   2 — v1 plus bounded `SemanticContextBundleV1.for_analysis_planning` projections carrying the
#:       D2 authority axes and the closed missing-context codes (semantic Task 9).
#: The OUTPUT schema is UNCHANGED at both versions — the version identifies which INPUT egressed,
#: which is the only thing that makes an audit record unambiguous about what the model was shown.
PROMPT_VERSION = 1
PROMPT_VERSION_V2 = 2
SCHEMA_ID = "analysis_intent"
SCHEMA_VERSION = 1

#: The measure operations the plan contract knows. Closed, and checked here rather than downstream, so
#: an unknown op is a repair with a named complaint instead of a refusal three layers later.
MEASURE_OPS: frozenset[str] = frozenset({"count", "count_distinct", "sum", "avg", "min", "max"})
COMPARISONS: frozenset[str] = frozenset({"", "decrease", "increase", "change"})
CALENDAR_UNITS: frozenset[str] = frozenset({"month", "day"})

#: What THE MODEL may say it could not determine. Closed so an abstention is actionable — a
#: free-text "not sure" cannot be routed to a clarification question. This set, and only this set,
#: is what the wire schema's `unresolved` enum admits and what `validate_intent` accepts back.
MODEL_UNRESOLVED_CODES: frozenset[str] = frozenset({
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

#: Every abstention that can become a CLARIFICATION — the model's own codes plus the closed
#: Release-B selection refusals (`overlay.upload.source_selection.SELECTION_REFUSAL_CODES`).
#:
#: THE SPLIT IS DELIBERATE, and it is the reason `MODEL_UNRESOLVED_CODES` exists at all. The wire
#: schema's enum is derived from this vocabulary, so widening one set widens what the model is
#: invited to assert. Functional rule 1 says the LLM never attests uniqueness, overlap, population
#: completeness or row-history correctness — so a model must not be able to answer `TEMPORAL_SCD_OVERLAP`
#: or `SELECTION_BINDING_MISSING`. Those are conditions the SELECTOR observes about the catalog and
#: the data, never things a language model can know. Keeping the schema enum on the narrow set also
#: leaves the request body byte-identical, so no prompt/schema version bump and no replay churn
#: (D10: a schema change is a REAL version bump, never a silent widening).
UNRESOLVED_CODES: frozenset[str] = MODEL_UNRESOLVED_CODES | SELECTION_REFUSAL_CODES

#: Every wire object is CLOSED (`additionalProperties: false`) and every slot declared. A previous
#: schema in this codebase left objects open and a model omitted a required-but-unenforced field,
#: which produced 100% ungrounded output that looked structurally fine.
INTENT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entity", "entity_ref", "base_table_ref", "measure", "windows", "dimensions",
                 "comparison", "unresolved"],
    "properties": {
        "entity": {"type": "string",
                   "description": "The thing the answer is per, in business words, e.g. 'customer'."},
        "entity_ref": {
            "type": "string",
            "description": "The COLUMN that identifies that thing, chosen from the offered "
                           "column_refs. A column, never a table: it is what rows are counted per.",
        },
        "base_table_ref": {
            "type": "string",
            "description": "The TABLE holding the events being counted, chosen from the offered "
                           "table_refs.",
        },
        "measure": {
            "type": "object",
            "additionalProperties": False,
            "required": ["op", "logical_ref"],
            "properties": {
                "op": {"type": "string", "enum": sorted(MEASURE_OPS),
                       "description": "Use 'count' to count rows; the others aggregate a column."},
                "logical_ref": {
                    "type": "string",
                    "description": "The COLUMN aggregated. Leave empty for 'count'.",
                },
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
                    "label": {"type": "string",
                              "description": "A short name for this period, e.g. 'current'."},
                    "anchor_ref": {
                        "type": "string",
                        "description": "The COLUMN the period is measured on, from column_refs.",
                    },
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
                "properties": {"logical_ref": {
                    "type": "string",
                    "description": "A COLUMN to split the answer by, from column_refs.",
                }},
            },
        },
        "comparison": {"type": "string", "enum": sorted(COMPARISONS)},
        # The MODEL's vocabulary only (see MODEL_UNRESOLVED_CODES): a language model may not assert
        # a selection/temporal condition it cannot observe.
        "unresolved": {"type": "array", "items": {"type": "string",
                                                 "enum": sorted(MODEL_UNRESOLVED_CODES)}},
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
class AnalysisIntentInputV2:
    """The VERSIONED input contract for intent extraction (semantic Task 9).

    :class:`IntentCandidates` is UNVERSIONED and stays that way — it is the closed offered set and
    nothing more. This wrapper is what carries a version, so a later shape change is a contract
    bump with an audit trail rather than a silent widening of what the model was shown.

    **Placement is deliberately unchanged.** Everything here rides in the SAME `catalog_metadata`
    block the offered refs already ride in, which lands in the USER turn beside the instruction
    telling the model to choose only from them. Moving metadata into a cacheable `system` block once
    separated the refs from that instruction and the extraction failed validation until the repair
    budget ran out — a recorded repair-exhaustion history, not a hypothetical. So: version the
    contract, add keys, move nothing.

    `context` entries are bounded `for_analysis_planning` projections, each keyed by the SAME `ref`
    spelling :func:`validate_intent` matches on. They are EXPLANATION: context can never widen what
    may be named, because validation still turns on `candidates.column_refs` alone."""

    candidates: IntentCandidates
    context: tuple[dict, ...] = ()
    #: Closed `MISSING_CONTEXT_CODES` for the offered set as a whole — what this view does NOT
    #: carry. Read-scoped by construction, so it is never a data-quality verdict.
    missing_context: tuple[str, ...] = ()
    contract_version: int = 2


def _as_input(candidates: IntentCandidates | AnalysisIntentInputV2) -> AnalysisIntentInputV2:
    """Accept either contract. An `IntentCandidates` is the v1 input, unchanged and unversioned;
    the wrapper is what a v2 caller passes. Both are supported on purpose — the analysis route is
    not the only caller, and forcing every test to wrap would be churn without a property."""
    if isinstance(candidates, AnalysisIntentInputV2):
        return candidates
    return AnalysisIntentInputV2(candidates=candidates, contract_version=1)


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
    """Raise :class:`AttestedSchemaValidationError` when the model chose something it was not offered.

    Driving this through the repair loop rather than refusing outright is deliberate: the complaint
    names the offending ref, so a second attempt can correct it. What must never happen is a ref
    passing through — it would ground against nothing and read as a catalog gap rather than a model
    error.

    **Two complaints, and they are not the same string.** The MESSAGE stays as informative as it
    has always been — it interpolates the ref, and it is read by a human looking at a traceback,
    which no wire and no audit column ever sees. The ATTESTED reason
    (:class:`AttestedSchemaValidationError`) is the one `intake.llm._safe_reason` sends back to the
    provider and writes to `llm_call.repair_attempts`, so it is built from THIS FILE'S literals and
    the closed vocabularies declared at the top of it, and from nothing else.

    Every one of these raises used to reach `_safe_reason` with no jsonschema `__cause__`, so the
    whole intent flow complained with one generic constant — the repair budget bought a
    differently-shaped prompt carrying no information. Each site now attests.

    **What is deliberately NOT attested, at every site.**

    * The ref itself (`{ref!r}`, `{op!r}`, `{code!r}`, …). It is model-supplied text on an egress
      path. "The model already knows it" is a claim about ONE of the four sinks: the reason also
      lands in audit columns that store verbatim and never re-scan, and a model that was shown a
      question whose PII the scanner missed can echo it straight back into `entity_ref`. Task 2's
      `_path_is_schema_declared` suppresses exactly this class of text from the structural pointer;
      attesting it here would re-admit through the front door what that guard turns away at the
      back, two commits later, on the same function.
    * The offered-column `sample`. It is CATALOG text. It already egresses inside
      `catalog_metadata` — but that is a property of the CALLER, and `validate_intent` is public,
      so the safety of an egress string would rest on an invariant nothing enforces. It also buys
      nothing: `_wire_prompt` re-renders the full `catalog_metadata`, including every offered
      `column_refs` entry, in the repair turn itself. The model is already holding the list.

    What the shape-only reason costs: the model is told which field failed and what that field
    wants, not which of its refs was wrong. It has one answer in front of it and one named field to
    fix, which is what the repair loop needed and never had.
    """
    offered = sorted(candidates.column_refs)
    # Bounded: enough to correct the answer, not so many that the complaint becomes another prompt.
    sample = ", ".join(offered[:12]) + ("" if len(offered) <= 12 else ", ...")

    def _require_column(ref: str, what: str, field: str) -> None:
        # TWO namings on purpose. `what` is the prose the MESSAGE has always used, read by a human
        # in a traceback. `field` is the JSON field path the attested reason uses, read by the model
        # and by whoever queries `llm_call.repair_attempts`. Both are author literals at all four
        # call sites — which are the only ones there can be, this closure being local to
        # `validate_intent` — and that locality is the whole reason attesting them is safe.
        if ref and ref not in candidates.column_refs:
            hint = ""
            attested = f"{field}: not one of the offered column_refs"
            if ref in candidates.table_refs:
                # The observed failure: the customer TABLE was given where a column identifying the
                # customer was required. Naming the confusion is what lets one repair fix it — and
                # the confusion is a SHAPE, so it survives into the attested reason intact.
                hint = (f" — that is a TABLE. {what} must be a COLUMN, one of the offered "
                        f"column_refs")
                attested = (f"{field}: that is one of the offered table_refs; it must be one of "
                            f"the offered column_refs")
            raise AttestedSchemaValidationError(
                f"{what} {ref!r} is not one of the columns offered for this question{hint}. "
                f"Choose from: {sample}",
                llm_safe_reason=attested)

    _require_column(str(output.get("entity_ref", "")), "entity_ref", "entity_ref")
    table = str(output.get("base_table_ref", ""))
    if table and table not in candidates.table_refs:
        raise AttestedSchemaValidationError(
            f"base_table_ref {table!r} is not one of the tables offered for this question",
            llm_safe_reason="base_table_ref: not one of the offered table_refs")

    measure = output.get("measure") or {}
    op = str(measure.get("op", ""))
    if op not in MEASURE_OPS:
        # MEASURE_OPS is an in-code closed vocabulary that ALREADY egresses — `INTENT_SCHEMA`
        # carries it verbatim as the `measure.op` enum on every call. Author text, not data.
        raise AttestedSchemaValidationError(
            f"measure op {op!r} is not one of {sorted(MEASURE_OPS)}",
            llm_safe_reason=("measure.op: not one of the operations this contract defines: "
                             f"{sorted(MEASURE_OPS)}"))
    # count(*) needs no column; every other op aggregates one, and an op with no ref is not a measure.
    measure_ref = str(measure.get("logical_ref", ""))
    if op != "count" and not measure_ref:
        raise AttestedSchemaValidationError(
            f"measure op {op!r} needs a column to aggregate",
            llm_safe_reason=("measure.logical_ref: required for every op except 'count', which "
                             "counts rows and takes no column"))
    _require_column(measure_ref, "measure logical_ref", "measure.logical_ref")

    for window in output.get("windows") or ():
        _require_column(str(window.get("anchor_ref", "")), "window anchor_ref",
                        "windows[].anchor_ref")
        if not str(window.get("label", "")).strip():
            # The only site whose message was already value-free. Attested anyway: the seam is the
            # TYPE, and one site left generic because its message happened to be safe is a trap for
            # whoever edits it next.
            raise AttestedSchemaValidationError(
                "every window needs a label: partition values are matched to windows by label, and "
                "position would swap two periods and invert the answer",
                llm_safe_reason="windows[].label: every window needs a non-empty label")
    for dimension in output.get("dimensions") or ():
        _require_column(str(dimension.get("logical_ref", "")), "dimension logical_ref",
                        "dimensions[].logical_ref")

    comparison = str(output.get("comparison", ""))
    if comparison not in COMPARISONS:
        raise AttestedSchemaValidationError(
            f"comparison {comparison!r} is not one of {sorted(COMPARISONS)}",
            llm_safe_reason=("comparison: not one of the values this contract defines: "
                             f"{sorted(COMPARISONS)}"))

    for code in output.get("unresolved") or ():
        if code not in MODEL_UNRESOLVED_CODES:
            raise AttestedSchemaValidationError(
                f"unresolved code {code!r} is not actionable; use one of "
                f"{sorted(MODEL_UNRESOLVED_CODES)}",
                llm_safe_reason=("unresolved[]: not an actionable abstention code; use one of "
                                 f"{sorted(MODEL_UNRESOLVED_CODES)}"))


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


def extract_intent(conn, client: LLMClient, question: str,
                   candidates: IntentCandidates | AnalysisIntentInputV2, *,
                   actor: IdentityEnvelope, model: str | None = None) -> IntentExtraction:
    """Turn a question into a candidate plan, or fail into clarification.

    `conn` and `actor` are REQUIRED, not optional conveniences: there must be no ungoverned way to
    call this. The first version took neither and consequently did three things wrong — the user's
    question egressed to the provider unscanned, the payload skipped the §9.4 egress backstop, and
    no `llm_call` record was written, so a route docstring claiming the call was "attributed to the
    human who asked" was simply false.

    **The QUESTION is free text from a person.** "show me transactions for Ahmed Al-Mansouri" is a
    perfectly natural thing to type, and it carries a customer name. It goes through
    `redact_free_text`, which scans and classifies it honestly — a hit means the spans are scrubbed
    and the payload is marked `contains_pii`, and no hit means `clean` BECAUSE IT WAS SCANNED.

    The plan is NOT grounded here. Grounding is a separate pass over the catalog's governed facts,
    and keeping the two apart is what stops a model's confidence being mistaken for evidence.
    """
    supplied = _as_input(candidates)
    candidates = supplied.candidates
    # The question is the intent; the offered catalog objects are metadata. Same split the
    # enrichment path uses, so the reserved-key contract `assert_llm_safe` checks is satisfied.
    redaction = redact_free_text(question)
    metadata: dict = {
        "column_refs": sorted(candidates.column_refs),
        "table_refs": sorted(candidates.table_refs),
        "labels": dict(sorted(candidates.labels.items())),
        "instruction":
            "Choose, from the offered catalog objects only, which ones this question is about. "
            "Never name a ref that is not offered. Express periods as whole calendar units. If "
            "the question does not determine something, list it in `unresolved` and leave it "
            "empty rather than guessing.",
    }
    if supplied.contract_version >= 2:
        # SAME BLOCK, new keys — placement is deliberately untouched (see AnalysisIntentInputV2).
        metadata["contract_version"] = supplied.contract_version
        if supplied.context:
            metadata["semantic_context"] = [dict(entry) for entry in supplied.context]
        if supplied.missing_context:
            metadata["missing_context"] = list(supplied.missing_context)
    inputs = build_llm_inputs(
        redaction, catalog_metadata=metadata,
        raw_input_classification=(
            "contains_pii" if redaction.redacted_spans else "clean"))

    request = LLMRequest(
        task=TASK, prompt_id=PROMPT_ID,
        prompt_version=(PROMPT_VERSION_V2 if supplied.contract_version >= 2 else PROMPT_VERSION),
        inputs=inputs,
        output_schema_id=SCHEMA_ID, output_schema_version=SCHEMA_VERSION,
        # From the SAME env that configures the client, so the audit record says what the adapter
        # actually applied — and `llm_call.provider` is NOT NULL, so an empty dict cannot be written
        # at all. A per-call `model` override still wins.
        generation_settings=({**_generation_settings(), "model": model}
                             if model else _generation_settings()),
        output_schema=INTENT_SCHEMA)
    # NO `cacheable_metadata_keys`. That option exists for the enrichment path's genuinely STATIC
    # prefix — the ~276-concept vocabulary, identical across every item of every batch. The offered
    # refs here are RETRIEVED PER QUESTION, so they can never cache-hit; all the flag did was pop
    # them out of the user turn into a `system` block labelled "shared classification context",
    # separating the refs from the instruction telling the model to choose only from them. The
    # model then produced output that failed validation until the repair budget ran out.

    # §9.4 backstop, BEFORE dispatch. A hard failure: nothing is sent.
    assert_llm_safe(request)

    outcome = drive_structured_call(
        client, request, lambda out: validate_intent(dict(out), candidates))

    # Written whatever the disposition — a refused or malformed call still egressed, and the record
    # is the evidence that it did.
    # DURABLE: this route turns a refused extraction into a 422, which unwinds the request
    # transaction. A record on that connection would vanish with it — erasing the only
    # evidence that the question already egressed to the provider.
    record_llm_call_durable(
        conn, run_id=mint_id("arun"), request=request,
        input_hash=compute_input_hash(inputs),
        redaction_version=redaction.redaction_version,
        input_redaction={"redacted_spans": [dict(s) for s in redaction.redacted_spans]},
        raw_output={"output": outcome.output,
                    "self_reported_scores": outcome.self_reported_scores},
        validation_result=outcome.validation_result,
        repair_attempts=list(outcome.repair_attempts), latency_ms=None,
        cost_metadata=outcome.cost_metadata, created_by=identity_to_jsonb(actor))

    if outcome.status not in (STATUS_OK, STATUS_REPAIRED, STATUS_RETRIED):
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
        provider_calls=outcome.provider_calls)


class IntentUnavailable(RuntimeError):
    """The model could not produce an expressible plan within its budgets.

    Deliberately NOT an empty plan: a caller handed one cannot distinguish a question the model could
    not read from a question whose answer is genuinely nothing.
    """

    def __init__(self, status: str, reason: str = "") -> None:
        super().__init__(f"{status}: {reason or 'no reason reported'}")
        self.status = status
        self.reason = reason
