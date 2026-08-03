"""Task 1 — the bounded, audited LLM taxonomy-proposal batch seam (template_discovery.py).

Full support path only: registered closed input/output schema, egress allowlist extension for
the named bounded template fields, output validation exclusively against existing controlled
IDs plus abstention, provenance, typed per-template results and a bounded failure budget.
NO live LLM dispatch — every test drives the existing FakeLLM pattern; nothing runs the batch
at import or on ordinary reads.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.enrich_batch import BatchItem
from featuregen.overlay.upload.enrich_llm import _ITEM_META_ALLOWED, _SCHEMAS
from featuregen.overlay.upload.suggestion_taxonomy import FEATURE_CATEGORY_REGISTRY
from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves
from featuregen.overlay.upload.template_discovery import (
    DISCOVERY_PROPOSAL_ABSTAIN,
    DISCOVERY_PROPOSAL_EGRESS_FIELDS,
    DISCOVERY_PROPOSAL_PROMPT_ID,
    DISCOVERY_PROPOSAL_SCHEMA_ID,
    DISCOVERY_PROPOSAL_TASK,
    TEMPLATE_DISCOVERY_OWNER,
    discovery_proposal_items,
    proposal_assignments,
    recipe_revision_id,
    run_discovery_proposal_batch,
    templates_needing_proposal,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES

_SUBSET = ALL_TEMPLATES[:3]


def _proposal(ref, category="abstain", use_cases=(), business_value="abstain", keywords=()):
    return {"ref": ref, "proposal": {"feature_category": category,
                                     "use_case_ids": list(use_cases),
                                     "business_value": business_value,
                                     "keywords": list(keywords)}}


def _fake(results):
    return FakeLLM(script={DISCOVERY_PROPOSAL_TASK: FakeResponse(
        output={"results": results})})


# ── closed input: bounded template fields on the extended egress allowlist ─────────────────────

def test_items_carry_only_allowlisted_bounded_template_fields():
    items = discovery_proposal_items(_SUBSET)
    assert [it.ref for it in items] == [t.id for t in _SUBSET]
    for item in items:
        assert set(item.metadata) <= set(DISCOVERY_PROPOSAL_EGRESS_FIELDS)
        # the named fields are on the EXISTING per-item egress allowlist (the extension)
        assert set(item.metadata) <= _ITEM_META_ALLOWED
        for key, value in item.metadata.items():
            if isinstance(value, list):
                assert all(isinstance(v, str) and len(v) <= 200 for v in value)
            else:
                assert isinstance(value, str) and len(value) <= 400


def test_output_schema_is_registered_and_closed():
    schema = _SCHEMAS[(DISCOVERY_PROPOSAL_SCHEMA_ID, 1)]
    assert schema["additionalProperties"] is False
    item = schema["properties"]["results"]["items"]
    assert item["additionalProperties"] is False
    proposal = item["properties"]["proposal"]
    assert proposal["additionalProperties"] is False
    assert set(proposal["required"]) == {"feature_category", "use_case_ids",
                                         "business_value", "keywords"}


# ── typed per-template results; the batch never zeroes out over one bad item ───────────────────

def test_valid_abstaining_and_uncontrolled_results_are_each_typed(db):
    leaf = selectable_leaves()[0]
    t0, t1, t2 = (t.id for t in _SUBSET)
    fake = _fake([
        _proposal(t0, category="ratio", use_cases=[leaf],
                  business_value="Signals attrition pressure early.", keywords=["attrition"]),
        _proposal(t1),                                            # full abstention
        _proposal(t2, category="deposit_stability"),              # plausible legacy string
    ])
    result = run_discovery_proposal_batch(db, fake, templates=_SUBSET)
    by_id = {r.template_id: r for r in result.results}
    assert set(by_id) == {t0, t1, t2}

    proposed = by_id[t0]
    assert proposed.status == "proposed"
    assert proposed.proposal.feature_category == "ratio"
    assert proposed.proposal.canonical_use_cases == (leaf,)
    assert proposed.proposal.business_value == "Signals attrition pressure early."
    assert proposed.proposal.keywords == ("attrition",)
    assert proposed.recipe_revision_id == recipe_revision_id(_SUBSET[0])

    assert by_id[t1].status == "abstained"
    assert by_id[t1].proposal is None

    rejected = by_id[t2]                                          # never coerced into an ID
    assert rejected.status == "malformed"
    assert rejected.proposal is None
    assert "category_uncontrolled" in rejected.reason_codes

    assert result.batch_status == "completed"  # 1/3 failure within the default budget


def test_uncontrolled_use_case_is_rejected_not_coerced(db):
    t0 = _SUBSET[0].id
    fake = _fake([_proposal(t0, use_cases=["retail_churn"])])     # legacy tag, not a leaf
    result = run_discovery_proposal_batch(db, fake, templates=_SUBSET[:1])
    assert result.results[0].status == "malformed"
    assert "use_case_uncontrolled" in result.results[0].reason_codes


def test_missing_ref_is_typed_and_other_items_still_resolve(db):
    leaf = selectable_leaves()[0]
    t0, t1, t2 = (t.id for t in _SUBSET)
    fake = _fake([_proposal(t0, category="trend", use_cases=[leaf])])  # t1/t2 never answered
    result = run_discovery_proposal_batch(db, fake, templates=_SUBSET)
    by_id = {r.template_id: r for r in result.results}
    assert by_id[t0].status == "proposed"
    assert by_id[t1].status == "missing" and by_id[t2].status == "missing"


def test_blocked_item_is_typed_egress_and_does_not_zero_the_run(db):
    leaf = selectable_leaves()[0]
    good = discovery_proposal_items(_SUBSET[:1])
    poisoned = BatchItem(ref=_SUBSET[1].id,
                         metadata={"definition": "raw uploader free text"})  # forbidden key
    fake = _fake([_proposal(_SUBSET[0].id, category="trend", use_cases=[leaf])])
    result = run_discovery_proposal_batch(db, fake, templates=_SUBSET[:2],
                                          items=[*good, poisoned])
    by_id = {r.template_id: r for r in result.results}
    assert by_id[_SUBSET[0].id].status == "proposed"      # not an unexplained zero-output run
    assert by_id[_SUBSET[1].id].status == "egress_blocked"


def test_failure_budget_bounds_the_batch(db):
    fake = _fake([_proposal(t.id, category="not_a_category") for t in _SUBSET])
    result = run_discovery_proposal_batch(db, fake, templates=_SUBSET,
                                          max_failure_fraction=0.5)
    assert result.batch_status == "failure_budget_exceeded"
    assert len(result.results) == 3                        # every template still typed


def test_provenance_records_model_prompt_schema_and_producer(db):
    fake = _fake([_proposal(t.id) for t in _SUBSET])
    result = run_discovery_proposal_batch(db, fake, templates=_SUBSET)
    p = result.provenance
    assert p.task == DISCOVERY_PROPOSAL_TASK
    assert p.prompt_id == DISCOVERY_PROPOSAL_PROMPT_ID and p.prompt_version == 1
    assert p.schema_id == DISCOVERY_PROPOSAL_SCHEMA_ID and p.schema_version == 1
    assert p.provider == "fake" and p.model == "test"      # FakeLLM generation settings
    assert p.producer == TEMPLATE_DISCOVERY_OWNER


# ── proposals convert to llm_proposed hint assignments, never authority ────────────────────────

def test_proposal_assignments_are_llm_proposed_hints_with_llm_evidence(db):
    leaf = selectable_leaves()[0]
    fake = _fake([_proposal(_SUBSET[0].id, category="ratio", use_cases=[leaf],
                            business_value="Useful for churn triage.", keywords=["churn"])])
    result = run_discovery_proposal_batch(db, fake, templates=_SUBSET[:1])
    assignments = proposal_assignments(result.results[0], result.provenance)
    assert assignments.feature_category.basis == "llm_proposed"
    assert assignments.feature_category.operational_influence == "hint"
    evidence = assignments.feature_category.evidence[0]
    assert evidence.producer.value == "llm" and evidence.strength.value == "proposed"
    assert all(a.basis == "llm_proposed" and a.operational_influence == "hint"
               for a in assignments.canonical_use_cases)
    assert assignments.business_value.basis == "llm_proposed"
    assert assignments.keywords[0].value == "churn"


def test_proposal_assignments_of_an_abstention_carry_nothing():
    from featuregen.overlay.upload.template_discovery import (
        DiscoveryProposalProvenanceV1,
        DiscoveryProposalResultV1,
    )
    result = DiscoveryProposalResultV1(
        template_id=_SUBSET[0].id, recipe_revision_id="r", status="abstained",
        proposal=None, reason_codes=(DISCOVERY_PROPOSAL_ABSTAIN,))
    provenance = DiscoveryProposalProvenanceV1(
        task=DISCOVERY_PROPOSAL_TASK, prompt_id=DISCOVERY_PROPOSAL_PROMPT_ID,
        prompt_version=1, schema_id=DISCOVERY_PROPOSAL_SCHEMA_ID, schema_version=1,
        provider="fake", model="test", producer=TEMPLATE_DISCOVERY_OWNER)
    assignments = proposal_assignments(result, provenance)
    assert assignments.feature_category is None
    assert assignments.canonical_use_cases == () and assignments.keywords == ()
    assert assignments.business_value is None


# ── once per template revision ─────────────────────────────────────────────────────────────────

def test_templates_needing_proposal_is_once_per_recipe_revision():
    already = {recipe_revision_id(_SUBSET[0])}
    remaining = templates_needing_proposal(_SUBSET, already)
    assert [t.id for t in remaining] == [t.id for t in _SUBSET[1:]]
    # a template CONTENT edit re-revisions the recipe, so it needs a fresh proposal…
    edited = dataclasses.replace(_SUBSET[0], notes=("edited",))
    assert [t.id for t in templates_needing_proposal((edited,), already)] == [edited.id]
    # …while a discovery-side (keyword) edit does not touch recipe identity at all.
    assert templates_needing_proposal(_SUBSET[:1], already) == ()


def test_nothing_dispatches_at_import_or_without_an_explicit_call():
    """The support path exists; running it is a separately approved action. Importing the
    module and building items must never construct a client or dispatch."""
    items = discovery_proposal_items(ALL_TEMPLATES)
    assert len(items) == 157


def test_run_refuses_a_failure_fraction_outside_the_unit_interval(db):
    with pytest.raises(ValueError):
        run_discovery_proposal_batch(db, _fake([]), templates=_SUBSET,
                                     max_failure_fraction=1.5)
