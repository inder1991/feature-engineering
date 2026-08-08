"""Task 9b — the one-off `is_a` parent proposer, and the whole-registry termination proof.

WHAT IS ACTUALLY AT RISK HERE. Nothing in the platform gates on `is_a`: join candidacy, bridge
admission and Pass C all run on `namespace` (verified by the absence of any `.is_a` read outside
`concepts.py` and `attest/concept_critic.py`). The one thing a bad parent CAN do is stop the
application booting — `_validate_registry` fails the import on an unresolved parent or a cycle, and
`concept_path` raises on a cycle it walks. So the proposer's validation is not advisory: it is the
difference between "a slightly odd ancestor in a prompt" and "the process will not start".

These tests therefore pin the four ways a proposal can be wrong (off-registry, self-parent, cycle,
overwriting an existing parent), the joint case two individually-acyclic proposals can create, and
the property that matters most — EVERY concept in the shipped registry, not just the ones this task
touched, walks a terminating chain to a root.
"""
from __future__ import annotations

import pytest

from featuregen.intake.llm import PROVIDER_OK, LLMResult
from featuregen.overlay.upload.concepts import _ALL, CONCEPT_REGISTRY, concept_path
from featuregen.overlay.upload.propose_concept_parents import (
    OFFLINE_PROPOSALS,
    keep_valid,
    offline_proposals,
    parentless_records,
    propose_parents,
    render_patch,
)


class _ScriptedClient:
    """An `LLMClient` that answers with a fixed {concept: parent} map, whatever it is asked.

    Deliberately NOT filtered to the batch: a model answering about a concept nobody asked about is
    one of the failure modes under test.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)
        self.requests: list = []

    def call(self, request) -> LLMResult:
        self.requests.append(request)
        return LLMResult(
            output={"assignments": [{"concept": k, "parent": v}
                                    for k, v in self._mapping.items()]},
            self_reported_scores={}, call_ref="", status=PROVIDER_OK)


def _client_returning(mapping: dict[str, str]) -> _ScriptedClient:
    return _ScriptedClient(mapping)


def _records(*names: str):
    return [CONCEPT_REGISTRY[n] for n in names]


# ── the four single-proposal rejection rules ─────────────────────────────────────────────────────

def test_a_proposed_parent_outside_the_registry_is_dropped() -> None:
    got = propose_parents(_client_returning({"customer_id": "not_a_real_concept"}),
                          _records("customer_id"))
    assert got == {}


def test_a_proposed_parent_that_would_create_a_cycle_is_dropped() -> None:
    """`interest_income is_a monetary_flow` already, so monetary_flow -> interest_income loops."""
    assert CONCEPT_REGISTRY["interest_income"].is_a == "monetary_flow"
    got = propose_parents(_client_returning({"monetary_flow": "interest_income"}),
                          _records("monetary_flow"))
    assert got == {}


def test_self_parenting_is_dropped() -> None:
    got = propose_parents(_client_returning({"customer_id": "customer_id"}),
                          _records("customer_id"))
    assert got == {}


def test_a_concept_that_already_has_a_parent_is_never_re_parented() -> None:
    """The 52 authored parents are identity: a proposal may only ADD, never overwrite."""
    assert CONCEPT_REGISTRY["interest_income"].is_a == "monetary_flow"
    got = propose_parents(_client_returning({"interest_income": "monetary_stock"}),
                          _records("interest_income"))
    assert got == {}


def test_an_answer_about_a_concept_that_was_not_asked_is_dropped() -> None:
    got = propose_parents(_client_returning({"account_id": "customer_id"}),
                          _records("customer_id"))
    assert got == {}


def test_a_valid_parent_is_kept() -> None:
    # Precondition, asserted so a future registry change fails here with an obvious cause rather
    # than mysteriously: neither name may already carry a parent.
    assert CONCEPT_REGISTRY["customer_risk_rating"].is_a is None
    assert "rating" in CONCEPT_REGISTRY
    got = propose_parents(_client_returning({"customer_risk_rating": "rating"}),
                          _records("customer_risk_rating"))
    assert got == {"customer_risk_rating": "rating"}


# ── the joint case: two individually-acyclic proposals that together form a loop ──────────────────

def test_two_proposals_that_jointly_form_a_cycle_keep_only_the_first() -> None:
    assert CONCEPT_REGISTRY["count"].is_a is None
    assert CONCEPT_REGISTRY["quantity_units"].is_a is None
    kept, dropped = keep_valid({"count": "quantity_units", "quantity_units": "count"},
                               _records("count", "quantity_units"))
    assert kept == {"count": "quantity_units"}          # sorted order makes the winner deterministic
    assert dropped == {"quantity_units": "cycle"}


def test_keep_valid_is_deterministic_under_input_ordering() -> None:
    forward = keep_valid({"count": "quantity_units", "quantity_units": "count"},
                         _records("count", "quantity_units"))
    reverse = keep_valid({"quantity_units": "count", "count": "quantity_units"},
                         _records("count", "quantity_units"))
    assert forward == reverse


def test_a_legacy_alias_is_neither_parented_nor_used_as_a_parent() -> None:
    kept, dropped = keep_valid({"monetary_amount": "monetary_stock", "count": "rate_or_ratio"},
                               _records("monetary_amount", "count"))
    assert kept == {}
    assert dropped == {"monetary_amount": "legacy_alias", "count": "parent_is_legacy_alias"}


def test_a_blank_or_none_answer_is_a_normal_no_parent_outcome() -> None:
    kept, dropped = keep_valid({"count": "", "quantity_units": "none"},
                               _records("count", "quantity_units"))
    assert kept == {}
    assert dropped == {"count": "no_parent_offered", "quantity_units": "no_parent_offered"}


# ── batching: the whole parentless set is asked about, in bounded chunks ──────────────────────────

def test_every_parentless_record_is_asked_about_in_bounded_batches() -> None:
    records = list(parentless_records())
    assert len(records) > 40
    client = _client_returning({})
    propose_parents(client, records)
    asked: list[str] = []
    for request in client.requests:
        batch = request.inputs["catalog_metadata"]["concepts"]
        assert 0 < len(batch) <= 40
        asked.extend(c["name"] for c in batch)
    assert asked == [c.name for c in records]


def test_the_request_carries_registry_metadata_only() -> None:
    """No catalog, no column, no sample value ever reaches the provider — the tool reads a Python
    module, so there is nothing customer-owned to leak."""
    client = _client_returning({})
    propose_parents(client, _records("customer_id"))
    metadata = client.requests[0].inputs["catalog_metadata"]
    assert set(metadata) == {"concepts", "candidate_parents"}
    assert metadata["concepts"] == [
        {"name": "customer_id", "group": "identifier",
         "hint": CONCEPT_REGISTRY["customer_id"].description.split(".")[0].strip()[:150]}]


# ── the deterministic offline path (no provider call) ────────────────────────────────────────────

def test_the_offline_proposals_all_survive_validation() -> None:
    """Every curated pair must be a legal proposal in its own right — the offline path is held to
    the SAME validator as a provider answer, not trusted because a human wrote it."""
    kept, dropped = keep_valid(OFFLINE_PROPOSALS, _ALL)
    already = {n for n in OFFLINE_PROPOSALS if CONCEPT_REGISTRY[n].is_a is not None}
    assert dropped == {n: "already_parented" for n in already}
    assert set(kept) == set(OFFLINE_PROPOSALS) - already


def test_the_offline_path_is_idempotent_once_applied() -> None:
    """After the patch has landed in `concepts.py`, re-running the offline proposer must propose
    NOTHING — that is the proof the emitted patch was applied in full.

    The REASON matters as much as the count: a re-run must report "already_parented", not
    "not_asked". Once a name has a parent it drops out of `parentless_records()`, and reporting the
    absence rather than the parent would read as the source misbehaving instead of as a completed
    backfill.
    """
    records = parentless_records()
    assert offline_proposals(records) == {}
    _, dropped = keep_valid(OFFLINE_PROPOSALS, records)
    assert set(dropped.values()) == {"already_parented"}
    assert len(dropped) == len(OFFLINE_PROPOSALS)


def test_render_patch_emits_an_applicable_is_a_argument() -> None:
    patch = render_patch({"customer_risk_rating": "rating"})
    assert 'customer_risk_rating' in patch
    assert 'is_a="rating"' in patch


# ── the whole-registry property: every ancestry terminates at a root ──────────────────────────────

def test_every_concept_in_the_registry_walks_a_terminating_chain_to_a_root() -> None:
    for record in _ALL:
        path = concept_path(record.name)
        assert path[0] == record.name, record.name
        assert len(path) == len(set(path)), (record.name, path)      # no repeat => no cycle
        assert CONCEPT_REGISTRY[path[-1]].is_a is None, (record.name, path)
        for name in path:
            assert name in CONCEPT_REGISTRY, (record.name, name)


def test_no_concept_is_parented_to_a_legacy_alias() -> None:
    from featuregen.overlay.upload.concepts import _LEGACY_ALIASES
    for record in _ALL:
        assert record.is_a not in _LEGACY_ALIASES, (record.name, record.is_a)


def test_the_shipped_registry_still_validates_at_import() -> None:
    from featuregen.overlay.upload.concepts import _validate_registry
    _validate_registry()


def test_is_a_coverage_moved_off_the_authored_52() -> None:
    """The measure of what this task bought. A floor, not an exact count, so a later hand-authored
    parent does not fail an unrelated test."""
    parented = [c for c in _ALL if c.is_a is not None]
    assert len(parented) >= 140, len(parented)


def test_the_join_candidacy_axis_is_untouched() -> None:
    """`namespace` is the ONLY join-candidacy gate. Adding ancestry must not have moved one."""
    identifiers = {c.name: c.namespace for c in _ALL if c.group == "identifier"}
    assert identifiers["customer_id"] == identifiers["counterparty_id"] == "cif"
    assert identifiers["external_account_ref"] != identifiers["account_id"]
    assert all(ns for ns in identifiers.values())
    assert all(c.namespace is None for c in _ALL if c.group != "identifier")


def test_a_registry_with_a_pre_existing_cycle_still_terminates_the_walk() -> None:
    """`_would_cycle` must not loop forever against a corrupt chain — it is the thing that has to
    keep working when the registry is already wrong."""
    from featuregen.overlay.upload.propose_concept_parents import _would_cycle
    accepted = {"a": "b", "b": "a"}
    assert _would_cycle("z", "a", accepted) is True


def test_concept_path_stays_a_single_element_for_a_root() -> None:
    assert concept_path("monetary_stock") == ("monetary_stock",)
    with pytest.raises(KeyError):
        CONCEPT_REGISTRY["definitely_not_a_concept"]
