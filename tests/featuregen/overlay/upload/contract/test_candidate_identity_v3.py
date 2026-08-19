"""The canonical candidate identity covers what a candidate COMPUTES, not just how it reads.

THE DEFECT THIS CLOSES. `_idea_json` emitted the feature's description — name, derives_from, an
aggregation STRING, a grain TABLE — and dropped the entire typed computation: operation_kind,
measure_refs, grain_ref, time_ref, window, grouping_refs. All six. So the identity a candidate was
sealed under did not say what it computes, and two candidates aggregating different measures over
different windows shared an identity whenever their name, derives_from and aggregation matched.

Downstream, `_idea_from_json` restored ideas with an empty typed block, which made the draft
worker's grain branch dead code: every formula was authored against a grain listing every column it
derived from rather than the one it is computed per. It was invisible because it was CONSISTENT —
the intent hash covered the wrong value and every re-derivation agreed.

These tests are the floor under that: change what a candidate computes, and its identity moves.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.contract.gate1 import (
    _candidate_identity,
    _idea_from_json,
    _idea_json,
)
from featuregen.overlay.upload.feature_assist import FeatureIdea

BASE = dict(
    name="posted_debit_amount_90d", description="recent debit volume",
    derives_from=["public.txns.txn_amt"],
    derives_pairs=(("cib", "public.txns.txn_amt"),),
    aggregation="sum", grain_table="account",
    operation_kind="sum",
    measure_refs=(("cib", "public.txns.txn_amt"),),
    grain_refs=(("cib", "public.txns.cif_id"),),
    time_ref=("cib", "public.txns.txn_dt"),
    window="90d",
    grouping_refs=(),
)

#: Every field whose movement changes WHAT IS COMPUTED. Parametrized so the rule holds for all six
#: rather than for whichever one somebody thought of.
EXECUTION_BEARING = [
    ("operation_kind", "avg"),
    ("measure_refs", (("cib", "public.txns.fee_amt"),)),
    ("grain_refs", (("cib", "public.txns.product_id"),)),
    ("time_ref", ("cib", "public.txns.posted_dt")),
    ("window", "30d"),
    ("grouping_refs", (("cib", "public.txns.channel"),)),
]


def _identity_of(**overrides) -> str:
    idea = FeatureIdea(**{**BASE, **overrides})
    return canonical_hash(
        _candidate_identity(path="anchor", source="anchor", lens="anchor", feature=idea))


# ══ EVERY EXECUTION-BEARING FIELD MOVES THE IDENTITY ═══════════════════════════════════════════
@pytest.mark.parametrize(("field", "changed"), EXECUTION_BEARING)
def test_CHANGING_WHAT_IT_COMPUTES_CHANGES_ITS_IDENTITY(field, changed):
    """Each of the six, separately.

    Before this, all six were absent from the serialized form, so every one of these assertions
    would have failed — two candidates computing different things hashed identically.
    """
    assert _identity_of() != _identity_of(**{field: changed}), (
        f"{field} moved and the candidate identity did not: two candidates computing different "
        f"things would share an identity")


def test_a_MULTI_KEY_GRAIN_is_not_the_same_as_its_first_key():
    """Order and arity both matter: a feature per (customer, product) is not a feature per customer,
    and the singular representation could not tell them apart."""
    one = _identity_of(grain_refs=(("cib", "public.txns.cif_id"),))
    two = _identity_of(grain_refs=(("cib", "public.txns.cif_id"),
                                   ("cib", "public.txns.product_id")))
    assert one != two


def test_GRAIN_KEY_ORDER_IS_PART_OF_THE_IDENTITY():
    """The order of grain keys decides the published column order, so two orders are two features."""
    forward = _identity_of(grain_refs=(("cib", "a"), ("cib", "b")))
    reverse = _identity_of(grain_refs=(("cib", "b"), ("cib", "a")))
    assert forward != reverse


# ══ THE ROUND TRIP IS LOSSLESS ═════════════════════════════════════════════════════════════════
def test_EVERY_EXECUTION_BEARING_FIELD_SURVIVES_THE_ROUND_TRIP():
    """`_idea_json` and `_idea_from_json` are a pair, and a field written by one and not read by the
    other is the same defect as a field neither handles."""
    idea = FeatureIdea(**BASE)
    restored = _idea_from_json(_idea_json(idea))
    for field, _changed in EXECUTION_BEARING:
        assert getattr(restored, field) == getattr(idea, field), field


def test_the_singular_grain_ref_is_DERIVED_and_cannot_disagree():
    """`grain_ref` is a display convenience over `grain_refs`, not a second stored value.

    Two stored representations would disagree the first time something set one and read the other —
    which is precisely how the original defect stayed invisible.
    """
    idea = FeatureIdea(**{**BASE, "grain_refs": (("cib", "first"), ("cib", "second"))})
    assert idea.grain_ref == ("cib", "first")
    assert not hasattr(type(idea), "__dataclass_fields__") or (
        "grain_ref" not in type(idea).__dataclass_fields__), "grain_ref must not be a stored field"


# ══ A PRE-REGENERATION CANDIDATE IS READABLE AND NOT EXECUTABLE ════════════════════════════════
def test_an_OLD_SNAPSHOT_deserializes_with_an_EMPTY_typed_block():
    """Which is correct, and is what the regeneration-required refusal detects.

    A v2 snapshot carries none of these keys. Restoring it must not invent values — an absent
    execution field is not a default, it is a candidate that cannot be executed.
    """
    old = {"name": "f", "description": "d", "derives_from": ["public.t.c"],
           "aggregation": "sum", "grain_table": "account", "derives_pairs": []}
    restored = _idea_from_json(old)
    assert restored.grain_refs == ()
    assert restored.operation_kind == ""
    assert restored.measure_refs == ()
    assert restored.time_ref is None
    assert restored.window is None
