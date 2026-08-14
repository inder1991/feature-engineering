"""Phase 2 — Gate #1 bridge: considered-set from the loop + recorded human choice."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import (
    Gate1Error,
    _chosen_option_from_revision,
    _private_considered_revision_snapshot,
    _with_option_ids,
    build_considered_set,
    chosen_feature,
    confirm_gate1,
)
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_assist import _candidate_columns, _validate_idea
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

NOW = datetime(2026, 7, 5, tzinfo=UTC)
CHURN = "customer.relationship_attrition.churn"

#: The analyst's own definition, EXTRACTED as one abstract intent — what the definition-mode anchor
#: is built from since the E4 cutover (2026-08-14). The free-form generator that used to answer
#: ``overlay.feature.recommend`` with physical column refs is deleted, so the model's only output on
#: this path is meaning (concepts + operand classes, never a column) and the shared binder decides
#: which columns serve. Sibling suites in this package import this fixture.
EXTRACTED_INTENT = {"intents": [{
    "display_name": "Days since last activity",
    "business_definition": "Days elapsed since the customer's most recent event.",
    "primary_objective": CHURN,
    "computation_kind": "deterministic_formula",
    "operation_class": "recency",
    "output_grain_entity": "customer",
    "source_grain": "transaction",
    "output": {
        "output_id": "recency_days", "display_label": "Days since last activity",
        "output_type": "numeric", "additivity": "non_additive", "unit_kind": "duration_days",
        "null_input_policy": "null timestamps are excluded and counted",
        "empty_population_policy": "null with populated flag",
    },
    "operands": [
        {"role": "who", "concept": "customer_id", "operand_class": "entity_key"},
        {"role": "when", "concept": "event_timestamp", "operand_class": "event_timestamp"},
    ],
    "temporal": {"anchor_kind": "event", "window_basis": "event time",
                 "window_unit": "days", "cutoff_inclusivity": "inclusive"},
    "rationale": "recency is the strongest dormancy precursor",
}]}

#: The definition an analyst types to get :data:`EXTRACTED_INTENT` back.
DEFINITION = "days since the customer's last activity"


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "churned", "boolean")])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (NOW, NOW))


def _bank_churn(db):
    # A _bank-style catalog carrying the concept-tagged columns the retail_churn templates need to
    # ground: a monetary_stock balance, an as_of_date, a customer_id grain, a monetary_flow amount and
    # an event_timestamp — plus the churn outcome_label (a leakage anchor grounding refuses by
    # construction). Concepts are applied via build_graph's concepts dict (keyed on content_hash),
    # exactly like an enriched upload. Since the E4 cutover (2026-08-14) this is the catalog shape a
    # non-empty considered set needs: the recipe lens is the only candidate source on this path, so a
    # catalog nothing grounds on honestly yields nothing.
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(db, "bank", rows, concepts=concepts)
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (NOW, NOW))


def _client():
    return FakeLLM(script={
        # E4 cutover (2026-08-14): nothing dispatches `overlay.feature.recommend` any more — the
        # free-form physical-column generator behind it is deleted. The one generation call left on
        # this path is the definition-mode anchor's extraction, on the intents task.
        "overlay.feature.intents": FakeResponse(output=EXTRACTED_INTENT),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def test_considered_set_has_anchor_alternatives_and_advisory(db):
    # The three parts of a considered set still arrive together: the requester's own definition as the
    # ANCHOR, the grounded alternatives, and the caveated advisory. Since the E4 cutover (2026-08-14)
    # the anchor is EXTRACTED as an abstract intent and bound by the engine — no free-form generator
    # invents it — so nothing here names a candidate the fixture LLM made up.
    _bank_churn(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition=DEFINITION, actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    assert cs.anchor is not None                                            # definition -> anchor
    assert any(s.features for s in cs.alternatives)
    assert cs.recommendation is not None and cs.recommendation.recommended_lens == "monetary"
    assert "backtest" in cs.recommendation.caveat                           # advisory, caveated


def test_considered_set_threads_feedback(db):
    """A whole-round feedback re-runs the considered set under the human's instruction and still
    produces a valid, governable set (its own intent + persisted snapshot) — this is what makes
    post-feedback candidates governable (I2b), lifting the stale-intent guard.

    Driven through the ENGINE (a confirmed scope) because that is where the feedback text goes since
    the E4 cutover (2026-08-14): it is appended to the redacted hypothesis the intent lens plans
    from. The free-form generator it used to enrich no longer exists."""
    _bank_churn(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned",
                              scope=ConfirmedScope(primary=CHURN),
                              feedback="focus on behavioral signals", now=NOW)
    assert cs.intent_id == intent.intent_id
    assert any(s.features for s in cs.alternatives)   # a feedback round still yields a governable set


def test_hypothesis_only_has_no_anchor(db):
    _bank_churn(db)   # a catalog the recipe lens grounds on, so "alternatives exist" is non-trivial
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    assert cs.anchor is None                                               # hypothesis-only -> no anchor
    assert any(s.features for s in cs.alternatives)                        # but alternatives generated


def test_confirm_gate1_records_choice_and_rejects_out_of_set(db):
    _bank_churn(db)
    intent = submit_intent(hypothesis="churn from balance drop",
                           definition=DEFINITION, actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    # The chosen name is read from the set the builder actually returned. It used to be hard-coded to
    # a free-form LLM candidate; that generator is gone (E4 cutover, 2026-08-14), and an anchor's name
    # is now the engine's to assign.
    assert cs.anchor is not None
    anchor_name = cs.anchor.name
    ref = confirm_gate1(db, cs, chosen_source="anchor", chosen_option_id=anchor_name,
                        actor="ds1", why="best fit for the hypothesis")
    assert ref == anchor_name
    row = db.execute("SELECT chosen_source, chosen_option_id, why FROM contract_gate1_choice "
                     "WHERE intent_id = %s", (intent.intent_id,)).fetchone()
    assert row == ("anchor", anchor_name, "best fit for the hypothesis")

    with pytest.raises(Gate1Error):        # a choice not in the considered set is rejected
        confirm_gate1(db, cs, chosen_source="alternative", chosen_option_id="ghost", actor="ds1")
    with pytest.raises(Gate1Error):        # 'anchor' source but not the anchor
        confirm_gate1(db, cs, chosen_source="anchor", chosen_option_id="not_the_anchor", actor="ds1")


def test_confirm_gate1_validates_the_alternative_source(db):
    # M2: an 'alternative' choice that is actually the anchor's name must be rejected
    from featuregen.overlay.upload.contract.gate1 import ConsideredSet
    from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet
    _bank(db)
    anchor = FeatureIdea("anchor_feat", "", ["public.accounts.balance"], "avg_90d", "accounts")
    alt = FeatureSet("monetary",
                     [FeatureIdea("alt_feat", "", ["public.accounts.balance"], "avg_90d", "accounts")])
    cs = ConsideredSet("intent-x", anchor, [alt], None)
    with pytest.raises(Gate1Error):
        confirm_gate1(db, cs, chosen_source="alternative", chosen_option_id="anchor_feat", actor="ds1")
    assert confirm_gate1(db, cs, chosen_source="alternative", chosen_option_id="alt_feat",
                         actor="ds1") == "alt_feat"


def test_exact_option_ids_preserve_same_name_recipe_and_llm_variants(db):
    from featuregen.overlay.upload.contract.gate1 import ConsideredSet
    from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet

    llm = FeatureIdea(
        "shared_name",
        "",
        ["public.accounts.balance"],
        "avg_90d",
        "accounts",
        generation_source="llm_freeform",
    )
    recipe = FeatureIdea(
        "shared_name",
        "",
        ["public.accounts.balance"],
        "avg_90d",
        "accounts",
        generation_source="recipe",
        recipe_id="balance_trend",
    )
    cs = _with_option_ids(
        ConsideredSet(
            "intent-collision",
            None,
            [
                FeatureSet("llm", [llm]),
                FeatureSet("templates", [recipe]),
            ],
            None,
        ),
        "run-collision",
    )
    revision = _private_considered_revision_snapshot(db, cs)
    options = [
        feature
        for feature_set in revision["public"]["alternatives"]
        for feature in feature_set["features"]
    ]
    assert [feature["name"] for feature in options] == ["shared_name", "shared_name"]
    assert options[0]["option_id"] != options[1]["option_id"]

    first, first_source, first_hash = _chosen_option_from_revision(
        revision, options[0]["option_id"])
    second, second_source, second_hash = _chosen_option_from_revision(
        revision, options[1]["option_id"])
    assert (first.generation_source, first_source) == ("llm_freeform", "alternative")
    assert (second.generation_source, second.recipe_id, second_source) == (
        "recipe", "balance_trend", "alternative")
    assert first_hash != second_hash
    with pytest.raises(Gate1Error, match="unknown considered option"):
        _chosen_option_from_revision(revision, "opt_forged")


def test_exact_option_ids_cover_recipe_grain_reordering_and_shadow_identity(db):
    from featuregen.overlay.upload.contract.gate1 import ConsideredSet
    from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet

    common = {
        "name": "shared_name",
        "description": "same display description",
        "derives_from": ["public.accounts.balance"],
        "aggregation": "avg_90d",
        "generation_source": "recipe",
    }
    recipe_a = FeatureIdea(**common, grain_table="accounts", recipe_id="recipe_a")
    recipe_b = FeatureIdea(**common, grain_table="accounts", recipe_id="recipe_b")
    different_grain = FeatureIdea(**common, grain_table="customers", recipe_id="recipe_c")
    cs = _with_option_ids(
        ConsideredSet(
            "intent-collisions",
            None,
            [
                FeatureSet("temporal", [recipe_a]),
                FeatureSet("monetary", [recipe_b, different_grain]),
            ],
            None,
            recipe_candidate_keys_by_recipe_id={
                "recipe_a": ("rck_a",),
                "recipe_b": ("rck_b",),
                "recipe_c": ("rck_c",),
            },
        ),
        "run-collisions",
    )
    revision = _private_considered_revision_snapshot(db, cs)
    public = [
        feature
        for feature_set in revision["public"]["alternatives"]
        for feature in feature_set["features"]
    ]
    assert len({feature["option_id"] for feature in public}) == 3
    assert {feature["recipe_id"] for feature in public} == {
        "recipe_a", "recipe_b", "recipe_c"}
    assert {
        record["recipe_candidate_key"]
        for record in revision["options_by_id"].values()
    } == {"rck_a", "rck_b", "rck_c"}

    # Transport/UI order is not identity: carrying the opaque IDs with their options still resolves the
    # exact recipe after the response sets are reordered.
    reordered = deepcopy(revision)
    reordered["public"]["alternatives"].reverse()
    selected_id = next(
        feature["option_id"] for feature in public if feature["recipe_id"] == "recipe_b")
    selected, source, _identity_hash = _chosen_option_from_revision(reordered, selected_id)
    assert (selected.recipe_id, selected.grain_table, source) == (
        "recipe_b", "accounts", "alternative")


def test_intent_is_persisted_at_gate1(db):
    # M6: the mandatory hypothesis is durably recorded when the flow reaches Gate #1
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition=DEFINITION, actor="ds1")
    build_considered_set(db, intent, _client(), catalog_source="bank",
                         target_ref="public.accounts.churned", now=NOW)
    row = db.execute("SELECT hypothesis, definition, intake_mode FROM contract_intent "
                     "WHERE intent_id = %s", (intent.intent_id,)).fetchone()
    assert row == ("customers churn when their balance drops", DEFINITION, "definition")


# ── B4: parametric templates seed the considered set ──────────────────────────────────────────────
def test_grounded_templates_seed_a_templates_lens(db):
    # Grounded churn templates enter the considered set as a "templates" lens, each judged by the
    # gauntlet. This lens used to sit beside a free-form LLM lens (the "two-source model"); the E4
    # cutover (2026-08-14) deleted that generator, so on a no-scope run the recipe lens is the set.
    _bank_churn(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    template_sets = [s for s in cs.alternatives if s.lens == "templates"]
    assert len(template_sets) == 1                                    # exactly one templates lens
    names = {f.name for f in template_sets[0].features}
    assert "balance_trend_90d" in names                              # the headline template grounded
    assert all(f.verification == "DESIGN-CHECKED" for f in template_sets[0].features)


def test_template_binding_the_target_ref_is_rejected_for_leakage(db):
    # A template is safe-by-construction against tagged leakage ANCHORS, but the intent's SPECIFIC
    # target_ref may be an untagged column a template binds (here `balance`). The reused gauntlet must
    # still reject it — it surfaces in rejections, never in the templates set or any candidate's derives.
    _bank_churn(db)
    intent = submit_intent(hypothesis="predict the closing balance", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.balance", now=NOW)   # a col templates bind
    template_feats = [f for s in cs.alternatives if s.lens == "templates" for f in s.features]
    assert "balance_trend_90d" not in {f.name for f in template_feats}          # binds target -> out
    assert any(r.get("code") == "LEAKAGE" and r.get("name") == "balance_trend_90d"
               for r in cs.rejections)                                          # surfaced, not dropped
    # the resolved target never appears in ANY surviving template candidate's inputs.
    assert all("public.accounts.balance" not in f.derives_from for f in template_feats)


def test_template_pick_round_trips_through_the_snapshot(db):
    # A template pick is reconstructable from the SERVER-persisted considered set — the snapshot
    # round-trips a template candidate as chosen_source="alternative" (draft is authored from HERE).
    _bank_churn(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    assert confirm_gate1(db, cs, chosen_source="alternative", chosen_option_id="balance_trend_90d",
                         actor="ds1", why="grounded template fits") == "balance_trend_90d"
    feat = chosen_feature(db, intent.intent_id, "alternative", "balance_trend_90d")
    assert feat is not None and feat.name == "balance_trend_90d"
    assert feat.aggregation == "trend_90d"
    assert "public.accounts.balance" in feat.derives_from                       # the grounded stock


def test_no_templates_lens_when_catalog_grounds_nothing(db):
    # The plain _bank catalog has no concept-tagged churn columns, so nothing grounds -> no empty lens.
    # After the E4 cutover (2026-08-14) there is no free-form lens left to fall back on either, so the
    # honest answer on such a catalog is an EMPTY considered set — not a lens with no members, and not
    # candidates a generator invented over columns nothing had classified.
    _bank(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    assert cs.alternatives == []                                               # nothing grounded, no lens


# ── H1a: recipe_id survives Gate 1 + generation_source is SERVER-assigned ──────────────────────────
def test_recipe_id_survives_gate1_round_trip(db):
    # A recipe-sourced (grounded template) candidate carries a server-stamped generation_source="recipe"
    # + recipe_id; both MUST survive the considered-set persist → reload round-trip (Gate 1). Read the id
    # from the in-memory set (no hard-coded template id), then assert the reloaded idea keeps it.
    _bank_churn(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops", actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    tmpl_feat = next(f for s in cs.alternatives if s.lens == "templates" for f in s.features
                     if f.name == "balance_trend_90d")
    assert tmpl_feat.generation_source == "recipe"     # SERVER-assigned recipe label
    assert tmpl_feat.recipe_id                          # a registry recipe id was stamped
    assert confirm_gate1(db, cs, chosen_source="alternative", chosen_option_id="balance_trend_90d",
                         actor="ds1", why="grounded template fits") == "balance_trend_90d"
    feat = chosen_feature(db, intent.intent_id, "alternative", "balance_trend_90d")
    assert feat is not None
    assert feat.recipe_id == tmpl_feat.recipe_id        # recipe_id survived persist → reload (Gate 1)
    assert feat.generation_source == "recipe"


def test_definition_anchor_generation_source_is_user_defined(db):
    # The anchor is the user's OWN definition run through the engine, so its generation_source is the
    # server-assigned "user_defined" — distinct from the recipe lens's "recipe". (E4 cutover,
    # 2026-08-14: the anchor is extracted + bound by the engine, never free-form generated.)
    _bank_churn(db)
    intent = submit_intent(hypothesis="customers churn when their balance drops",
                           definition=DEFINITION, actor="ds1")
    cs = build_considered_set(db, intent, _client(), catalog_source="bank",
                              target_ref="public.accounts.churned", now=NOW)
    assert cs.anchor is not None and cs.anchor.generation_source == "user_defined"


def test_generation_source_is_server_assigned_not_from_raw(db):
    # The server-authority invariant: a candidate arriving on the free-form path with a CLIENT/LLM-set
    # recipe_id + generation_source="recipe" is NOT upgraded — the validator builds the idea from
    # server-known fields only, so it stays generation_source="llm_freeform" with recipe_id=None.
    _bank(db)
    cols = _candidate_columns(db, "bank", roles=())
    known = {c["object_ref"] for c in cols}
    src_of: dict[str, set[str]] = {}
    for c in cols:
        src_of.setdefault(c["object_ref"], set()).add(c["catalog_source"])
    poisoned = {"name": "sneaky", "derives_from": ["public.accounts.balance"], "aggregation": "avg",
                "recipe_id": "attacker_recipe", "generation_source": "recipe"}
    idea, rej = _validate_idea(db, poisoned, known, src_of, None, NOW, timedelta(hours=24), roles=())
    assert rej is None and idea is not None
    assert idea.generation_source == "llm_freeform"    # server default for the free-form path
    assert idea.recipe_id is None                       # client-supplied recipe_id ignored
