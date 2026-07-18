"""MF-3: a parseable no-grain/no-as-of synthesis is a VALID ABSTENTION, not a stage failure.

Some tables genuinely have no single grain / as-of. Pass B used to reject such a synthesis as
``empty_synthesis`` -> ``None``, discarding even a valid ``table_role`` / ``primary_entity`` it
returned; the table then vanished from ``syntheses`` and was counted UNRESOLVED, dragging the whole
Pass B stage to ``failed`` / ``partial``. Now ``make_ref_accept`` accepts it (retaining its advisory
role/entity, proposing zero grain/availability facts) and ``_enrichment_outcome`` counts it as
resolved-but-abstained so the stage reports ``succeeded`` with an ``abstained`` count.

The GENUINELY-invalid paths (unparseable / non-object) still reject with ``None``.
"""
import json

from featuregen.overlay.upload.ingest import _enrichment_outcome
from featuregen.overlay.upload.table_synth import make_ref_accept


def test_role_entity_only_synthesis_is_accepted_as_abstention():
    # `make_ref_accept` takes {table: columns}; the accept is called `accept(raw, ref)`.
    accept = make_ref_accept({"t": {"a", "b"}})
    payload = json.dumps({"grain_columns": [], "as_of_column": None,
                          "table_role": "reference", "primary_entity": "customer"})
    value, reason = accept(payload, "t")
    assert value is not None                       # accepted (was empty_synthesis -> None before)
    assert reason == "abstained"
    out = json.loads(value)
    assert out["grain"] is None and out["availability_time"] is None
    assert out["table_role"] == "reference" and out["primary_entity"] == "customer"


def test_a_real_grain_still_reports_valid_not_abstained():
    accept = make_ref_accept({"t": {"a", "b"}})
    payload = json.dumps({"grain_columns": ["a"]})
    value, reason = accept(payload, "t")
    assert value is not None and reason == "valid"
    assert json.loads(value)["grain"] == {"columns": ["a"], "is_unique": True}


def test_unparseable_is_still_rejected():
    accept = make_ref_accept({"t": {"a"}})
    value, reason = accept("not json", "t")
    assert value is None and reason == "unparseable"


def test_non_object_json_is_still_rejected():
    # "null" / "[]" / '"x"' parse fine but cannot .get(...) — a genuine reject, not an abstention.
    accept = make_ref_accept({"t": {"a"}})
    assert accept("null", "t") == (None, "not_object")
    assert accept("[]", "t") == (None, "not_object")


def test_outcome_counts_abstention_as_resolved():
    syntheses = {"t1": {"grain": {"columns": ["id"], "is_unique": True}, "availability_time": None},
                 "t2": {"grain": None, "availability_time": None, "table_role": "reference"}}
    state, reason, detail = _enrichment_outcome(syntheses, 2)
    assert state == "succeeded"
    assert detail["resolved"] == 2
    assert detail["abstained"] == 1


def test_string_valued_stage_never_miscounts_or_crashes():
    # `_enrichment_outcome` is shared by string-valued stages (concept/definition/domain). Their
    # values are NOT dicts, so they must never be scanned for grain/as-of (no `.get` on a str) and
    # never gain a spurious `abstained` key.
    state, reason, detail = _enrichment_outcome({"h1": "money", "h2": "date"}, 2)
    assert state == "succeeded"
    assert "abstained" not in detail
