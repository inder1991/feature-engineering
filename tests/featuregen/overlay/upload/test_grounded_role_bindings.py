from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_grounding_context import (
    assert_canonical_recipe_exhaustive,
    build_recipe_grounding_context,
)
from featuregen.overlay.upload.templates import (
    BindingResolution,
    GroundingStatus,
    Need,
    Template,
    ground_template_outcome,
)


def _template() -> Template:
    return Template(
        id="amount_by_customer",
        family="test",
        intent="amount by customer",
        needs=(
            Need("entity", "customer_id"),
            Need("measure", "monetary_flow"),
        ),
        params={"window": (90, 30)},
        aggregation="sum",
        additivity="additive",
        explain="H",
        use_cases=(),
        pit="trailing",
        source_entity_need_role="entity",
    )


def _graph(db, *, duplicate_measure: bool = False) -> None:
    rows = [
        CanonicalRow("bank", "transactions", "customer_id", "text", is_grain=True),
        CanonicalRow("bank", "transactions", "amount", "numeric"),
    ]
    concepts = {
        content_hash(rows[0]): "customer_id",
        content_hash(rows[1]): "monetary_flow",
    }
    if duplicate_measure:
        duplicate = CanonicalRow("bank", "transactions", "other_amount", "numeric")
        rows.append(duplicate)
        concepts[content_hash(duplicate)] = "monetary_flow"
    build_graph(db, "bank", rows, concepts=concepts)


def test_grounding_preserves_ordered_recipe_roles_and_private_identity(db) -> None:
    _graph(db)
    template = _template()
    outcome = ground_template_outcome(db, template, catalog_source="bank")

    assert outcome.status is GroundingStatus.GROUNDED
    assert outcome.feature is not None
    assert [binding.role for binding in outcome.feature.role_bindings] == ["entity", "measure"]
    assert all(
        binding.binding_resolution is BindingResolution.UNIQUE
        for binding in outcome.feature.role_bindings
    )
    assert all(
        binding.logical_ref.startswith("bank::public.")
        for binding in outcome.feature.role_bindings
    )
    assert all(len(binding.tied_candidate_set_hash) == 64 for binding in outcome.feature.role_bindings)

    context = build_recipe_grounding_context(template, outcome.feature)
    assert context.recipe_id == template.id
    assert context.source_entity_need_role == "entity"
    assert dict(context.semantic_parameters) == {"window": 90}
    assert len(context.recipe_candidate_key) == 64
    assert not hasattr(outcome.feature, "input_role_bindings")


def test_equal_best_columns_remain_ambiguous_with_stable_candidate_hash(db) -> None:
    _graph(db, duplicate_measure=True)
    outcome = ground_template_outcome(db, _template(), catalog_source="bank")

    assert outcome.feature is not None
    measure = next(binding for binding in outcome.feature.role_bindings if binding.role == "measure")
    assert measure.binding_resolution is BindingResolution.AMBIGUOUS
    assert len(measure.tied_candidate_logical_refs) == 2
    assert measure.tied_candidate_logical_refs == tuple(sorted(measure.tied_candidate_logical_refs))


def test_candidate_budget_exhaustion_is_not_reported_as_no_match(db, monkeypatch) -> None:
    _graph(db, duplicate_measure=True)
    monkeypatch.setattr(
        "featuregen.overlay.upload.templates.MAX_GROUNDING_CANDIDATES_PER_NEED",
        1,
    )
    outcome = ground_template_outcome(db, _template(), catalog_source="bank")

    assert outcome.status is GroundingStatus.BUDGET_TRUNCATED
    assert outcome.feature is None
    assert outcome.reason_codes == ("grounding_candidate_budget_truncated", "measure")


def test_canonical_recipe_serializer_is_exhaustive() -> None:
    assert_canonical_recipe_exhaustive()
