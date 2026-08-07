import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import feature_assist as fa
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import (
    RejectCode,
    feature_recipe,
    leakage_check,
    recommend_features,
    recommend_features_report,
)
from featuregen.overlay.upload.graph import build_graph


def _bank_graph(db):
    rows = [
        CanonicalRow("bank", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("bank", "transactions", "amount", "numeric", definition="txn amount"),
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),  # point-in-time
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "churned", "boolean", definition="customer churned flag"),
    ]
    build_graph(db, "bank", rows)


def test_recommend_features_grounds_out_hallucinations(db):
    _bank_graph(db)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "txn_count_90d", "description": "count of txns",
         "derives_from": ["public.transactions.amount"], "aggregation": "count_90d",
         "grain_table": "accounts"},
        {"name": "ghost", "description": "uses a column that doesn't exist",
         "derives_from": ["public.transactions.nonexistent"]},   # hallucinated -> dropped
    ]})})
    ideas = recommend_features(db, "predict churn", client, catalog_source="bank")
    assert len(ideas) == 1
    assert ideas[0].name == "txn_count_90d"
    assert ideas[0].derives_from == ["public.transactions.amount"]


def test_feature_recipe_pairs_llm_intent_with_deterministic_join_path(db):
    _bank_graph(db)
    client = FakeLLM(script={"overlay.feature.recipe": FakeResponse(output={
        "grain_table": "accounts", "join_table": "transactions",
        "derives_from": ["public.transactions.amount"],
        "aggregation": "sum_90d", "as_of_column": "posted_at"})})
    recipe = feature_recipe(db, "total spend per account last 90 days", client, catalog_source="bank")
    assert recipe.grain_table == "accounts"
    assert recipe.derives_from == ["public.transactions.amount"]
    # the join path is real (found deterministically), not invented by the LLM
    assert len(recipe.join_path) == 1
    step = recipe.join_path[0]
    # Traversal accounts -> transactions is the REVERSE of the stored transactions->accounts N:1,
    # so oriented to the traversal it is 1:N (one account fans out to many transactions) and the
    # step reads forward from the grain (M7).
    assert step.cardinality == "1:N"
    assert step.from_ref == "public.accounts.account_id"
    assert step.to_ref == "public.transactions.acct_id"


def test_leakage_check_flags_target_derived_column(db):
    _bank_graph(db)
    derives = ["public.accounts.churned", "public.transactions.amount"]
    client = FakeLLM(script={"overlay.feature.leakage": FakeResponse(output={"leaks": [
        {"object_ref": "public.accounts.churned", "reason": "looks like the target label"},
        {"object_ref": "public.not.used", "reason": "not in derives_from -> ignored"},
    ]})})
    warnings = leakage_check(db, derives, "public.accounts.churned", client)
    assert len(warnings) == 1
    assert warnings[0].object_ref == "public.accounts.churned"


def test_recommend_and_recipe_respect_read_scope(db):
    """M6: a PII column must NOT be fed to the LLM (or returned) without the role."""
    from featuregen.overlay.upload.canonical import CanonicalRow
    rows = [
        CanonicalRow("bank", "accounts", "balance", "numeric", definition="ledger balance"),
        CanonicalRow("bank", "accounts", "ssn", "text", sensitivity="pii", definition="customer SSN"),
    ]
    build_graph(db, "bank", rows)

    captured = {}

    class _CaptureLLM:
        def call(self, request):
            captured["columns"] = request.inputs.get("catalog_metadata", {}).get("columns", [])
            from featuregen.intake.llm import LLMResult
            return LLMResult(output={"features": []}, self_reported_scores={}, call_ref="", status="ok")

    # No role -> the PII column is not among the candidate columns sent to the LLM.
    recommend_features(db, "predict risk", _CaptureLLM(), catalog_source="bank")
    refs = {c["object_ref"] for c in captured["columns"]}
    assert "public.accounts.balance" in refs
    assert "public.accounts.ssn" not in refs

    # With the pii_reader role -> the PII column is available.
    recommend_features(db, "predict risk", _CaptureLLM(), catalog_source="bank", roles={"pii_reader"})
    refs2 = {c["object_ref"] for c in captured["columns"]}
    assert "public.accounts.ssn" in refs2


# ── Task 6c: the generator shows its grounding ──────────────────────────────────────────────────
#
# Earlier tasks widened WHAT the generator is shown (classification axes, adjudication confidence
# and alternatives, proposed values). This closes the loop: each proposed feature returns which of
# that context it actually used. The value is EXPLANATORY — a reviewer reads it, and the human's
# dry run reads it to tell whether the widened context is being consumed at all. Nothing branches
# on its CONTENT, and no feature is more trusted for carrying a confident-sounding one.


@pytest.fixture
def v5(monkeypatch):
    """Flag-on with no version override — the contract version whose OUTPUT carries `grounding`."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.delenv(fa.FEATURE_CONTEXT_VERSION_ENV, raising=False)
    return monkeypatch


_AMOUNT = "public.transactions.amount"
_GRAIN = "public.accounts.account_id"


def _idea(**over) -> dict:
    """One well-formed proposal against `_bank_graph`. A COUNTING aggregation deliberately: it
    clears the currency/unit gates, so any refusal in these tests is about grounding and nothing
    else."""
    raw = {"name": "txn_count_90d", "description": "count of txns",
           "derives_from": [_AMOUNT], "aggregation": "count_90d", "grain_table": "accounts"}
    raw.update(over)
    return raw


def _returning(*features) -> FakeLLM:
    return FakeLLM(script={"overlay.feature.recommend": FakeResponse(
        output={"features": list(features)})})


def _report(db, client, **kw):
    return recommend_features_report(db, "total counterparty exposure by customer", client,
                                     catalog_source="bank", critic=False, **kw)


def test_each_generated_feature_carries_its_grounding(db, v5):
    _bank_graph(db)
    report = _report(db, _returning(_idea(grounding=[
        {"column": _AMOUNT, "role": "measure", "why": "monetary, concept credit_limit_amount"},
        {"column": _GRAIN, "role": "grain", "why": "confirmed grain column"}])))
    idea = report.ideas[0]
    assert [(g.column, g.role, g.why) for g in idea.grounding] == [
        (_AMOUNT, "measure", "monetary, concept credit_limit_amount"),
        (_GRAIN, "grain", "confirmed grain column"),
    ]


def test_a_grounding_entry_naming_an_unoffered_column_is_refused(db, v5):
    """The existing grounding check validates the refs; this proves the new array is covered too.

    A column the catalog never offered is a claim about the CATALOG that is false — the feature's
    stated account of itself is fabricated — so the whole proposal goes, visibly, with its own code.
    Left in place the array would be a channel for ungrounded refs to re-enter beside the ones
    `_ground_refs` already filters."""
    _bank_graph(db)
    report = _report(db, _returning(_idea(grounding=[
        {"column": "NOT_IN_THE_MENU", "role": "measure", "why": "invented"}])))
    assert report.ideas == []
    assert [r["code"] for r in report.rejections] == [RejectCode.UNKNOWN_GROUNDING_COLUMN]


def test_a_feature_that_returns_no_grounding_is_accepted_unchanged(db, v5):
    """`grounding` is OPTIONAL on the response. A model that skips the key must not turn every such
    response into a whole-call failure, nor cost the feature its acceptance — the alternative is a
    required field the model sometimes omits, which is a call-level failure mode."""
    _bank_graph(db)
    report = _report(db, _returning(_idea()))
    assert [i.name for i in report.ideas] == ["txn_count_90d"]
    assert report.ideas[0].grounding == ()
    # An EMPTY array is the same disposition as an absent key: honest absence, never a refusal.
    assert _report(db, _returning(_idea(grounding=[]))).ideas[0].grounding == ()


def test_an_unknown_role_drops_that_entry_and_never_the_feature(db, v5):
    """The role vocabulary is CLOSED: free text there would become an ungoverned second vocabulary.
    But an off-vocabulary role is a limit of OUR taxonomy, not a false claim about the catalog, so
    it costs the entry and never the feature — the asymmetry with an unoffered column is deliberate.
    """
    _bank_graph(db)
    report = _report(db, _returning(_idea(grounding=[
        {"column": _AMOUNT, "role": "measure", "why": "the amount"},
        {"column": _GRAIN, "role": "primary_key", "why": "not one of the six"}])))
    assert [(g.column, g.role) for g in report.ideas[0].grounding] == [(_AMOUNT, "measure")]


def test_a_bare_column_name_grounds_to_its_object_ref(db, v5):
    """Resolved through the SAME resolver `derives_from` uses, and EMITTED resolved: the model's
    reference format must not un-ground an otherwise-valid feature, and what reaches the reviewer
    must be a real catalog ref they can follow, never the model's raw string."""
    _bank_graph(db)
    report = _report(db, _returning(_idea(grounding=[
        {"column": "amount", "role": "measure", "why": "bare name"}])))
    assert [g.column for g in report.ideas[0].grounding] == [_AMOUNT]


def test_grounding_changes_no_disposition(db, v5):
    """EXPLANATORY, NEVER AUTHORITY. The same proposal with and without a confident grounding array
    reaches the identical status, requirements and operands — a model cannot talk a feature into
    being more trusted."""
    _bank_graph(db)
    bare = _report(db, _returning(_idea())).ideas[0]
    grounded = _report(db, _returning(_idea(grounding=[
        {"column": _AMOUNT, "role": "measure", "why": "confirmed, governed, and ideal"},
        {"column": _GRAIN, "role": "grain", "why": "confirmed grain column"}]))).ideas[0]
    assert grounded.grounding and not bare.grounding
    from dataclasses import replace
    assert replace(grounded, grounding=(), grounding_trace=None) == replace(
        bare, grounding=(), grounding_trace=None)


def test_an_over_long_why_is_bounded_in_code_and_costs_the_feature_nothing(db, v5):
    """The bound is CODE-SIDE and truncating, deliberately. A schema `maxLength` is stripped from
    the wire but still validated on the response, so it would fail the WHOLE call for one long
    clause the model was never told to shorten."""
    _bank_graph(db)
    report = _report(db, _returning(_idea(grounding=[
        {"column": _AMOUNT, "role": "measure", "why": "w" * 5_000}])))
    assert len(report.ideas[0].grounding[0].why) == fa._MAX_GROUNDING_WHY_LEN


def test_the_grounding_list_is_capped_after_every_entry_is_validated(db, v5):
    """Capped LAST, so an unoffered column past the cap is still caught rather than silently
    trimmed away — the cap bounds the payload, it is not a place fabrications can hide."""
    _bank_graph(db)
    over = [{"column": _AMOUNT, "role": "measure", "why": f"entry {i}"}
            for i in range(fa._MAX_GROUNDING_ENTRIES + 4)]
    assert len(_report(db, _returning(_idea(grounding=over))).ideas[0].grounding) == (
        fa._MAX_GROUNDING_ENTRIES)
    hidden = [*over, {"column": "NOT_IN_THE_MENU", "role": "measure", "why": "past the cap"}]
    assert _report(db, _returning(_idea(grounding=hidden))).ideas == []


# ── the response contract ───────────────────────────────────────────────────────────────────────


def _feature_item(version: int) -> dict:
    from featuregen.overlay.upload.enrich_llm import _SCHEMAS
    return _SCHEMAS[("feature_ideas", version)]["properties"]["features"]["items"]


def _grounding_node(version: int) -> dict:
    return _feature_item(version)["properties"]["grounding"]


def _keywords(node, kw: str) -> bool:
    if isinstance(node, dict):
        return kw in node or any(_keywords(v, kw) for v in node.values())
    if isinstance(node, list):
        return any(_keywords(v, kw) for v in node)
    return False


def test_the_grounding_contract_carries_no_response_side_bounds():
    """THE landmine this class of change keeps hitting. `maxLength` / `maxItems` are stripped from
    the WIRE (the model never learns them) but the canonical schema still validates the RESPONSE —
    so a bound here fails the whole call rather than dropping one entry, and the `feature_ideas`
    body is permissive precisely so one bad item cannot take its siblings' good answers with it.
    Same for `required` and `enum`: both are carried WIRE-ONLY, via `x-wire-*`.

    STRUCTURE is a different matter and IS validated (array of objects of strings) — exactly as
    `derives_from`'s already is. That constraint survives the projection, so the model is TOLD it;
    the ones removed here are precisely the ones it would be judged against without being told.
    """
    node = _grounding_node(5)
    for banned in ("maxLength", "maxItems", "minItems", "required", "enum"):
        assert not _keywords(node, banned), banned


def test_the_wire_projection_does_ask_the_model_for_the_shape():
    """The other half: leniency on the response must not become silence on the wire. The projected
    schema names the three keys as required and closes the role vocabulary to six values."""
    from featuregen.intake.schema_projection import project_for_anthropic

    wire = project_for_anthropic(_grounding_node(5))
    assert wire["items"]["required"] == ["column", "role", "why"]
    assert wire["items"]["properties"]["role"]["enum"] == list(fa.GROUNDING_ROLES)
    assert wire["items"]["additionalProperties"] is False
    assert "grounding" in project_for_anthropic(_feature_item(5))["required"]


def test_v5_is_v4_plus_grounding_and_v4_stays_a_clean_rollback():
    """The version identifies the contract that egressed. v2-v4 were byte-aliases of v1 because only
    the INPUT contract moved; v5 is the first whose OUTPUT differs, so the earlier versions must
    still resolve AND must still carry no grounding."""
    from featuregen.overlay.upload.enrich_llm import _SCHEMAS

    assert fa._FEATURE_CONTEXT_SCHEMA_VERSION == 5
    for schema_id in ("feature_ideas", "feature_recipe", "leakage", "feature_set_rec"):
        assert (schema_id, 5) in _SCHEMAS, schema_id
    for older in (1, 2, 3, 4):
        item = _SCHEMAS[("feature_ideas", older)]["properties"]["features"]["items"]
        assert "grounding" not in item["properties"], older
    v5_item = _SCHEMAS[("feature_ideas", 5)]["properties"]["features"]["items"]
    v1_item = _SCHEMAS[("feature_ideas", 1)]["properties"]["features"]["items"]
    assert set(v5_item["properties"]) - set(v1_item["properties"]) == {"grounding"}


def test_the_instruction_asks_for_grounding_only_at_the_version_that_can_carry_it(db, v5,
                                                                                  monkeypatch):
    """A model must never be asked for output the schema forbids. At v4 the wire item is CLOSED
    without a `grounding` key, so asking for one would demand something the model cannot emit."""
    _bank_graph(db)
    seen: list[str] = []

    class _Capture:
        def call(self, request):
            from featuregen.intake.llm import LLMResult
            seen.append(request.inputs["redacted_intent"])
            return LLMResult(output={"features": []}, self_reported_scores={}, call_ref="",
                             status="ok")

    _report(db, _Capture(), budget=1)
    assert "grounding" in seen[-1]
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    _report(db, _Capture(), budget=1)
    assert "grounding" not in seen[-1]
