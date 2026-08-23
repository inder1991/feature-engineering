"""Question -> candidate plan, with a bounded vocabulary the model cannot escape.

The whole reason this is not text-to-SQL: a model handed column names writes confident wrong SQL on a
bank catalog, and the wrongness is invisible in the output. Here the model only CHOOSES among catalog
objects it was given, and two properties make that safe:

* a ref it was not offered is rejected — proven by `test_a_hallucinated_column_is_rejected`, which is
  the difference between a hallucination and a plausible plan that grounds against nothing;
* abstention is a real answer — `unresolved` leaves the field EMPTY instead of inventing a dimension
  or a window, because a plan filled with guesses cannot be told apart from a plan that was understood.

Hermetic: `FakeLLM`, no network, no key.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.intent import (
    INTENT_SCHEMA,
    IntentCandidates,
    IntentUnavailable,
    extract_intent,
    validate_intent,
)
from featuregen.contracts import AttestedSchemaValidationError, SchemaValidationError
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import (
    PROVIDER_REFUSAL,
    STATUS_OK,
    STATUS_REPAIRED,
    FakeLLM,
    FakeResponse,
)

_QUESTION = "which customers had fewer transactions this month than last, by segment and sector"

_COLUMNS = frozenset({
    "ftr::dpl_eib.tran_repos.cif_id",
    "ftr::dpl_eib.tran_repos.tran_month",
    "ftr::dpl_eib.tran_repos.tran_amt",
    "ftr::dpl_eib.customer_segment_history.segment",
    "ftr::dpl_eib.customer_segment_history.sector",
})
_TABLES = frozenset({"ftr::dpl_eib.tran_repos", "ftr::dpl_eib.customer_segment_history"})


def _candidates() -> IntentCandidates:
    return IntentCandidates(column_refs=_COLUMNS, table_refs=_TABLES,
                            labels={"ftr::dpl_eib.tran_repos.cif_id": "customer identifier"})


def _output(**over) -> dict:
    out = {
        "entity": "customer",
        "entity_ref": "ftr::dpl_eib.tran_repos.cif_id",
        "base_table_ref": "ftr::dpl_eib.tran_repos",
        "measure": {"op": "count", "logical_ref": ""},
        "windows": [
            {"label": "current", "anchor_ref": "ftr::dpl_eib.tran_repos.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0},
            {"label": "previous", "anchor_ref": "ftr::dpl_eib.tran_repos.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 1},
        ],
        "dimensions": [{"logical_ref": "ftr::dpl_eib.customer_segment_history.segment"},
                       {"logical_ref": "ftr::dpl_eib.customer_segment_history.sector"}],
        "comparison": "decrease",
        "unresolved": [],
    }
    out.update(over)
    return out


_ACTOR = IdentityEnvelope(subject="analyst", actor_kind="human", authenticated=True,
                         auth_method="oidc", role_claims=("feature_engineer",))


def _extract(db, client, question=None, candidates=None, **kw):
    """`extract_intent` now REQUIRES a connection and an actor — it writes the llm_call record and
    runs the egress backstop, and there is deliberately no ungoverned way to call it."""
    return extract_intent(db, client, question or _QUESTION,
                          candidates or _candidates(), actor=_ACTOR, **kw)


def _llm(*responses: FakeResponse) -> FakeLLM:
    from featuregen.analysis.intent import TASK
    return FakeLLM(script={TASK: list(responses) or [FakeResponse(output=_output())]})


# ── the pilot question ───────────────────────────────────────────────────────────────────────────

def test_the_pilot_question_becomes_a_candidate_plan(db):
    got = _extract(db, _llm(), _QUESTION, _candidates())
    plan = got.plan
    assert plan.question == _QUESTION
    assert plan.entity == "customer"
    assert plan.comparison == "decrease"
    assert plan.measure.op == "count"
    assert [w.label for w in plan.windows] == ["current", "previous"]
    assert [d.logical_ref for d in plan.dimensions] == [
        "ftr::dpl_eib.customer_segment_history.segment",
        "ftr::dpl_eib.customer_segment_history.sector"]
    # The population is ALWAYS unresolved for a comparison: the model is not asked, because choosing
    # among look-alike population tables is inference. See `plan.py` / `materialize/spine.py`.
    assert got.unresolved == ("population",)


def test_the_windows_arrive_as_whole_calendar_periods(db):
    """Day spans cannot express a calendar month, so the model is asked for units — and the offsets
    are what make "this month vs last" two distinct partitions."""
    plan = _extract(db, _llm(), _QUESTION, _candidates()).plan
    current, previous = plan.windows
    assert (current.calendar_unit, current.calendar_offset) == ("month", 0)
    assert (previous.calendar_unit, previous.calendar_offset) == ("month", 1)


def test_the_extracted_plan_flows_through_grounding_and_execution(db):
    """The whole chain in one test: question -> plan -> partitions -> IR -> SQL -> the fixture's
    hand-counted answer. Without this the pieces could each pass and not compose."""
    from datetime import UTC, datetime

    from tests.featuregen.analysis.test_plan_to_execution import _inputs
    from tests.featuregen.data_agent.pilot_fixture import EXPECTED, create_pilot_tables

    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.analysis.plan import GroundedPlan
    from featuregen.analysis.windows import PartitionGranularity, resolve_window_partitions
    from featuregen.data_agent.analysis import run_analysis
    from featuregen.data_agent.sql_postgres import PostgresDialect

    create_pilot_tables(db)
    plan = _extract(db, _llm(), _QUESTION, _candidates()).plan
    partitions = resolve_window_partitions(
        plan.windows, granularity=PartitionGranularity.MONTH,
        reference=datetime(2026, 6, 30, tzinfo=UTC))
    ir = plan_to_execution_ir(GroundedPlan(plan=plan, answerable=True),
                              _inputs(window_partitions=partitions))
    rows = run_analysis(db, ir, dialect=PostgresDialect())
    assert tuple(sorted(r.key for r in rows if r.decreased)) == EXPECTED["decreased_customers"]


# ── the bounded vocabulary ───────────────────────────────────────────────────────────────────────

def test_a_hallucinated_column_is_rejected():
    """THE property. A ref the model was not offered must never pass: it would ground against nothing
    and read as a catalog gap rather than a model error."""
    with pytest.raises(SchemaValidationError, match="not one of the columns"):
        validate_intent(_output(entity_ref="ftr::dpl_eib.tran_repos.customer_name"), _candidates())


def test_a_hallucinated_TABLE_is_rejected():
    with pytest.raises(SchemaValidationError, match="not one of the tables"):
        validate_intent(_output(base_table_ref="ftr::dpl_eib.secret_table"), _candidates())


def test_a_hallucinated_DIMENSION_is_rejected():
    with pytest.raises(SchemaValidationError, match="dimension logical_ref"):
        validate_intent(
            _output(dimensions=[{"logical_ref": "ftr::dpl_eib.customer_segment_history.vip_tier"}]),
            _candidates())


def test_the_repair_loop_recovers_from_one_bad_ref(db):
    """Rejecting through the repair loop rather than refusing outright is what gives the model a
    named complaint and a second attempt — the bad ref is reported, not silently dropped."""
    bad = FakeResponse(output=_output(entity_ref="ftr::dpl_eib.tran_repos.nope"))
    got = _extract(db, _llm(bad, FakeResponse(output=_output())), _QUESTION, _candidates())
    assert got.status == STATUS_REPAIRED
    assert got.plan.entity_ref == "ftr::dpl_eib.tran_repos.cif_id"
    assert got.provider_calls == 2


def test_an_aggregate_with_no_column_is_rejected():
    """`count` needs no column; `sum` of nothing is not a measure."""
    with pytest.raises(SchemaValidationError, match="needs a column"):
        validate_intent(_output(measure={"op": "sum", "logical_ref": ""}), _candidates())


def test_count_star_needs_no_column():
    validate_intent(_output(measure={"op": "count", "logical_ref": ""}), _candidates())


def test_an_unlabelled_window_is_rejected():
    """Partition values are keyed by label all the way down; position would swap two periods."""
    with pytest.raises(SchemaValidationError, match="needs a label"):
        validate_intent(_output(windows=[
            {"label": "", "anchor_ref": "ftr::dpl_eib.tran_repos.tran_month",
             "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0}]), _candidates())


@pytest.mark.parametrize("bad", ["median", "top_n", ""])
def test_an_unknown_measure_op_is_rejected(bad):
    with pytest.raises(SchemaValidationError, match="measure op"):
        validate_intent(_output(measure={"op": bad, "logical_ref": ""}), _candidates())


def test_an_unactionable_abstention_code_is_rejected():
    """A free-text "not sure" cannot be routed to a clarification question, so the vocabulary is
    closed."""
    with pytest.raises(SchemaValidationError, match="not actionable"):
        validate_intent(_output(unresolved=["dunno"]), _candidates())


# ── abstention ───────────────────────────────────────────────────────────────────────────────────

def test_an_abstained_dimension_stays_EMPTY_rather_than_guessed(db):
    """A model forced to fill every field invents a split, and the answer then looks like it was
    asked for. The abstention is carried instead."""
    got = _extract(db, _llm(FakeResponse(output=_output(
            unresolved=["dimensions"],
            dimensions=[{"logical_ref": "ftr::dpl_eib.customer_segment_history.segment"}]))),
        _QUESTION, _candidates())
    assert got.plan.dimensions == ()
    assert got.needs_clarification
    assert set(got.unresolved) == {"dimensions", "population"}


def test_the_population_is_raised_even_when_the_model_resolved_everything_else(db):
    """Unconditional for a comparison, and NOT in the output schema — so a model cannot resolve it,
    omit it, or be blamed for it. A population chosen by a model is inference by something with less
    standing than the catalog."""
    got = _extract(db, _llm(), _QUESTION, _candidates())
    assert "population" in got.unresolved
    assert "population" not in str(INTENT_SCHEMA["properties"].keys())


def test_a_question_with_NO_comparison_needs_no_population(db):
    """A single-period question has no spine to lose customers from."""
    got = _extract(db, _llm(FakeResponse(output=_output(comparison=""))), _QUESTION,
                         _candidates())
    assert "population" not in got.unresolved


def test_an_abstained_comparison_is_not_silently_a_decrease(db):
    got = _extract(db, _llm(FakeResponse(output=_output(unresolved=["comparison"], comparison="decrease"))),
        _QUESTION, _candidates())
    assert got.plan.comparison == ""


def test_an_abstained_window_leaves_no_windows(db):
    got = _extract(db, _llm(FakeResponse(output=_output(unresolved=["windows"]))), _QUESTION, _candidates())
    assert got.plan.windows == ()


# ── failure is not an empty plan ─────────────────────────────────────────────────────────────────

def test_a_provider_refusal_fails_into_clarification_not_a_blank_plan(db):
    """A caller handed an empty plan cannot tell a question the model could not read from a question
    whose answer is genuinely nothing."""
    with pytest.raises(IntentUnavailable):
        _extract(db, _llm(FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)),
                       _QUESTION, _candidates())


# ── egress ───────────────────────────────────────────────────────────────────────────────────────

def test_the_payload_uses_the_GOVERNED_reserved_key_contract(db):
    """The question is free text from a person — "show me transactions for Ahmed Al-Mansouri" is a
    natural thing to type and carries a customer name. It must go through `redact_free_text` and
    arrive under the reserved keys `assert_llm_safe` checks, not an ad-hoc `{"question": ...}` dict.

    The first version of this module sent exactly that ad-hoc shape, which meant the egress backstop
    could not inspect it and the question egressed unscanned."""
    captured: list = []

    class _Capture:
        def call(self, request):
            captured.append(request)
            return FakeLLM(script={request.task: FakeResponse(output=_output())}).call(request)

    _extract(db, _Capture())
    (request,) = captured
    assert set(request.inputs) == {"redacted_intent", "catalog_metadata",
                                   "raw_input_classification", "redaction_version",
                                   "input_redaction"}
    # The classification is what the scan ESTABLISHED, never a hardcoded "clean".
    assert request.inputs["raw_input_classification"] in {"clean", "contains_pii"}
    assert request.inputs["redaction_version"]
    # Refs and labels only — no sample, profile or data value.
    assert set(request.inputs["catalog_metadata"]) == {
        "column_refs", "table_refs", "labels", "instruction"}
    assert request.output_schema is INTENT_SCHEMA


def test_a_question_naming_a_person_is_SCANNED_before_it_egresses(db):
    """Not "is redacted" — scanned and classified honestly. A hit scrubs the spans and marks the
    payload; no hit means clean BECAUSE IT WAS SCANNED."""
    captured: list = []

    class _Capture:
        def call(self, request):
            captured.append(request)
            return FakeLLM(script={request.task: FakeResponse(output=_output())}).call(request)

    _extract(db, _Capture(), "transactions for card 4111111111111111")
    (request,) = captured
    assert "4111111111111111" not in str(request.inputs), "a card number reached the provider"


def test_the_call_is_RECORDED_with_the_caller_as_its_author(db):
    """A route docstring claimed every llm_call was attributed to the human who asked, while this
    module called the raw driver and wrote no record at all. The claim is now true, and asserted."""
    _extract(db, _llm())
    row = db.execute(
        "select task, provider, created_by->>'subject' from llm_call "
        "where task = 'analysis.intent' order by created_at desc limit 1").fetchone()
    assert row is not None, "no llm_call was written for a dispatched call"
    assert row[0] == "analysis.intent"
    assert row[1], "provider is NOT NULL and must say which provider ran it"
    assert row[2] == "analyst"


def test_a_REFUSED_call_is_still_recorded(db):
    """It egressed. The record is the evidence that it did, and a disposition that erases its own
    audit trail is the worst possible one."""
    from featuregen.intake.llm import PROVIDER_REFUSAL

    before = db.execute("select count(*) from llm_call where task='analysis.intent'").fetchone()[0]
    with pytest.raises(IntentUnavailable):
        _extract(db, _llm(FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)))
    after = db.execute("select count(*) from llm_call where task='analysis.intent'").fetchone()[0]
    assert after == before + 1


def test_every_wire_object_in_the_schema_is_CLOSED():
    """An open object let a model omit an unenforced field once before, and the run produced 100%
    ungrounded output that looked structurally fine."""
    def _walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False, node
                assert "required" in node, node
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(INTENT_SCHEMA)


def test_the_status_is_reported_so_a_repair_is_never_invisible(db):
    got = _extract(db, _llm(), _QUESTION, _candidates())
    assert got.status == STATUS_OK


def test_the_offered_refs_ride_the_SAME_turn_as_the_instruction(db):
    """The refs and the instruction to choose only from them must not be separated.

    `cacheable_metadata_keys` moves a key out of the user turn into a cached `system` block. That is
    right for the enrichment path's static concept vocabulary and wrong here: retrieval builds a
    DIFFERENT candidate set per question, so it can never cache-hit — and displacing the refs left
    the model producing output that failed validation until the repair budget ran out. Asserted on
    the request, because the symptom appeared only against a real provider.
    """
    captured: list = []

    class _Capture:
        def call(self, request):
            captured.append(request)
            return FakeLLM(script={request.task: FakeResponse(output=_output())}).call(request)

    _extract(db, _Capture())
    (request,) = captured
    assert request.cacheable_metadata_keys == ()
    metadata = request.inputs["catalog_metadata"]
    assert {"column_refs", "table_refs", "instruction"} <= set(metadata)


def test_a_TABLE_given_where_a_column_belongs_says_so_explicitly():
    """The failure observed against a real provider: asked which column identifies the customer, the
    model answered with the customer TABLE — a reasonable reading of the field name, and wrong.

    The old complaint said only "is not one of the columns offered", which names the violation and
    not the requirement, so the identical answer came back twice and exhausted the repair budget. A
    repair loop is only worth having if its complaint can be acted on."""
    with pytest.raises(SchemaValidationError) as exc:
        validate_intent(_output(entity_ref="ftr::dpl_eib.tran_repos"), _candidates())
    message = str(exc.value)
    assert "TABLE" in message and "must be a COLUMN" in message
    assert "Choose from:" in message


def test_the_complaint_lists_valid_choices_but_stays_bounded():
    """Enough to correct the answer; not so many that the complaint becomes a second prompt."""
    many = IntentCandidates(
        column_refs=frozenset(f"ftr::t.c{i}" for i in range(40)),
        table_refs=frozenset({"ftr::t"}))
    with pytest.raises(SchemaValidationError) as exc:
        validate_intent(_output(entity_ref="ftr::t.nope"), many)
    assert str(exc.value).count("ftr::t.c") <= 12
    assert "..." in str(exc.value)


# ── Task 2b: the complaint reaches the model again, value-free ──────────────────────────────────
#
# `validate_intent` raises by hand, so there is no jsonschema `__cause__` for `_safe_reason` to
# rebuild a pointer from, and every one of these seven failures used to collapse into one generic
# constant on the way to the provider — the repair budget bought a differently-shaped prompt with
# nothing in it. Each site now ATTESTS a value-free reason. The attested string is author literal
# plus in-code closed vocabulary ONLY: the interpolated `{ref!r}` in the exception MESSAGE is
# model-supplied text and stays out, and so does the offered-column `sample`.

_MODEL_TEXT = "ftr::dpl_eib.tran_repos.Ahmed_Al_Mansouri"


def _attested(output) -> str:
    from featuregen.intake.llm import _safe_reason

    with pytest.raises(AttestedSchemaValidationError) as exc:
        validate_intent(output, _candidates())
    return _safe_reason(exc.value)


@pytest.mark.parametrize("output,expect", [
    ({"entity_ref": _MODEL_TEXT}, "entity_ref"),
    ({"measure": {"op": "sum", "logical_ref": _MODEL_TEXT}}, "measure.logical_ref"),
    ({"windows": [{"label": "current", "anchor_ref": _MODEL_TEXT,
                   "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0}]},
     "windows[].anchor_ref"),
    ({"dimensions": [{"logical_ref": _MODEL_TEXT}]}, "dimensions[].logical_ref"),
])
def test_every_offered_column_complaint_names_its_field_without_the_ref(output, expect):
    reason = _attested(_output(**output))
    assert reason.startswith(expect)
    assert "column_refs" in reason
    assert "Ahmed_Al_Mansouri" not in reason


def test_the_TABLE_confusion_survives_as_a_shape_not_a_ref():
    """The one the whole task exists for. The message says "that is a TABLE. entity_ref must be a
    COLUMN. Choose from: ..." — actionable, and unsendable, because it interpolates the ref the
    model chose and twelve offered column refs. The attested reason keeps the SHAPE of the
    complaint (a table was named where a column belongs) and drops both."""
    reason = _attested(_output(entity_ref="ftr::dpl_eib.tran_repos"))
    assert reason == ("entity_ref: that is one of the offered table_refs; it must be one of the "
                      "offered column_refs")
    assert "ftr::dpl_eib.tran_repos" not in reason


def test_the_offered_column_sample_never_rides_out_on_the_attested_reason():
    """`sample` is CATALOG text. It already egresses inside `catalog_metadata`, and the repair turn
    re-renders that metadata in full — so re-listing twelve refs in the complaint buys the model
    nothing it is not already holding, while resting the safety of an egress string on an invariant
    of the CALLER (that `candidates` and the egressed `column_refs` are the same set). Dropped."""
    reason = _attested(_output(entity_ref="ftr::dpl_eib.tran_repos.nope"))
    for ref in _COLUMNS:
        assert ref not in reason


def test_the_base_table_complaint_names_its_field_without_the_ref():
    reason = _attested(_output(base_table_ref="ftr::dpl_eib.secret_table"))
    assert reason == "base_table_ref: not one of the offered table_refs"
    assert "secret_table" not in reason


def test_an_unknown_measure_op_is_attested_with_the_in_code_vocabulary():
    """`MEASURE_OPS` is a module constant that ALREADY egresses — `INTENT_SCHEMA` carries it as the
    `measure.op` enum on every call. Author text, not data, so it may ride."""
    reason = _attested(_output(measure={"op": "median", "logical_ref": ""}))
    assert reason.startswith("measure.op: not one of the operations this contract defines")
    assert "'sum'" in reason and "'count_distinct'" in reason
    assert "median" not in reason


def test_an_aggregate_with_no_column_is_attested_as_a_missing_field():
    reason = _attested(_output(measure={"op": "sum", "logical_ref": ""}))
    assert reason == ("measure.logical_ref: required for every op except 'count', which counts "
                      "rows and takes no column")


def test_an_unlabelled_window_is_attested():
    """The only one of the seven whose MESSAGE was already value-free — it interpolates nothing.
    It is attested anyway: the seam is the type, and leaving one site generic because its message
    happened to be safe would make the next author guess."""
    reason = _attested(_output(windows=[
        {"label": "  ", "anchor_ref": "ftr::dpl_eib.tran_repos.tran_month",
         "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0}]))
    assert reason == "windows[].label: every window needs a non-empty label"


def test_an_unknown_comparison_is_attested_with_the_in_code_vocabulary():
    reason = _attested(_output(comparison="plummeted"))
    assert reason.startswith("comparison: not one of the values this contract defines")
    assert "'decrease'" in reason
    assert "plummeted" not in reason


def test_an_unactionable_abstention_code_is_attested_with_the_in_code_vocabulary():
    reason = _attested(_output(unresolved=["dunno about Ahmed Al-Mansouri"]))
    assert reason.startswith("unresolved[]: not an actionable abstention code")
    assert "'population'" in reason
    assert "Ahmed" not in reason


def test_no_attested_reason_from_this_module_carries_model_or_catalog_text():
    """One sweep over all seven, so a NEW site cannot be added without meeting the rule. The probe
    ref is planted in every model-supplied slot at once and must survive nowhere."""
    from featuregen.intake.llm import MAX_ATTESTED_REASON_CHARS, _safe_reason

    probes = [
        _output(entity_ref=_MODEL_TEXT),
        _output(base_table_ref=_MODEL_TEXT),
        _output(measure={"op": _MODEL_TEXT, "logical_ref": ""}),
        _output(measure={"op": "sum", "logical_ref": ""}),
        _output(measure={"op": "sum", "logical_ref": _MODEL_TEXT}),
        _output(windows=[{"label": "", "anchor_ref": "ftr::dpl_eib.tran_repos.tran_month",
                          "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0}]),
        _output(comparison=_MODEL_TEXT),
        _output(unresolved=[_MODEL_TEXT]),
    ]
    for probe in probes:
        with pytest.raises(SchemaValidationError) as exc:
            validate_intent(probe, _candidates())
        assert isinstance(exc.value, AttestedSchemaValidationError), probe
        reason = _safe_reason(exc.value)
        assert "Ahmed_Al_Mansouri" not in reason, reason
        for ref in _COLUMNS | _TABLES:
            assert ref not in reason, reason
        # Un-truncated: `_safe_reason` clips at the bound, so a widened vocabulary would otherwise
        # be silently cut mid-list rather than noticed here.
        assert 0 < len(reason) < MAX_ATTESTED_REASON_CHARS, reason


def test_the_full_message_still_names_the_ref_for_a_human_reading_a_traceback():
    """The attestation ADDS a sendable reason; it does not gut the exception. `str(exc)` is an
    in-process artefact — it reaches no wire, no audit column and no log (traced in the task-2b
    report) — and a developer staring at a traceback wants the ref."""
    with pytest.raises(SchemaValidationError) as exc:
        validate_intent(_output(entity_ref="ftr::dpl_eib.tran_repos"), _candidates())
    message = str(exc.value)
    assert "TABLE" in message and "must be a COLUMN" in message and "Choose from:" in message


def test_the_attested_reason_reaches_a_real_repair_turn(db):
    """End to end through the driver: what the SECOND provider request actually carries."""
    seen: list = []

    class _Capture:
        def __init__(self):
            self._inner = _llm(FakeResponse(output=_output(entity_ref="ftr::dpl_eib.tran_repos")),
                               FakeResponse(output=_output()))

        def call(self, request):
            seen.append(dict(request.inputs))
            return self._inner.call(request)

    got = _extract(db, _Capture())
    assert got.status == STATUS_REPAIRED
    assert seen[1]["_repair_errors"] == [
        "entity_ref: that is one of the offered table_refs; it must be one of the offered "
        "column_refs"]


def test_every_ref_field_in_the_schema_says_whether_it_wants_a_column_or_a_table():
    """The schema was property names and types only. Nothing told the model that `entity_ref` is a
    column and `base_table_ref` is a table, which is exactly the distinction it got wrong."""
    props = INTENT_SCHEMA["properties"]
    assert "COLUMN" in props["entity_ref"]["description"]
    assert "TABLE" in props["base_table_ref"]["description"]
    assert "COLUMN" in props["windows"]["items"]["properties"]["anchor_ref"]["description"]
    assert "COLUMN" in props["dimensions"]["items"]["properties"]["logical_ref"]["description"]


def test_this_package_does_not_import_the_overlay_enrichment_stage():
    """Importing `overlay.upload.enrich_llm` for a five-line helper dragged that module's whole
    graph — and its import-time registrations — into this package, reordering a registry an
    unrelated bridge test asserts on. The suite failed only in FULL runs, and passed in isolation
    and beside its own neighbours, which is the hardest shape of failure to attribute.

    Duplicating five lines beats a dependency that reaches across two subsystems to fetch them."""
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path("src/featuregen/analysis").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                if "overlay.upload.enrich" in module:
                    offenders.append((path.name, module))
    assert not offenders, offenders
