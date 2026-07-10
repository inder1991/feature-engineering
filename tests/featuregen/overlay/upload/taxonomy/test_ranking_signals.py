"""Phase-2A Task A1 — tests for the typed ranking signals + their total derivations."""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.ranking_signals import (
    BindingQuality,
    EntityCompatibility,
    ModellingContextFit,
    PITCompleteness,
    binding_quality,
    entity_compatibility,
    modelling_context_fit,
    pit_completeness,
    semantic_group,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES, GroundedFeature, Need, Template


# ── factories ───────────────────────────────────────────────────────────────────────────────────
def _template(**overrides: object) -> Template:
    """A minimal valid Template; override any field a case needs."""
    kwargs: dict[str, object] = dict(
        id="synthetic", family="synthetic", intent="test recipe", needs=(),
        params={"window": (90, 60, 30)}, aggregation="trend", additivity="non_additive",
        explain="H", use_cases=("retail_churn",),
        pit="trailing window (as_of − {window}, as_of], values knowable strictly ≤ as_of.")
    kwargs.update(overrides)
    return Template(**kwargs)  # type: ignore[arg-type]


def _grounded(notes: tuple[str, ...] = (), template_id: str = "synthetic",
              name: str = "synthetic") -> GroundedFeature:
    return GroundedFeature(
        template_id=template_id, name=name, aggregation="trend_90d", grain_table=None,
        as_of_column=None, derives_pairs=(), params={}, notes=notes)


# ── BindingQuality ──────────────────────────────────────────────────────────────────────────────
def test_binding_quality_clean_bind_is_exact():
    assert binding_quality(_grounded(notes=())) is BindingQuality.EXACT
    assert binding_quality(_grounded(notes=("anchor: 'ecl' routes this off a churn catalog.",))) \
        is BindingQuality.EXACT


def test_binding_quality_substitution_note_is_strong():
    gf = _grounded(notes=("concept sub: entity uses 'customer_id' (Part F: customer_identifier)",))
    assert binding_quality(gf) is BindingQuality.STRONG


def test_binding_quality_unmet_optional_note_is_acceptable():
    gf = _grounded(notes=("optional need 'salary' (category_code) unmet -> derive from credits",))
    assert binding_quality(gf) is BindingQuality.ACCEPTABLE


def test_binding_quality_ambiguous_marker_is_ambiguous():
    # AMBIGUOUS is reserved (grounding resolves deterministically) but the member is reachable.
    gf = _grounded(notes=("ambiguous binding: multiple viable stock columns",))
    assert binding_quality(gf) is BindingQuality.AMBIGUOUS


def test_binding_quality_worst_wins_precedence():
    # An unmet optional (ACCEPTABLE) drags the quality below a concept substitution (STRONG).
    gf = _grounded(notes=("concept sub: entity uses 'customer_id'",
                          "optional need 'salary' unmet -> derive"))
    assert binding_quality(gf) is BindingQuality.ACCEPTABLE
    # And an ambiguous bind is the worst of all.
    gf2 = _grounded(notes=("ambiguous binding", "optional need 'x' unmet -> y"))
    assert binding_quality(gf2) is BindingQuality.AMBIGUOUS


def test_binding_quality_total_over_all_templates():
    # Derivation is total: a grounded feature built from every recipe's notes yields a valid member.
    for t in ALL_TEMPLATES:
        gf = _grounded(notes=t.notes, template_id=t.id, name=t.id)
        assert isinstance(binding_quality(gf), BindingQuality)


# ── PITCompleteness ─────────────────────────────────────────────────────────────────────────────
def test_pit_completeness_total_over_all_templates():
    for t in ALL_TEMPLATES:
        assert isinstance(pit_completeness(t), PITCompleteness)


def test_pit_completeness_complete_for_trailing_window():
    assert pit_completeness(_template()) is PITCompleteness.COMPLETE
    # Every authored recipe bakes in a real PIT/as-of declaration, so all resolve COMPLETE today.
    assert all(pit_completeness(t) is PITCompleteness.COMPLETE for t in ALL_TEMPLATES)


def test_pit_completeness_not_applicable_for_non_time_dependent_recipe():
    # No window param + additive-neutral output + empty PIT rule -> PIT does not apply.
    t = _template(params={}, additivity="n/a", pit="")
    assert pit_completeness(t) is PITCompleteness.NOT_APPLICABLE


def test_pit_completeness_unknown_for_empty_pit_that_should_have_one():
    # Empty PIT but the recipe IS time-windowed -> we cannot attest it -> UNKNOWN (not NOT_APPLICABLE).
    t = _template(params={"window": (30,)}, additivity="non_additive", pit="")
    assert pit_completeness(t) is PITCompleteness.UNKNOWN
    # Empty PIT, no window, but a non-neutral additivity is also UNKNOWN (only n/a is NOT_APPLICABLE).
    assert pit_completeness(_template(params={}, additivity="additive", pit="none")) \
        is PITCompleteness.UNKNOWN


def test_pit_completeness_partial_for_marker_less_declaration():
    # A non-empty rule that names no PIT anchor is a short / partial statement of intent.
    assert pit_completeness(_template(pit="rolling 90-day count")) is PITCompleteness.PARTIAL


# ── ModellingContextFit (Task B3 — real fit vs the confirmed context) ───────────────────────────────
def test_modelling_context_fit_neutral_without_context_total():
    # No confirmed context -> NEUTRAL for every recipe (2A ranking is byte-identical).
    for t in ALL_TEMPLATES:
        assert modelling_context_fit(t) is ModellingContextFit.NEUTRAL


def test_modelling_context_fit_required_match_on_own_context():
    # A recipe carrying the ifrs9_staging framework tag, confirmed ifrs9 -> REQUIRED_MATCH.
    t = _template(use_cases=("credit_risk", "ifrs9_staging"))
    assert modelling_context_fit(t, confirmed_contexts=("ifrs9",)) is ModellingContextFit.REQUIRED_MATCH


def test_modelling_context_fit_compatible_for_generic_recipe():
    # A generic recipe (no modelling_context tag) is COMPATIBLE under any confirmed context.
    t = _template(use_cases=("retail_churn",))
    assert modelling_context_fit(t, confirmed_contexts=("ifrs9",)) is ModellingContextFit.COMPATIBLE


def test_modelling_context_fit_conflict_for_disjoint_context():
    # An frtb-only recipe under confirmed ifrs9 -> CONFLICT (a surfaced warning, NEVER a hard reject).
    t = _template(use_cases=("market_risk", "frtb"))
    assert modelling_context_fit(t, confirmed_contexts=("ifrs9",)) is ModellingContextFit.CONFLICT


def test_modelling_context_fit_empty_confirmed_is_neutral():
    # An explicit empty confirmed set is still NEUTRAL, even on a framework-tagged recipe.
    t = _template(use_cases=("credit_risk", "ifrs9_staging"))
    assert modelling_context_fit(t, confirmed_contexts=()) is ModellingContextFit.NEUTRAL


def test_modelling_context_fit_total_over_all_templates():
    # Derivation is total: every recipe under a confirmed context yields a valid member.
    for t in ALL_TEMPLATES:
        assert isinstance(modelling_context_fit(t, confirmed_contexts=("ifrs9",)), ModellingContextFit)


# ── EntityCompatibility (Task B3 — the SOFT grain signal; never a hard reject) ──────────────────────
def _customer_grain(**overrides: object) -> Template:
    """A customer-grain recipe: its entity-role need links the customer entity (customer_id)."""
    return _template(needs=(Need("entity", "customer_id"),), **overrides)


def _account_grain(**overrides: object) -> Template:
    """An account-grain recipe: its entity-role need links the account entity (account_id)."""
    return _template(needs=(Need("entity", "account_id"),), **overrides)


def test_entity_compatibility_unknown_without_entity_total():
    # No confirmed target_entity -> UNKNOWN for every recipe (the axis is a no-op in ranking).
    for t in ALL_TEMPLATES:
        assert entity_compatibility(t) is EntityCompatibility.UNKNOWN


def test_entity_compatibility_exact_when_grain_is_target():
    # A customer-grain recipe under target_entity="customer" already predicts at the target grain.
    assert entity_compatibility(_customer_grain(), target_entity="customer") is EntityCompatibility.EXACT


def test_entity_compatibility_unknown_when_no_rollup_to_target():
    # customer does NOT roll up to account -> UNKNOWN (a soft no-op), NEVER INCOMPATIBLE.
    assert entity_compatibility(_customer_grain(), target_entity="account") \
        is EntityCompatibility.UNKNOWN


def test_entity_compatibility_derivable_via_rollup():
    # An account-grain recipe rolls up to customer (account -> customer) -> DERIVABLE.
    assert entity_compatibility(_account_grain(), target_entity="customer") \
        is EntityCompatibility.DERIVABLE


def test_entity_compatibility_derivable_transitively():
    # A transaction-grain recipe rolls up to customer via account (transaction -> account -> customer).
    txn = _template(needs=(Need("entity", "transaction_id"),))
    assert entity_compatibility(txn, target_entity="customer") is EntityCompatibility.DERIVABLE


def test_entity_compatibility_none_target_is_unknown():
    assert entity_compatibility(_customer_grain(), target_entity=None) is EntityCompatibility.UNKNOWN


def test_entity_compatibility_has_no_incompatible_member():
    # Hard entity rejection is deferred to Phase 3 — the member must not exist.
    assert not hasattr(EntityCompatibility, "INCOMPATIBLE")


def test_entity_compatibility_never_returns_a_reject():
    # No (recipe, target) combination ever yields anything but EXACT / DERIVABLE / UNKNOWN.
    ok = (EntityCompatibility.EXACT, EntityCompatibility.DERIVABLE, EntityCompatibility.UNKNOWN)
    for target in ("customer", "account", "facility", "obligor", "nonsense", None):
        for t in (_customer_grain(), _account_grain(), _template(), *ALL_TEMPLATES[:10]):
            assert entity_compatibility(t, target_entity=target) in ok


# ── semantic_group ────────────────────────────────────────────────────────────────────────────────
def test_semantic_group_is_source_template_id_total():
    for t in ALL_TEMPLATES:
        assert semantic_group(t) == t.id


def test_semantic_group_groups_balance_trend_variants():
    balance_trend = next(t for t in ALL_TEMPLATES if t.id == "balance_trend")
    group = semantic_group(balance_trend)
    assert group == "balance_trend"
    # Grounded variants of one template all carry that template_id -> they share the group.
    v90 = _grounded(template_id="balance_trend", name="balance_trend_90d")
    v60 = _grounded(template_id="balance_trend", name="balance_trend_60d")
    assert v90.template_id == group == v60.template_id
