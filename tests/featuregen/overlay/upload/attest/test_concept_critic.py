"""The refute-oriented enrichment concept critic (ingestion-richness Task 2).

Mirrors ``test_bridge_critic``'s mocking style exactly: a task-key-scripted ``FakeLLM`` plays the
provider, the real audited dispatch seam and the real ``1039`` structured-result store run
underneath. The critic's contract under test:

* a deterministic ``shape_conflicts`` contradiction refutes WITHOUT asking the model whether the
  assignment is supported (and refutes even with NO client at all);
* the model may uphold (``supported``), refute, or abstain (``uncertain``) — but its one revise
  pass can only land on a registry concept that itself survives the shape checks, so an
  off-registry answer is never emitted;
* identical inputs replay the stored validated result without a provider call.
"""
from __future__ import annotations

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.attest.concept_critic import (
    CONCEPT_CRITIC_RUN_ID,
    CONCEPT_CRITIC_TASK,
    CONCEPT_REVISION_TASK,
    ConceptCriticItemV1,
    ConceptDisposition,
    critique_concept_batch,
)
from featuregen.overlay.upload.structured_results import load_structured_result


def _item(
    *,
    ref: str = "cib::dpl_eib_compliance.bo_cib_customer.cif_id",
    column: str = "cif_id",
    declared: str | None = "varchar(20)",
    definition: str | None = "Customer CIF",
    concept: str = "customer_id",
) -> ConceptCriticItemV1:
    return ConceptCriticItemV1(
        logical_ref=ref,
        column_name=column,
        declared_type=declared,
        definition=definition,
        proposed_concept=concept,
    )


def _desc_item() -> ConceptCriticItemV1:
    # The live defect the critic exists for: a branch DESCRIPTION classified as branch_id.
    return _item(
        ref="cib::dpl_eib_compliance.bo_cib_customer.sol_desc",
        column="sol_desc",
        declared="varchar(255)",
        definition="Branch description",
        concept="branch_id",
    )


def _supported_client() -> FakeLLM:
    return FakeLLM(script={
        CONCEPT_CRITIC_TASK: FakeResponse(output={
            "verdict": "supported",
            "reason_codes": ["definition names the customer master identifier"],
        }),
    })


def _llm_calls(db, task: str | None = None) -> int:
    if task is None:
        return db.execute(
            "SELECT count(*) FROM llm_call WHERE run_id=%s", (CONCEPT_CRITIC_RUN_ID,),
        ).fetchone()[0]
    return db.execute(
        "SELECT count(*) FROM llm_call WHERE run_id=%s AND task=%s",
        (CONCEPT_CRITIC_RUN_ID, task),
    ).fetchone()[0]


def test_description_column_is_refuted_citing_the_deterministic_conflict(db) -> None:
    # (a) — the revise pass runs (scripted to withdraw), the critique question is NEVER dispatched
    # for a deterministically-conflicted item (an unscripted critique task would raise).
    client = FakeLLM(script={
        CONCEPT_REVISION_TASK: FakeResponse(output={"concept": "unclassified",
                                                    "reason_codes": []}),
    })
    item = _desc_item()
    result = critique_concept_batch(db, client, [item], catalog_revision="r1")[item.logical_ref]
    assert result.disposition is ConceptDisposition.REFUTED
    assert result.resolved_concept is None
    assert "name_or_description_not_identifier" in result.conflict_codes
    assert "deterministic_shape_conflict" in result.reason_codes
    assert _llm_calls(db, CONCEPT_CRITIC_TASK) == 0
    assert _llm_calls(db, CONCEPT_REVISION_TASK) == 1


def test_refuted_bic_shape_can_be_revised_into_the_bank_namespace(db) -> None:
    client = FakeLLM(script={
        CONCEPT_REVISION_TASK: FakeResponse(output={"concept": "bank_bic",
                                                    "reason_codes": ["bic identifies the bank"]}),
    })
    item = _item(
        ref="ftr::real.comp_financial_tran_repos_dly.counter_party_bic",
        column="counter_party_bic",
        declared="varchar(11)",
        definition="SWIFT BIC of the counterparty bank",
        concept="counterparty_id",
    )
    result = critique_concept_batch(db, client, [item], catalog_revision="r1")[item.logical_ref]
    assert result.disposition is ConceptDisposition.REVISED
    assert result.resolved_concept == "bank_bic"
    assert "identifier_namespace_mismatch" in result.conflict_codes
    assert "revision_accepted" in result.reason_codes


def test_clean_cif_assignment_is_upheld(db) -> None:
    # (b)
    item = _item()
    result = critique_concept_batch(
        db, _supported_client(), [item], catalog_revision="r1")[item.logical_ref]
    assert result.disposition is ConceptDisposition.ACCEPTED
    assert result.resolved_concept == "customer_id"
    assert result.conflict_codes == ()
    assert result.structured_result_id is not None
    stored = load_structured_result(db, result.structured_result_id)
    assert stored is not None
    assert stored.output["disposition"] == "accepted"


def test_genuinely_ambiguous_assignment_abstains(db) -> None:
    # (c) — abstention keeps the proposal standing; the critic is refute-oriented, so the absence
    # of support is not itself an eviction.
    client = FakeLLM(script={
        CONCEPT_CRITIC_TASK: FakeResponse(output={"verdict": "uncertain",
                                                  "reason_codes": ["evidence cannot decide"]}),
    })
    item = _item(
        ref="cib::dpl_eib_compliance.bo_cib_customer.module_id",
        column="module_id",
        declared="varchar(10)",
        definition="Identifier",
        concept="category_code",
    )
    # category_code is not an identifier concept -> no deterministic conflict; the model abstains.
    result = critique_concept_batch(db, client, [item], catalog_revision="r1")[item.logical_ref]
    assert result.disposition is ConceptDisposition.ABSTAINED
    assert result.resolved_concept == "category_code"


def test_a_legacy_alias_revision_canonicalizes_and_is_never_stored_raw(db) -> None:
    # The revise pass routes through canonical_concept_name exactly like Pass A's _accept_concept:
    # a revise answer of counterparty_id (a legacy alias, still a registry member) lands as
    # customer_id — in the typed result AND in the replay store, never raw.
    client = FakeLLM(script={
        CONCEPT_CRITIC_TASK: FakeResponse(output={
            "verdict": "refuted",
            "reason_codes": ["a category code does not identify the counterparty CIF"]}),
        CONCEPT_REVISION_TASK: FakeResponse(output={"concept": "counterparty_id",
                                                    "reason_codes": []}),
    })
    item = _item(
        ref="ftr::real.comp_financial_tran_repos_dly.counter_party_cif_id",
        column="counter_party_cif_id",
        declared="varchar(20)",
        definition="Counterparty CIF",
        concept="category_code",
    )
    result = critique_concept_batch(db, client, [item], catalog_revision="r1")[item.logical_ref]
    assert result.disposition is ConceptDisposition.REVISED
    assert result.resolved_concept == "customer_id"
    assert "revision_accepted" in result.reason_codes
    stored = load_structured_result(db, result.structured_result_id)
    assert stored is not None
    assert stored.output["resolved_concept"] == "customer_id"


def test_an_alias_revision_never_resurrects_its_own_refuted_successor(db) -> None:
    # MUTATION (difference gate compares CANONICAL forms): customer_id was refuted; answering its
    # legacy alias counterparty_id is the SAME selection restated, never a "different" revision.
    client = FakeLLM(script={
        CONCEPT_CRITIC_TASK: FakeResponse(output={
            "verdict": "refuted", "reason_codes": ["not supported"]}),
        CONCEPT_REVISION_TASK: FakeResponse(output={"concept": "counterparty_id",
                                                    "reason_codes": []}),
    })
    item = _item(concept="customer_id")
    result = critique_concept_batch(db, client, [item], catalog_revision="r1")[item.logical_ref]
    assert result.disposition is ConceptDisposition.REFUTED
    assert result.resolved_concept is None
    assert "revision_rejected" in result.reason_codes


def test_an_off_registry_revision_is_never_emitted(db) -> None:
    # (d) — the model names a concept outside CONCEPT_REGISTRY; the item falls to refuted, and the
    # fabricated name appears nowhere in the result.
    client = FakeLLM(script={
        CONCEPT_REVISION_TASK: FakeResponse(output={"concept": "galactic_branch_code",
                                                    "reason_codes": []}),
    })
    item = _desc_item()
    result = critique_concept_batch(db, client, [item], catalog_revision="r1")[item.logical_ref]
    assert result.disposition is ConceptDisposition.REFUTED
    assert result.resolved_concept is None
    assert "revision_rejected" in result.reason_codes


def test_a_conflicted_revision_is_also_rejected(db) -> None:
    # A revision that lands on ANOTHER conflicted identifier (branch_id again, or a namespace the
    # shape contradicts) must not stand — the ruleset applies to the revision too.
    client = FakeLLM(script={
        CONCEPT_REVISION_TASK: FakeResponse(output={"concept": "branch_id",
                                                    "reason_codes": []}),
    })
    item = _desc_item()
    result = critique_concept_batch(db, client, [item], catalog_revision="r1")[item.logical_ref]
    assert result.disposition is ConceptDisposition.REFUTED
    assert result.resolved_concept is None


def test_replay_serves_the_stored_result_without_a_client_call(db) -> None:
    # (e) — the second, unscripted client would RAISE if the provider were consulted; the exact
    # same versioned input replays the stored validated outcome instead.
    item = _item()
    first = critique_concept_batch(
        db, _supported_client(), [item], catalog_revision="r1")[item.logical_ref]
    second = critique_concept_batch(db, FakeLLM(), [item], catalog_revision="r1")[item.logical_ref]
    assert first.structured_result_id is not None
    assert second.structured_result_id == first.structured_result_id
    assert second.disposition is ConceptDisposition.ACCEPTED
    assert _llm_calls(db) == 1


def test_a_new_catalog_revision_re_critiques(db) -> None:
    item = _item()
    critique_concept_batch(db, _supported_client(), [item], catalog_revision="r1")
    critique_concept_batch(db, _supported_client(), [item], catalog_revision="r2")
    assert _llm_calls(db) == 2


def test_deterministic_conflicts_refute_without_any_client(db) -> None:
    # The suite app has no LLM client; the deterministic ruleset must still evict a shape-refuted
    # identifier while a clean assignment abstains (nothing to refute it with, nothing to uphold).
    item = _desc_item()
    refuted = critique_concept_batch(db, None, [item], catalog_revision="r1")[item.logical_ref]
    assert refuted.disposition is ConceptDisposition.REFUTED
    assert refuted.resolved_concept is None
    assert "name_or_description_not_identifier" in refuted.conflict_codes
    assert refuted.skipped_reason == "llm_critic_unavailable"

    clean = _item()
    abstained = critique_concept_batch(db, None, [clean], catalog_revision="r1")[clean.logical_ref]
    assert abstained.disposition is ConceptDisposition.ABSTAINED
    assert abstained.resolved_concept == "customer_id"
    assert abstained.skipped_reason == "llm_critic_unavailable"
    assert _llm_calls(db) == 0


# ---- A.54: the loop is BOUNDED, and a bound never evicts a concept --------------------------------


def test_the_call_ceiling_stops_dispatch_and_the_item_ABSTAINS(db) -> None:
    """[A.54] `critique_concept_batch` was a plain per-item loop: no call ceiling, no deadline, no
    `not_attempted` accounting. On a 144-column catalog that is ~70-100 sequential provider calls at
    up to 300s each — and by A.53 it is exactly the stage the first run after merge runs hardest.

    FAIL-OPEN is the disposition, deliberately. The critic is refute-oriented ("absence of support
    is not an eviction"), so an item the bound skipped keeps the classifier's proposal and says so
    via `skipped_reason` — it must never be silence, and it must never be a refutation."""
    from featuregen.overlay.upload.enrich_batch import CallLedger

    items = [_item(ref=f"cib::t.c.col{i}") for i in range(3)]
    ledger = CallLedger(max_provider_calls=0)          # nothing may dispatch
    results = critique_concept_batch(db, _supported_client(), items, catalog_revision="r1",
                                     call_ledger=ledger)

    assert len(results) == 3                            # EVERY item still resolves to a disposition
    for item in items:
        r = results[item.logical_ref]
        assert r.disposition is ConceptDisposition.ABSTAINED
        assert r.resolved_concept == item.proposed_concept       # the proposal STANDS
        assert r.skipped_reason == "critic_call_ceiling"
    assert _llm_calls(db) == 0                          # and nothing was dispatched


def test_the_deadline_stops_dispatch_midway_and_earlier_items_keep_their_verdicts(db) -> None:
    """The time bound, which is the one that matters for the advisory lock. A fake clock keeps this
    assertable without wall-clock, the way `run_batched`'s own deadline tests do."""
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    items = [_item(ref="cib::t.c.first"), _item(ref="cib::t.c.second")]
    results = critique_concept_batch(db, _supported_client(), items, catalog_revision="r1",
                                     deadline_s=10.0, now=lambda: next(ticks))

    assert results["cib::t.c.first"].disposition is ConceptDisposition.ACCEPTED
    second = results["cib::t.c.second"]
    assert second.disposition is ConceptDisposition.ABSTAINED
    assert second.skipped_reason == "critic_deadline"
    assert second.resolved_concept == "customer_id"


def test_a_budget_STOP_can_never_refute_a_concept(db) -> None:
    """The trap this bound walks into if written naively. `_critique_one` ends
    `REVISED if revised else REFUTED`, so a deterministically-conflicted item whose ONE revise pass
    cannot be funded would come out REFUTED — evicting a concept, and its bridge candidacy, for a
    BUDGET reason. A refutation must always be a judgement, never an accounting outcome."""
    from featuregen.overlay.upload.enrich_batch import CallLedger

    item = _desc_item()                                 # deterministically conflicted -> revise pass
    ledger = CallLedger(max_provider_calls=0)
    result = critique_concept_batch(db, FakeLLM(), [item], catalog_revision="r1",
                                    call_ledger=ledger)[item.logical_ref]

    assert result.disposition is not ConceptDisposition.REFUTED
    assert result.resolved_concept == item.proposed_concept
    assert result.skipped_reason == "critic_call_ceiling"


def test_the_batch_reports_how_many_items_it_SKIPPED(db) -> None:
    """`not_attempted` is the third thing A.54 says is missing. Without it the stage reports a clean
    `succeeded` over a run that silently criticised a fraction of its columns."""
    from featuregen.overlay.upload.enrich_batch import CallLedger

    items = [_item(ref=f"cib::t.c.col{i}") for i in range(4)]
    stats: dict = {}
    critique_concept_batch(db, _supported_client(), items, catalog_revision="r1",
                           call_ledger=CallLedger(max_provider_calls=2), stats=stats)

    assert stats["not_attempted"] == 2
    assert stats["stopped_by"] == "critic_call_ceiling"
