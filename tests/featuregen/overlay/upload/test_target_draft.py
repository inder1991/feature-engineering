"""The proposed draft: what the tool fills in, what it leaves blank, and why."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_draft import (
    NEEDS_INPUT_REASONS,
    DraftError,
    TargetDraftV1,
)


def _draft(**over) -> TargetDraftV1:
    base = dict(
        shape="state_change",
        fields={"name": "tgt_npe_90d", "entity": "customer", "anchor_catalog": "cib",
                "grain_ref": "public.bo_cib_customer.cust_num",
                "as_of_ref": "public.bo_cib_customer.business_dt",
                "window_days": 90, "as_of_frequency": "monthly", "label_type": "binary",
                "column_ref": "public.bo_cib_customer.cust_perf_nonperf_flg"},
        needs_input=("from_values", "to_values"),
        notes={"from_values": "no_value_profile", "to_values": "no_value_profile"})
    return TargetDraftV1(**{**base, **over})


def test_a_draft_may_be_INCOMPLETE_where_a_rule_may_not():
    """The whole reason this type exists: the tool cannot know the flag's values, so it must be
    able to hand back a form with those fields blank rather than guessing them."""
    d = _draft()
    assert "from_values" in d.needs_input
    assert "from_values" not in d.fields


def test_a_field_cannot_be_both_FILLED_and_NEEDED():
    """That contradiction is how a guessed value gets rendered as if a person supplied it."""
    with pytest.raises(DraftError, match="both"):
        _draft(needs_input=("window_days", "from_values", "to_values"),
               notes={"window_days": "not_stated", "from_values": "no_value_profile",
                      "to_values": "no_value_profile"})


def test_every_needed_field_must_say_WHY_it_is_needed():
    """A blank with no reason is indistinguishable from a bug, and gets filled in carelessly."""
    with pytest.raises(DraftError, match="reason"):
        TargetDraftV1(shape="state_change", fields={}, needs_input=("from_values",), notes={})


def test_the_reason_must_be_one_the_form_can_render():
    with pytest.raises(DraftError, match="reason"):
        TargetDraftV1(shape="state_change", fields={}, needs_input=("from_values",),
                      notes={"from_values": "because I said so"})


def test_the_shape_is_closed():
    with pytest.raises(DraftError, match="shape"):
        _draft(shape="whatever")


def test_a_complete_draft_needs_nothing():
    complete = dict(_draft().fields)
    complete |= {"from_values": ["Performing"], "to_values": ["Non-performing"]}
    assert TargetDraftV1(shape="state_change", fields=complete).needs_input == ()


def test_a_frozen_draft_cannot_be_mutated_through_its_dicts():
    """`frozen=True` protects the attributes, not the dicts they point at. Copying on construction
    makes the guarantee real rather than advertised."""
    supplied = dict(_draft().fields)
    supplied |= {"from_values": ["Performing"], "to_values": ["Non-performing"]}
    draft = TargetDraftV1(shape="state_change", fields=supplied)
    supplied["name"] = "tampered"
    assert draft.fields["name"] == "tgt_npe_90d"


# ══ the proposer ═════════════════════════════════════════════════════════════════════════════════

from featuregen.intake.llm import FakeLLM, FakeResponse  # noqa: E402
from featuregen.overlay.upload.canonical import CanonicalRow  # noqa: E402
from featuregen.overlay.upload.enrich import content_hash  # noqa: E402
from featuregen.overlay.upload.graph import build_graph  # noqa: E402
from featuregen.overlay.upload.target_draft import (  # noqa: E402
    TARGET_DRAFT_TASK,
    propose_target_draft,
)

CIB = "cib"
_GRAIN = "public.customers.cust_num"
_ASOF = "public.customers.business_dt"
_FLAG = "public.customers.perf_flg"


def _catalog(db):
    rows = [
        (CanonicalRow(CIB, "customers", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(CIB, "customers", "business_dt", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow(CIB, "customers", "perf_flg", "text"), "npe_flag"),
    ]
    build_graph(db, CIB, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})


def _client(output: dict) -> FakeLLM:
    return FakeLLM(script={TARGET_DRAFT_TASK: FakeResponse(output=output)})


def _propose(db, client):
    return propose_target_draft(
        db, client, hypothesis="which customers go non-performing in 90 days",
        entity="customer", catalog_source=CIB, grain_ref=_GRAIN, as_of_ref=_ASOF,
        roles=("data_owner",))


def test_a_draft_comes_back_with_its_blanks_and_reasons(db):
    _catalog(db)
    draft = _propose(db, _client({
        "shape": "state_change",
        "fields": {"name": "tgt_npe_90d", "column_ref": _FLAG, "window_days": 90,
                   "as_of_frequency": "monthly", "label_type": "binary"},
        "needs_input": ["from_values", "to_values"],
        "notes": {"from_values": "no_value_profile", "to_values": "no_value_profile"}}))
    assert draft.shape == "state_change"
    assert "from_values" in draft.needs_input
    assert draft.fields["column_ref"] == _FLAG


def test_the_chosen_entitys_SPINE_is_stamped_on_never_guessed(db):
    """The person picked the entity and `selectable_entities` already returns its spine. Letting
    the model guess a grain already chosen only invites a disagreement the grain check rejects,
    with the person unable to see why."""
    _catalog(db)
    draft = _propose(db, _client({
        "shape": "state_change",
        "fields": {"name": "tgt_npe_90d", "column_ref": _FLAG, "window_days": 90,
                   "as_of_frequency": "monthly", "label_type": "binary",
                   "from_values": ["P"], "to_values": ["N"],
                   "grain_ref": "public.customers.something_else"},
        "needs_input": [], "notes": {}}))
    assert draft.fields["grain_ref"] == _GRAIN
    assert draft.fields["as_of_ref"] == _ASOF
    assert draft.fields["entity"] == "customer"


def test_a_ref_the_model_INVENTED_is_dropped_and_becomes_a_blank(db):
    """Selection, never generation — the intake ticket's rule, applied here. Repairing it would
    let the model name a column that is not there and have the platform agree."""
    _catalog(db)
    draft = _propose(db, _client({
        "shape": "state_change",
        "fields": {"name": "tgt_npe_90d", "column_ref": "public.customers.invented",
                   "window_days": 90, "as_of_frequency": "monthly", "label_type": "binary",
                   "from_values": ["P"], "to_values": ["N"]},
        "needs_input": [], "notes": {}}))
    assert "column_ref" not in draft.fields
    assert "column_ref" in draft.needs_input
    assert draft.notes["column_ref"] == "not_in_catalog", \
        "the blank must say the column is absent, not that its VALUES are unprofiled"


def test_an_unstated_horizon_comes_back_as_a_BLANK_not_a_default(db):
    """`as_of_frequency` is the one mandatory field with no safe default, so "the model omitted it"
    is the case most worth pinning: it must arrive as a justified blank, never quietly filled."""
    _catalog(db)
    draft = _propose(db, _client({
        "shape": "state_change",
        "fields": {"name": "tgt_npe_90d", "column_ref": _FLAG, "window_days": 90,
                   "label_type": "binary", "from_values": ["P"], "to_values": ["N"]},
        "needs_input": ["as_of_frequency"], "notes": {"as_of_frequency": "not_stated"}}))
    assert "as_of_frequency" in draft.needs_input
    assert "as_of_frequency" not in draft.fields


def test_no_client_returns_nothing_rather_than_a_fabricated_draft(db):
    _catalog(db)
    assert _propose(db, None) is None


def test_a_body_that_contradicts_itself_returns_nothing(db):
    """Filled AND needed is refused by the draft type; the proposer must not paper over it."""
    _catalog(db)
    assert _propose(db, _client({
        "shape": "state_change", "fields": {"window_days": 90},
        "needs_input": ["window_days"], "notes": {"window_days": "not_stated"}})) is None


def test_a_draft_must_ACCOUNT_FOR_every_field_its_shape_needs():
    """Found by the first real model call: it returned `shape: state_change` and nothing else —
    no column, no window, no name — with an EMPTY needs_input. That passed validation, because
    the type only checked that blanks carry reasons, never that the shape was covered. The form
    would then render everything blank with no explanation for any of it, which is the exact
    failure this type exists to prevent."""
    with pytest.raises(DraftError, match="accounted for"):
        TargetDraftV1(shape="state_change",
                      fields={"entity": "customer", "anchor_catalog": "cib",
                              "grain_ref": "public.t.k", "as_of_ref": "public.t.d"})


def test_a_field_is_accounted_for_by_being_BLANK_with_a_reason():
    """Covered does not mean filled — a justified blank is a complete answer."""
    draft = TargetDraftV1(
        shape="state_change",
        fields={"entity": "customer", "anchor_catalog": "cib", "grain_ref": "public.t.k",
                "as_of_ref": "public.t.d", "name": "tgt_npe_90d", "window_days": 90,
                "as_of_frequency": "monthly", "label_type": "binary", "operator": ">=",
                "threshold": 1, "column_ref": "public.t.flag"},
        needs_input=("from_values", "to_values"),
        notes={"from_values": "no_value_profile", "to_values": "no_value_profile"})
    assert draft.needs_input == ("from_values", "to_values")


def test_an_event_window_draft_is_measured_against_ITS_OWN_fields():
    """The two shapes need different keys; a state_change field list must not satisfy an
    event_window draft."""
    with pytest.raises(DraftError, match="accounted for"):
        TargetDraftV1(shape="event_window",
                      fields={"entity": "customer", "anchor_catalog": "cib",
                              "grain_ref": "public.t.k", "as_of_ref": "public.t.d",
                              "name": "tgt_x_90d", "window_days": 90,
                              "as_of_frequency": "monthly", "label_type": "binary",
                              "operator": ">=", "threshold": 1,
                              "column_ref": "public.t.flag"})


# ══ the schema is what actually steers the model ═════════════════════════════════════════════════

from featuregen.overlay.upload.enrich_llm import _SCHEMAS  # noqa: E402
from featuregen.overlay.upload.target_draft import (  # noqa: E402
    SHAPE_FIELDS,
    TARGET_DRAFT_SCHEMA_ID,
    TARGET_DRAFT_SCHEMA_VERSION,
)


def test_the_dispatched_schema_DECLARES_every_field_it_expects():
    """Found by a live call, twice. The INSTRUCTION is not what constrains structured output — the
    schema is. `fields` was declared as an open object with no properties, which makes `{}` both
    the easiest answer and a perfectly valid one, and the model returned exactly that both times
    while `validation_result` said "ok".

    Naming the fields in the prompt did not change it and could not have.
    """
    schema = _SCHEMAS[(TARGET_DRAFT_SCHEMA_ID, TARGET_DRAFT_SCHEMA_VERSION)]
    declared = set(schema["properties"]["fields"].get("properties", {}))
    for shape, needed in SHAPE_FIELDS.items():
        missing = [f for f in needed if f not in declared]
        assert not missing, f"{shape} needs {missing}, which the schema never mentions"


def test_the_schema_still_ACCEPTS_the_stamped_fields_the_model_may_echo():
    """The instruction tells it not to supply these, but a closed schema that REFUSES them would
    turn a harmless echo into a failed call. They are overwritten server-side anyway."""
    schema = _SCHEMAS[(TARGET_DRAFT_SCHEMA_ID, TARGET_DRAFT_SCHEMA_VERSION)]
    declared = set(schema["properties"]["fields"].get("properties", {}))
    assert {"entity", "anchor_catalog", "grain_ref", "as_of_ref"} <= declared


def test_the_schema_constrains_a_BLANKS_REASON_to_the_closed_set():
    """An unrecognised reason renders as a blank with no explanation. Constraining it in the schema
    lets the bounded repair loop fix it, rather than the draft being discarded whole afterwards."""
    schema = _SCHEMAS[(TARGET_DRAFT_SCHEMA_ID, TARGET_DRAFT_SCHEMA_VERSION)]
    notes = schema["properties"]["notes"]["additionalProperties"]
    assert set(notes["enum"]) == set(NEEDS_INPUT_REASONS)


def test_version_1_stays_BYTE_FROZEN():
    """Two live llm_call rows were produced under v1. It is the contract they were produced under,
    so it neither changes nor goes away — the same rule `use_case_recognition` v1 follows."""
    assert _SCHEMAS[("target_draft", 1)] == {
        "type": "object", "additionalProperties": False,
        "properties": {
            "shape": {"type": "string", "enum": ["state_change", "event_window"]},
            "fields": {"type": "object", "additionalProperties": True},
            "needs_input": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "object", "additionalProperties": True}},
        "required": ["shape", "fields"]}
