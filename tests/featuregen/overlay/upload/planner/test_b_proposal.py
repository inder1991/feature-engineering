"""Phase 3C.2b-i-B · Task 2 — ``RawFeatureProposalV1``: lossless capture.

Pins: (1) the raw operand refs, operation, window, and grain hint are captured VERBATIM and
order-preserved — this is the "pre-`_vet` operand set verbatim" guarantee a later task (T4) diffs
the vetted idea against to detect any dropped/rewritten operand (``proposal_lossy``); (2) the
dataclass is frozen+slotted (immutable); (3) ``operands`` is always stored as a ``tuple``, even when
constructed from a list; (4) ``operation``/``window``/``grain_hint`` are legally ``None`` (the window
is captured-not-consumed — a later task decides RECENCY/TREND, deferred here); (5)
``new_raw_proposal`` stamps ``version=RAW_PROPOSAL_VERSION`` without the caller hand-passing it."""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.overlay.upload.planner import b_proposal as b


def test_round_trip_verbatim_capture_with_tuple_conversion_and_order_preservation():
    proposal = b.new_raw_proposal(
        operands=["ftr.tran_amt", "ftr.cif_id"],
        operation="sum",
        window="90d",
        grain_hint="customer",
    )
    assert proposal.operands == ("ftr.tran_amt", "ftr.cif_id")
    assert isinstance(proposal.operands, tuple)
    assert proposal.operation == "sum"
    assert proposal.window == "90d"
    assert proposal.grain_hint == "customer"
    assert proposal.version == b.RAW_PROPOSAL_VERSION


def test_single_operand_tuple_preserved_verbatim():
    proposal = b.new_raw_proposal(
        operands=("ftr.tran_amt",),
        operation="sum",
        window="90d",
        grain_hint="customer",
    )
    assert proposal.operands == ("ftr.tran_amt",)
    assert isinstance(proposal.operands, tuple)


def test_instance_is_frozen():
    proposal = b.new_raw_proposal(
        operands=("ftr.tran_amt",), operation="sum", window="90d", grain_hint="customer")
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.operation = "avg"  # type: ignore[misc]


def test_optional_fields_accept_and_preserve_none():
    proposal = b.new_raw_proposal(
        operands=("ftr.tran_amt",), operation=None, window=None, grain_hint=None)
    assert proposal.operation is None
    assert proposal.window is None
    assert proposal.grain_hint is None
    assert proposal.version == b.RAW_PROPOSAL_VERSION


def test_dataclass_constructed_directly_also_pins_the_version():
    proposal = b.RawFeatureProposalV1(
        operands=("ftr.tran_amt",), operation="sum", window="90d", grain_hint="customer",
        version=b.RAW_PROPOSAL_VERSION)
    assert proposal.version == b.RAW_PROPOSAL_VERSION
