from featuregen.intake.scoring import (
    CatalogView,
    FieldScore,
    catalog_cardinality_score,
    combine_scores,
    current_catalog_view,
    register_catalog_view,
    score_fields,
)


def test_cautious_max_takes_higher_ambiguity_and_lower_confidence():
    llm = FieldScore(0.10, 0.90, "llm")
    catalog = FieldScore(0.80, 0.40, "catalog")
    c = combine_scores(llm, catalog)
    assert c.ambiguity == 0.80
    assert c.confidence == 0.40
    assert c.source == "catalog"  # the deterministic check raised the doubt → it owns the score


def test_llm_can_never_lower_a_deterministic_doubt():
    # The model is near-certain, but the concept binds to three candidate columns.
    llm = FieldScore(0.05, 0.99, "llm")
    catalog = FieldScore(0.85, 0.35, "catalog")
    c = combine_scores(llm, catalog)
    assert c.ambiguity == 0.85 and c.confidence == 0.35


def test_catalog_cardinality_scales_with_bindings():
    assert catalog_cardinality_score(1).ambiguity <= 0.30
    assert catalog_cardinality_score(1).confidence >= 0.70
    assert catalog_cardinality_score(2).ambiguity > 0.30
    assert catalog_cardinality_score(3).ambiguity >= 0.70  # many incompatible readings


def test_score_fields_only_combines_concept_bearing_fields():
    llm_scores = {
        "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},  # verbatim; no concept
        "filters": {"ambiguity": 0.40, "confidence": 0.70, "source": "llm"},  # binds a status concept
    }
    concept_of = {"windows": None, "filters": "declined card authorization"}
    scored = score_fields(llm_scores, concept_of, cardinality=lambda concept: 3)
    assert scored["windows"] == {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"}
    assert scored["filters"]["ambiguity"] >= 0.70  # cardinality(3) raised it above the LLM's 0.40
    assert scored["filters"]["source"] == "catalog"


class _View:
    def candidate_count(self, concept: str) -> int:
        return {"declined card authorization": 3}.get(concept, 1)

    def metadata(self):
        return {"objects": ["card_authorizations"]}


def test_catalog_view_single_source_accessor():
    register_catalog_view(_View())
    view = current_catalog_view()
    assert isinstance(view, CatalogView)  # runtime-checkable Protocol
    assert view.candidate_count("declined card authorization") == 3
