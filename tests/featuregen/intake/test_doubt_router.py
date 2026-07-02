from featuregen.intake.doubt_router import RouterThresholds, route_draft, route_field


def _route(**kw):
    base = dict(
        ambiguity=0.05, confidence=0.98, source="llm", has_value=True,
        policy_sensitive=False, is_calculation_method_choice=False,
    )
    base.update(kw)
    return route_field(**base)


def test_auto_resolves_low_ambiguity_high_confidence_with_a_value():
    assert _route() == "auto"


def test_unknown_field_without_a_value_is_never_auto():
    assert _route(has_value=False) == "human"  # a safe source must exist


def test_policy_sensitive_is_always_human_regardless_of_score():
    assert _route(ambiguity=0.0, confidence=1.0, source="catalog", policy_sensitive=True) == "human"


def test_calc_method_choice_is_always_human():
    assert _route(ambiguity=0.0, confidence=1.0, is_calculation_method_choice=True) == "human"


def test_high_ambiguity_is_human():
    assert _route(ambiguity=0.80, confidence=0.40) == "human"


def test_low_confidence_is_human():
    assert _route(ambiguity=0.10, confidence=0.55) == "human"


def test_thresholds_are_config_gated():
    strict = RouterThresholds(ambiguity_max=0.10, confidence_min=0.90)
    assert route_field(
        ambiguity=0.20, confidence=0.80, source="default", has_value=True,
        policy_sensitive=False, is_calculation_method_choice=False, thresholds=strict,
    ) == "human"


def test_route_draft_definition_example():
    field_scores = {
        "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
        "calculation_method": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"},
        "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
        "filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"},
    }
    d = route_draft(field_scores, ["filters.declined_status_encoding"], mode="definition")
    assert d["entity_grain"] == "auto"
    assert d["windows"] == "auto"
    assert d["calculation_method"] == "auto"  # definition mode: not a choice
    assert d["filters"] == "human"            # UNKNOWN sub-path + high ambiguity


def test_route_draft_hypothesis_calc_method_is_a_choice():
    d = route_draft(
        {"calculation_method": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"}},
        [], mode="hypothesis",
    )
    assert d["calculation_method"] == "human"  # picking the method IS Gate #1's job


def test_route_draft_policy_sensitive_target_is_human():
    d = route_draft(
        {"target": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"}},
        [], mode="hypothesis", policy_sensitive_fields=("target",),
    )
    assert d["target"] == "human"


def test_single_binding_005_floor_field_still_auto_resolves():
    # Carry-forward (Task 5.1): catalog_cardinality_score imposes a 0.05 ambiguity floor even on a
    # single unambiguous binding. The auto-resolve cutoff (0.30) must sit above that floor so such a
    # field STILL auto-resolves.
    assert _route(ambiguity=0.05, confidence=0.95, source="catalog") == "auto"
