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


def test_an_ambiguous_bare_name_drops_that_entry_and_never_the_feature(db, v5):
    """The payload HANDS the model bare names — `_table_context` presents grain and as-of columns
    as bare column names, and the grounding directive asks the model to account for them. A model
    that copies the name it was SHOWN, for a column that really was offered, must not lose its
    feature because that name happens to exist in two tables.

    The asymmetry is the argument: `derives_from` names `amount` by its exact ref below and grounds
    fine, and were IT the ambiguous one it would drop a ref and keep the feature. The EXPLANATION
    must never be judged more harshly than the thing it explains."""
    from featuregen.overlay.upload.graph import add_column_row

    _bank_graph(db)
    add_column_row(db, "bank", CanonicalRow("bank", "accounts", "amount", "numeric"))
    report = _report(db, _returning(_idea(grounding=[
        {"column": "amount", "role": "measure", "why": "ambiguous across two tables"},
        {"column": _GRAIN, "role": "grain", "why": "unambiguous, so it survives"}])))
    assert [(g.column, g.role) for g in report.ideas[0].grounding] == [(_GRAIN, "grain")]


def test_an_entry_with_no_column_is_dropped_and_never_read_as_a_fabrication(db, v5):
    """`column` is wire-required and response-OPTIONAL by deliberate design — a canonical `required`
    would fail the whole call. Treating the omission the schema tolerates as a fabrication would
    contradict that in the same breath, so an incomplete entry costs itself and nothing more."""
    _bank_graph(db)
    for missing in ({"role": "measure", "why": "no column key"},
                    {"column": "   ", "role": "measure", "why": "whitespace"}):
        report = _report(db, _returning(_idea(grounding=[
            missing, {"column": _AMOUNT, "role": "measure", "why": "the good one"}])))
        assert [g.column for g in report.ideas[0].grounding] == [_AMOUNT], missing
        assert report.rejections == [], missing


def test_a_non_string_column_is_absence_not_a_column_literally_named_None():
    """The sharp case, and the one place a unit test is the HONEST test.

    `str(None)` is `"None"` — a naive read would report to a human that the model invented a column
    called None. `_ground_notes` reads a non-string as ABSENT instead.

    Deliberately not driven through `recommend_features_report`: it cannot get there. The canonical
    schema types `column` as `string` (structure IS validated, and unlike a stripped `maxLength` the
    model is TOLD it on the wire), so a `null` fails response validation, burns the 2 repairs and
    fails the round — the same treatment `derives_from`'s own `items: {type: string}` gives. This
    branch is the belt-and-braces behind that, asserted where it is reachable rather than pretended
    to be exercised end to end."""
    known = {_AMOUNT}
    for bad in (None, 42, ["a"], {"x": 1}):
        notes, unresolved = fa._ground_notes(
            [{"column": bad, "role": "measure", "why": "w"},
             {"column": _AMOUNT, "role": "measure", "why": "good"}], known)
        assert unresolved is None, bad                      # never reported as a fabrication
        assert [n.column for n in notes] == [_AMOUNT], bad  # …and never the string "None"
    # A non-dict entry is skipped for the same reason, and neither shape refuses the feature.
    assert fa._ground_notes(["a bare string", None], known) == ((), None)


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


def test_refine_is_told_the_grounding_rules_its_schema_now_compels(db, v5):
    """`refine_idea` stamps `_feature_schema_version()` like every other feature-gen call, so at v5
    the projected wire item makes `grounding` REQUIRED — the schema compels an answer. Compelling an
    answer while withholding the rules for it is the same prompt/schema inversion as asking for
    output the schema forbids, so the directive travels with this call too, even though this path
    has never carried the derives-from one.

    It is version-gated here as well: at v4 the wire item has no such key and the human's
    instruction goes out untouched."""
    from featuregen.overlay.upload.feature_assist import refine_idea

    _bank_graph(db)
    seen: list[str] = []

    class _Client:
        def call(self, request):
            from featuregen.intake.llm import LLMResult
            seen.append(request.inputs["redacted_intent"])
            return LLMResult(output={"features": [_idea(name="txn_count_30d",
                                                        aggregation="count_30d",
                                                        grounding=[{"column": _AMOUNT,
                                                                    "role": "measure",
                                                                    "why": "the amount"}])]},
                             self_reported_scores={}, call_ref="", status="ok")

    revised, rejection = refine_idea(db, {"name": "txn_count_90d", "derives_from": [_AMOUNT]},
                                     "use a 30 day window", _Client(), catalog_source="bank")
    assert rejection is None and revised is not None
    assert seen[-1].startswith("use a 30 day window") and "grounding" in seen[-1]
    # …and a revision's grounding is carried through the same gauntlet, not silently dropped.
    assert [(g.column, g.role) for g in revised.grounding] == [(_AMOUNT, "measure")]


# ── Task 6d: expand the question, not the corpus ────────────────────────────────────────────────


def _expansion_client(terms):
    """Answers the expansion schema, and counts how many provider requests it was asked for."""
    from featuregen.intake.llm import LLMResult

    class _Client:
        count = 0

        def call(self, request):
            self.count += 1
            return LLMResult(output={"terms": list(terms)}, self_reported_scores={},
                             call_ref="", status="ok")

    return _Client()


def test_the_objective_is_expanded_with_related_business_terms(db):
    toks = fa._objective_tokens("total counterparty exposure by customer", entity=None, scope=None,
                                conn=db,
                                client=_expansion_client(["obligor", "limit", "facility"]))
    assert {"counterparty", "exposure", "customer"} <= toks    # the literal words survive
    assert {"obligor", "limit", "facility"} <= toks            # and the derived ones join them


def test_expansion_is_replayed_for_an_identical_objective(db):
    """The same question must not re-bill. Second call issues no provider request."""
    calls = _expansion_client(["obligor"])
    fa._objective_tokens("total counterparty exposure", None, None, conn=db, client=calls)
    fa._objective_tokens("total counterparty exposure", None, None, conn=db, client=calls)
    assert calls.count == 1


def test_no_client_degrades_to_literal_tokens(db):
    """Expansion is advisory: with no provider the search behaves exactly as it does today."""
    toks = fa._objective_tokens("counterparty exposure", None, None, conn=db, client=None)
    assert toks == {"counterparty", "exposure"}


def test_no_conn_degrades_to_literal_tokens():
    """The other half of the same guard, and the one every pure caller takes."""
    assert fa._objective_tokens("counterparty exposure", None, None,
                                conn=None, client=_expansion_client(["obligor"])) == {
        "counterparty", "exposure"}


# ── every degradation path lands on the same fallback ───────────────────────────────────────────


def _degrading_client(behaviour):
    from featuregen.intake.llm import LLMResult

    class _Client:
        def call(self, request):
            if behaviour == "throw":
                raise RuntimeError("provider outage")
            return LLMResult(output=behaviour, self_reported_scores={}, call_ref="", status="ok")

    return _Client()


@pytest.mark.parametrize("behaviour", [
    "throw",                       # provider outage / a fake with no script for this task
    {"terms": "not a list"},       # a validated-looking answer the code gate cannot read
    {"nothing": "useful"},         # the wrong shape entirely (fails repair, output None)
    {"terms": []},                 # an HONEST empty expansion
    {"terms": [None, 42, "   ", "x" * 65]},   # every entry individually unusable
])
def test_every_expansion_failure_falls_back_to_exactly_todays_literal_tokens(db, behaviour):
    """Feature generation must never fail because expansion failed. Each of these reaches the
    fallback by a DIFFERENT route — a client throw, a code-gate rejection, repair exhaustion, an
    empty answer, per-entry drops — and every one of them must be indistinguishable from today."""
    literal = fa._objective_tokens("counterparty exposure", None, None, conn=db, client=None)
    assert fa._objective_tokens("counterparty exposure", None, None, conn=db,
                                client=_degrading_client(behaviour)) == literal


def test_an_objective_carrying_pii_is_blocked_at_the_egress_guard_and_still_ranks(db):
    """The objective is USER-TYPED text. It rides the same guard as every other call, so a payload
    the guard refuses costs the expansion and nothing else — the ranking still happens."""
    objective = "exposure for alice@example.com"
    assert fa._objective_tokens(objective, None, None, conn=db,
                                client=_expansion_client(["obligor"])) == fa._objective_tokens(
        objective, None, None, conn=db, client=None)
    assert db.execute("SELECT count(*) FROM security_audit "
                      "WHERE event_type = 'EGRESS_BLOCKED'").fetchone()[0] == 1


def test_an_unusable_entry_costs_only_itself_and_never_its_siblings(db):
    """THE reason the length bound is code-side rather than a schema `maxLength`. A 65-char term is
    dropped and its 39 usable siblings survive; a schema bound would have been validated against
    the response FIRST and destroyed the whole expansion — and cached nothing, so the question
    would re-bill forever. Blanks and case-variant repeats are dropped the same way."""
    toks = fa._objective_tokens("counterparty", None, None, conn=db,
                                client=_expansion_client(
                                    ["obligor", "OBLIGOR", "   ", "facility", "y" * 65]))
    assert {"obligor", "facility"} <= toks
    assert "y" not in toks
    assert fa._accept_expansion_terms(["obligor", "OBLIGOR", "  ", "y" * 65]) == ("obligor",)
    assert fa._accept_expansion_terms([f"t{i}" for i in range(200)]) == tuple(
        f"t{i}" for i in range(fa._MAX_EXPANSION_TERMS))


# ── source priority survives the expansion ──────────────────────────────────────────────────────


def test_the_governed_route_expands_the_SCOPE_and_never_the_discarded_objective(db):
    """The trap this design exists to avoid. Under a confirmed scope the free-text objective is
    DISCARDED by source priority (spec §6) — so expanding the raw objective would put it straight
    back in through the side door, as LLM-derived synonyms of the very words the rule excluded.
    What gets expanded is what gets tokenised: the scope."""
    from featuregen.intake.llm import LLMResult
    from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

    sent: list[str] = []

    class _Client:
        def call(self, request):
            sent.append(request.inputs["catalog_metadata"]["objective"])
            return LLMResult(output={"terms": ["attrition"]}, self_reported_scores={},
                             call_ref="", status="ok")

    scope = ConfirmedScope(primary="retail_churn", secondary=(), target_entity="Account",
                           modelling_contexts=("ifrs9",))
    toks = fa._objective_tokens("weather forecast", None, scope, conn=db, client=_Client())
    assert {"retail", "churn", "account", "ifrs9"} <= toks     # the governed words still lead
    assert "attrition" in toks                                  # widened from the SCOPE
    assert "weather" not in toks and "forecast" not in toks     # …and never from the objective
    assert "weather" not in sent[0] and "retail_churn" in sent[0]  # it never even egressed


def test_case_and_spacing_are_one_cached_question(db):
    """The cache key is the NORMALIZED subject, so a reviewer retyping their own question with
    different capitalisation does not re-bill it."""
    calls = _expansion_client(["obligor"])
    fa._objective_tokens("Total  Counterparty   Exposure", None, None, conn=db, client=calls)
    fa._objective_tokens("total counterparty exposure", None, None, conn=db, client=calls)
    assert calls.count == 1


def test_two_different_questions_are_two_different_cache_entries(db):
    """THE load-bearing property of the cache key, and the ONLY test that can catch its absence.

    Every other cache test here uses ONE normalized subject, so a degenerate key that dropped
    `subject` altogether — `canonical_hash({"expansion_version": 1})` — would replay the first
    question's expansion for EVERY question thereafter and still pass all of them, and the full
    suite besides. That is exactly the failure this design exists to avoid: one question's synonyms
    silently serving every other question in the deployment."""
    from featuregen.intake.llm import LLMResult

    answers = {"total counterparty exposure": ["obligor"], "customer churn risk": ["attrition"]}
    asked: list[str] = []

    class _PerSubject:
        def call(self, request):
            subject = request.inputs["catalog_metadata"]["objective"]
            asked.append(subject)
            return LLMResult(output={"terms": answers[subject]}, self_reported_scores={},
                             call_ref="", status="ok")

    client = _PerSubject()
    first = fa._objective_tokens("total counterparty exposure", None, None, conn=db, client=client)
    second = fa._objective_tokens("customer churn risk", None, None, conn=db, client=client)

    assert asked == ["total counterparty exposure", "customer churn risk"]  # a 2nd Q = a 2nd call
    assert "obligor" in first and "obligor" not in second       # …and no cross-contamination,
    assert "attrition" in second and "attrition" not in first   # in either direction.
    # …while EACH question is still replayed on its own second asking. Distinctness and replay are
    # one property, and a test that pins only one of them pins neither.
    assert fa._objective_tokens("customer churn risk", None, None, conn=db, client=client) == second
    assert len(asked) == 2


def test_an_empty_expansion_is_cached_too(db):
    """"This question has no useful expansion" IS an answer. Not storing it would re-bill that
    question on every request forever — the cache-shaped-thing failure mode."""
    calls = _expansion_client([])
    fa._objective_tokens("predict churn", None, None, conn=db, client=calls)
    fa._objective_tokens("predict churn", None, None, conn=db, client=calls)
    assert calls.count == 1


def test_a_provider_fault_is_NOT_frozen_into_the_cache(db):
    """The other direction, and the one a naive cache gets wrong: a transient outage must not pin
    an empty expansion in place for the life of the store."""
    fa._objective_tokens("predict churn", None, None, conn=db, client=_degrading_client("throw"))
    assert "attrition" in fa._objective_tokens("predict churn", None, None, conn=db,
                                               client=_expansion_client(["attrition"]))


# ── what consumes the tokens ────────────────────────────────────────────────────────────────────


def test_the_derived_terms_reach_the_RANKING_and_not_merely_the_token_set(db):
    """The consumption trace, end to end. `_objective_tokens` feeds exactly one thing — the
    `-len(_column_tokens(c) & obj_tokens)` sort key — so the proof that the expansion works is that
    the menu comes back in a different ORDER, with the column nobody's literal words could reach
    now in front."""
    from featuregen.overlay.upload.graph import build_graph as _bg

    _bg(db, "bank", [
        CanonicalRow("bank", "book", "aaa_paint_colour", "text"),
        CanonicalRow("bank", "book", "obligor_limit", "numeric"),
    ])
    cols = fa._candidate_columns(db, "bank", roles=())

    def _refs(**kw):
        return [c["object_ref"] for c in fa.select_relevant_context(
            db, cols, objective="counterparty exposure", entity=None, scope=None, **kw)[0]]

    # Literally, NOTHING matches "counterparty exposure": the tie breaks on object_ref.
    assert _refs() == ["public.book.aaa_paint_colour", "public.book.obligor_limit"]
    # Widened by one derived term, the column that means the same thing leads.
    assert _refs(client=_expansion_client(["obligor"])) == [
        "public.book.obligor_limit", "public.book.aaa_paint_colour"]


def _task_recorder(terms):
    from featuregen.intake.llm import LLMResult

    class _Client:
        def __init__(self):
            self.tasks: list[str] = []

        def call(self, request):
            self.tasks.append(request.task)
            if request.task == fa.OBJECTIVE_EXPANSION_TASK:
                return LLMResult(output={"terms": list(terms)}, self_reported_scores={},
                                 call_ref="", status="ok")
            return LLMResult(output={"features": []}, self_reported_scores={}, call_ref="",
                             status="ok")

    return _Client()


@pytest.mark.parametrize("version", ["3", "4", "5"])
def test_the_expansion_is_not_payload_VERSION_gated(db, monkeypatch, version):
    """Relevance ranking runs for every feature-context payload version, so the thing that widens
    it does too. Gating this on the v5 contract would silently switch it off on a rollback."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, version)
    _bank_graph(db)
    client = _task_recorder(["obligor"])
    recommend_features(db, "counterparty exposure", client, catalog_source="bank", budget=1,
                       critic=False)
    assert fa.OBJECTIVE_EXPANSION_TASK in client.tasks


def test_flag_off_issues_no_expansion_call_at_all(db, monkeypatch):
    """Flag-OFF returns the thin pre-Slice-3 menu without ever ranking, so there is no question to
    widen and nothing may be billed for one."""
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    _bank_graph(db)
    client = _task_recorder(["obligor"])
    recommend_features(db, "counterparty exposure", client, catalog_source="bank", budget=1,
                       critic=False)
    assert fa.OBJECTIVE_EXPANSION_TASK not in client.tasks


def test_the_expansion_lands_on_an_llm_call_like_every_other_call(db):
    """User text egressed for the platform's own convenience is still egress: it is audited, and
    the replay row points back at the call that produced it."""
    fa._objective_tokens("counterparty exposure", None, None, conn=db,
                         client=_expansion_client(["obligor"]))
    ref = db.execute("SELECT llm_call_ref FROM llm_call WHERE task = %s",
                     (fa.OBJECTIVE_EXPANSION_TASK,)).fetchone()
    assert ref is not None
    assert db.execute(
        "SELECT count(*) FROM structured_result_provenance "
        "WHERE producer_kind = 'llm_call' AND producer_ref = %s", (ref[0],)).fetchone()[0] == 1


def test_every_call_in_one_request_names_the_HUMAN_who_typed_the_question(db, v5):
    """The expansion payload is a BARE USER SENTENCE — the row in this flow that most needs to say
    who typed it. Absent a threaded actor the seam substitutes `enrich_llm._ENRICH_ACTOR`, an
    UNAUTHENTICATED service principal, while the sibling calls carrying the SAME sentence name the
    human. One request must never attribute one copy of a sentence to a service and the rest to a
    person: the audit would read as two different parties asking."""
    from tests.featuregen._helpers import mint_test_identity

    human = mint_test_identity(subject="user:analyst")
    _bank_graph(db)
    client = _task_recorder(["obligor"])
    recommend_features(db, "counterparty exposure", client, catalog_source="bank", budget=1,
                       critic=False, actor=human)

    rows = db.execute("SELECT task, created_by->>'subject', created_by->>'actor_kind' "
                      "FROM llm_call").fetchall()
    assert {t for t, _s, _k in rows} >= {fa.OBJECTIVE_EXPANSION_TASK, "overlay.feature.recommend"}
    assert {s for _t, s, _k in rows} == {"user:analyst"}          # every row, expansion included
    assert {k for _t, _s, k in rows} == {"human"}                 # …and never a service principal
    # Named explicitly so the assertion above cannot pass by the expansion simply not happening.
    assert any(t == fa.OBJECTIVE_EXPANSION_TASK for t, _s, _k in rows)


def test_refine_at_v4_carries_the_human_instruction_untouched(db, v5, monkeypatch):
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    from featuregen.overlay.upload.feature_assist import refine_idea

    _bank_graph(db)
    seen: list[str] = []

    class _Client:
        def call(self, request):
            from featuregen.intake.llm import LLMResult
            seen.append(request.inputs["redacted_intent"])
            return LLMResult(output={"features": [_idea()]}, self_reported_scores={}, call_ref="",
                             status="ok")

    refine_idea(db, {"name": "orig", "derives_from": [_AMOUNT]}, "tighten it", _Client(),
                catalog_source="bank")
    assert seen[-1] == "tighten it"


# ── Task 8: an unconfirmed grain / as-of reaches the model, LABELLED ─────────────────────────────
#
# `_table_context` used to require a non-null `grain_fact_event_id` / `availability_fact_event_id`,
# so a table whose grain the FILE declares but no human has confirmed reached the generator with no
# grain at all — and the model invented one. The declared value is now emitted with an honest
# `grain_status` / `as_of_status`, because an unconfirmed grain is better information than a missing
# one PROVIDED the reader is told which it is.


def _ctx_row(column: str, **over) -> dict:
    """One candidate row in the shape `_candidate_columns` produces, grain/as-of unset by default."""
    row = {"catalog_source": "cib", "table": "facility_limits", "column": column,
           "is_grain": False, "grain_fact_event_id": None,
           "is_as_of": False, "availability_fact_event_id": None,
           "table_definition": None, "table_primary_entity": None}
    row.update(over)
    return row


def test_table_context_carries_declared_grain_with_its_status():
    """A file-declared, unconfirmed grain reaches the model labelled — not omitted."""
    cols = [
        {"catalog_source": "cib", "table": "facility_limits", "column": "cust_id",
         "is_grain": True, "grain_fact_event_id": None,
         "is_as_of": False, "availability_fact_event_id": None,
         "table_definition": None, "table_primary_entity": None},
    ]
    block = fa._table_context(cols)[0]
    assert block["grain_columns"] == ["cust_id"]
    assert block["grain_status"] == "declared"


def test_table_context_marks_a_confirmed_grain_confirmed():
    cols = [
        {"catalog_source": "cib", "table": "facility_limits", "column": "cust_id",
         "is_grain": True, "grain_fact_event_id": "evt_1",
         "is_as_of": False, "availability_fact_event_id": None,
         "table_definition": None, "table_primary_entity": None},
    ]
    block = fa._table_context(cols)[0]
    assert block["grain_status"] == "confirmed"


def test_table_context_carries_a_declared_as_of_with_its_status():
    """The same relaxation on the OTHER axis. The as-of column decides point-in-time correctness,
    so a table that has one and cannot say so is exactly as dangerous as one with no grain."""
    block = fa._table_context([_ctx_row("booked_at", is_as_of=True)])[0]
    assert block["as_of_column"] == "booked_at"
    assert block["as_of_status"] == "declared"


def test_table_context_marks_a_confirmed_as_of_confirmed():
    block = fa._table_context([
        _ctx_row("booked_at", is_as_of=True, availability_fact_event_id="evt_a")])[0]
    assert (block["as_of_column"], block["as_of_status"]) == ("booked_at", "confirmed")


def test_a_table_with_neither_carries_neither_the_value_nor_a_status():
    """HONEST ABSENCE. A status with no value beside it would be a claim about a grain that does not
    exist, and `no grain at all` is a different statement from `an unconfirmed grain`."""
    block = fa._table_context([_ctx_row("amount")])[0]
    assert not {"grain_columns", "grain_status", "as_of_column", "as_of_status"} & set(block)


def test_a_confirmed_grain_is_never_widened_by_a_file_declaration():
    """THE CONFIGURATION THE RELAXATION COULD HAVE BROKEN — a human confirmed something the file
    disagrees with. `project_table_facts_for_ref` SPARES file-declared columns from its clear, so a
    table whose file declares (cust_id, region) while a human confirmed grain=(cust_id) really does
    carry is_grain on both, one stamped and one not.

    Emitting the UNION there would assert a grain nobody attested — not the file's and not the
    human's — and label the human's own answer `declared` on the way past. A confirmation is the
    stronger statement, so where any confirmation exists the block is exactly what it was before
    this task: the confirmed set, marked confirmed. The relaxation is additive, never a downgrade."""
    cols = [_ctx_row("cust_id", is_grain=True, grain_fact_event_id="evt_1"),
            _ctx_row("region", is_grain=True)]
    block = fa._table_context(cols)[0]
    assert block["grain_columns"] == ["cust_id"]
    assert block["grain_status"] == "confirmed"


def test_a_confirmed_as_of_wins_over_a_declared_one():
    """Same rule on the time axis, and the ordering makes it bite: `a_ts` sorts first, so a naive
    `first as-of column` pick would hand the model the UNCONFIRMED one and call it the anchor."""
    cols = [_ctx_row("a_ts", is_as_of=True),
            _ctx_row("z_ts", is_as_of=True, availability_fact_event_id="evt_a")]
    block = fa._table_context(cols)[0]
    assert (block["as_of_column"], block["as_of_status"]) == ("z_ts", "confirmed")


def test_a_multi_column_declared_grain_is_carried_whole_and_marked_declared():
    """Nothing is confirmed here, so the whole file-declared compound grain travels — sorted, and
    with one status for the set. A compound grain half-emitted is a different table."""
    cols = [_ctx_row("region", is_grain=True), _ctx_row("cust_id", is_grain=True)]
    block = fa._table_context(cols)[0]
    assert block["grain_columns"] == ["cust_id", "region"]
    assert block["grain_status"] == "declared"


def test_the_two_status_keys_are_egress_classified(monkeypatch):
    """The landmine every payload task in this plan has hit. Neither key appears in any
    `_TABLE_CONTEXT_*` classifier by default and the adapter's terminal branch returns None, so
    emitting one unclassified refuses the WHOLE payload — not the field. Each is pinned against the
    list it ACTUALLY belongs to, so this fails both if someone drops one and if someone re-grades
    one."""
    from featuregen.overlay.upload import enrich_llm
    from featuregen.overlay.upload.enrich_llm import sanitize_feature_context

    blocks = fa._table_context([_ctx_row("cust_id", is_grain=True),
                               _ctx_row("booked_at", is_as_of=True)])
    assert {"grain_status", "as_of_status"} <= set(blocks[0])
    safe, _pii, _samples, _version = sanitize_feature_context({"table_context": blocks})
    assert safe is not None, "a status key is unclassified — egress blocked the whole payload"
    assert set(safe["table_context"][0]) == set(blocks[0])

    original = enrich_llm._TABLE_CONTEXT_IDENTITY_KEYS
    for key in ("grain_status", "as_of_status"):
        monkeypatch.setattr(enrich_llm, "_TABLE_CONTEXT_IDENTITY_KEYS", original - {key})
        assert sanitize_feature_context({"table_context": blocks})[0] is None, key
        monkeypatch.setattr(enrich_llm, "_TABLE_CONTEXT_IDENTITY_KEYS", original)
    # …and neither is graded PROSE or DEFINITION: both are PLATFORM-minted closed tokens, never
    # uploader-typed text, so a redactor would have nothing to scrub and would misdocument the
    # egress surface.
    assert not ({"grain_status", "as_of_status"}
                & (enrich_llm._TABLE_CONTEXT_PROSE_KEYS
                   | enrich_llm._TABLE_CONTEXT_DEFINITION_KEYS))


def test_the_status_vocabulary_is_closed():
    """Two values, both short tokens. Anything else on this key is a value the model cannot weigh."""
    blocks = fa._table_context([_ctx_row("cust_id", is_grain=True),
                               _ctx_row("booked_at", is_as_of=True,
                                        availability_fact_event_id="evt_a")])
    statuses = {b.get("grain_status") for b in blocks} | {b.get("as_of_status") for b in blocks}
    assert statuses - {None} <= {"confirmed", "declared"}


# ── what "confirmed" ACTUALLY means, pinned against the real ingest path ─────────────────────────
#
# Both tests below were written because the task's premise — "a non-null grain_fact_event_id means a
# HUMAN confirmed it" — is not true of this codebase. They are the honest record of what the token
# means and of the gap it does NOT close. Neither asserts a behaviour introduced by Task 8; both
# would have held before it, and that is the point.


def _sealed() -> None:
    from datetime import timedelta

    from featuregen.overlay.config import OverlayConfig, register_overlay_config

    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_an_ordinary_uploads_declared_grain_reads_confirmed_because_ingest_AUTO_confirms_it(db):
    """`confirmed` is the LIFECYCLE sense — a CONFIRMED fact event is projected — NOT "a human
    checked this". `ingest._assert_fact` auto-confirms a file-declared grain/as-of with
    `authority_basis=source_declared` ("the fact is authoritative because the SOURCE declared it"),
    so the ordinary upload below lands on `confirmed` with nobody having reviewed anything.

    Pinned because the whole point of the status is that a reader can weigh it, and a reader who
    thinks this token means human review has been given confidence nobody earned. The surface that
    DOES draw that line is `bridge_assessment._human_reviewed`, off the fact STREAM."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay import facts
    from featuregen.overlay.upload.ingest import ingest_upload

    _sealed()
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    rows = [CanonicalRow("authp", "balances", "bal_id", "text", is_grain=True),
            CanonicalRow("authp", "balances", "snap_ts", "timestamp", as_of=True)]
    assert ingest_upload(db, "authp", rows, actor=owner,
                         now=datetime(2026, 8, 1, tzinfo=UTC)).status == "ingested"

    block = fa._table_context(fa._candidate_columns(db, "authp", roles=()))[0]
    assert (block["grain_columns"], block["grain_status"]) == (["bal_id"], "confirmed")
    assert (block["as_of_column"], block["as_of_status"]) == ("snap_ts", "confirmed")
    # …and no human ever confirmed it: the CONFIRMED event carries a source-declared authority basis
    # and no confirmers at all, which is exactly what `_human_reviewed` refuses to call human.
    payloads = [p for (p,) in db.execute(
        "SELECT payload FROM events WHERE aggregate = 'overlay_fact' AND type = %s",
        (facts.OVERLAY_FACT_CONFIRMED,)).fetchall()]
    assert payloads, "the upload asserted no table facts — this test is not exercising the path"
    assert all(p.get("authority_basis") == facts.AUTHORITY_SOURCE_DECLARED for p in payloads)
    assert not any(p.get("confirmers") for p in payloads)


def test_an_AI_PROPOSED_grain_still_reaches_no_table_block_at_all(db):
    """THE GAP TASK 8 DOES NOT CLOSE, pinned so nobody believes it did.

    Pass B routes its grain/availability candidates into PROPOSED-only governed facts
    (`table_synth._propose_table_facts`); `resolve_fact` serves VERIFIED only; and `is_grain` is
    written by exactly two things — the FILE (`build_graph`) and the VERIFIED projection
    (`table_fact_projection`). So a grain the AI worked out and nobody confirmed sets no flag on
    `graph_node`, and NO relaxation of the fact-event test can surface it: there is nothing in the
    candidate row to relax. Reaching it needs a read of the PROPOSED stream."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay import facts
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.store import append_overlay_event
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref
    from featuregen.overlay.upload.upload_catalog import table_ref

    _sealed()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    service = mint_test_identity(subject="user:enrich_service", role_claims=("data_owner",))
    # The file declares NO grain — which is precisely when Pass B proposes one.
    rows = [CanonicalRow("propp", "facility_limits", "cust_id", "text"),
            CanonicalRow("propp", "facility_limits", "limit_amt", "numeric")]
    assert ingest_upload(db, "propp", rows, actor=owner, now=now).status == "ingested"

    fk = fact_key(table_ref("propp", "facility_limits"), "grain")
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED, actor=service, expected_version=0,
        payload={"catalog_object_ref": {"catalog_source": "propp", "object_kind": "table",
                                        "schema": "public", "table": "facility_limits"},
                 "object_ref": "public.facility_limits", "fact_type": "grain",
                 "proposed_value": {"columns": ["cust_id"], "is_unique": True},
                 "proposal_fingerprint": "fp", "proposed_by": service.subject})
    project_table_facts_for_ref(db, source="propp", table="facility_limits", now=now)

    assert not db.execute("SELECT 1 FROM graph_node WHERE catalog_source = 'propp' "
                          "AND kind = 'column' AND is_grain").fetchall()
    block = fa._table_context(fa._candidate_columns(db, "propp", roles=()))[0]
    assert block == {"table": "facility_limits"}, (
        "an AI-proposed grain now reaches the table block — if that is deliberate, this test is the "
        "place to say how")
