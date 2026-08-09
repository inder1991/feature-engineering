import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
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


def test_refine_at_v4_carries_the_human_instruction_with_no_grounding_rules(db, v5, monkeypatch):
    """The GROUNDING rules do not ride at v4 — the wire item has no such key, so asking would demand
    output the schema forbids.

    UPDATED BY TASK 8b, deliberately: the human's instruction is no longer alone. v4 DOES send a
    `table_context`, and those blocks carry `grain_status` / `as_of_status`, so the sentence
    defining those tokens travels with them at every version that sends them. The distinction this
    test protects is intact and is now asserted directly — the grounding clause stays behind its
    version gate while the vocabulary the payload actually contains does not."""
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
    assert seen[-1].startswith("tighten it")
    assert "grounding" not in seen[-1]
    assert seen[-1] == "tighten it" + fa._TABLE_CONTEXT_STATUS_DIRECTIVE


# ── Task 8/8b: an unconfirmed grain / as-of reaches the model, LABELLED BY WHO ASSERTED IT ───────
#
# Task 8 relaxed `_table_context`'s non-null `grain_fact_event_id` / `availability_fact_event_id`
# requirement so a file-declared grain no longer reached the generator as nothing. Its review then
# proved the relaxation had almost nothing behind it and the vocabulary it shipped was wrong:
#
#   * `ingest._assert_fact` AUTO-CONFIRMS every file-declared grain/as-of, so `confirmed` was worn
#     by an unreviewed CSV flag and by a human endorsement alike;
#   * a Pass B AI proposal never sets `is_grain` at all, so the case the plan exists for — "use the
#     AI's answer even though nobody has verified it" — still reached nothing.
#
# Task 8b replaces the pair with an AUTHORITY axis — WHO asserted this value — read from the fact
# stream beside the resolved projection. `resolve_fact` is untouched: it still refuses to serve a
# PROPOSED fact, which is what keeps the execution path honest.


def _ctx_row(column: str, **over) -> dict:
    """One candidate row in the shape `_candidate_columns` produces, grain/as-of unset by default."""
    row = {"catalog_source": "cib", "table": "facility_limits", "column": column,
           "is_grain": False, "grain_fact_event_id": None,
           "is_as_of": False, "availability_fact_event_id": None,
           "table_definition": None, "table_primary_entity": None}
    row.update(over)
    return row


def _authority(*, grain_human=False, grain_proposed=None,
               as_of_human=False, as_of_proposed=None,
               table="facility_limits", source="cib") -> dict:
    """The resolved per-table fact authority in the shape `_table_fact_authority` returns."""
    return {(source, table): {
        "grain": {"human": grain_human, "proposed": grain_proposed},
        "availability_time": {"human": as_of_human, "proposed": as_of_proposed}}}


def test_a_human_endorsed_grain_says_so():
    """The strongest statement the platform can make about a grain: a person with authority over
    this table looked at it and signed. It is the ONLY one of the three tokens that means review."""
    block = fa._table_context([_ctx_row("cust_id", is_grain=True, grain_fact_event_id="evt_1")],
                              authority=_authority(grain_human=True))[0]
    assert block["grain_columns"] == ["cust_id"]
    assert block["grain_status"] == "human_confirmed"


def test_an_auto_confirmed_source_grain_is_never_labelled_human():
    """THE FINDING TASK 8's REVIEW RAISED. A file-declared grain is auto-confirmed at ingest with
    `authority_basis=source_declared`, so it carries a fact event id and is fully operational — and
    nobody reviewed it. Task 8 called this `confirmed`, which is the word a reader takes for review.
    """
    block = fa._table_context([_ctx_row("cust_id", is_grain=True, grain_fact_event_id="evt_1")],
                              authority=_authority(grain_human=False))[0]
    assert block["grain_status"] == "source_declared"


def test_a_file_declared_grain_with_no_servable_fact_is_also_source_declared():
    """Task 8's `declared` case — the file's flag survived a drift-STALEd / never-projected fact.
    It collapses onto `source_declared` deliberately: the axis is WHO ASSERTED the value, and the
    answer is the same source in both. Whether the fact is currently SERVABLE is an execution
    question, and the execution path answers it from the fact stream, never from this block."""
    block = fa._table_context([_ctx_row("cust_id", is_grain=True)],
                              authority=_authority())[0]
    assert (block["grain_columns"], block["grain_status"]) == (["cust_id"], "source_declared")


def test_an_ai_proposed_grain_now_reaches_the_model_labelled():
    """WHAT THIS TASK DELIVERS. Nothing on the candidate row carries it — `is_grain` is false on
    every column — so the value comes from the PROPOSED fact stream, read beside the resolved one.
    """
    block = fa._table_context([_ctx_row("cust_id"), _ctx_row("limit_amt")],
                              authority=_authority(grain_proposed=["cust_id"]))[0]
    assert block["grain_columns"] == ["cust_id"]
    assert block["grain_status"] == "ai_proposed"


def test_an_ai_proposed_as_of_now_reaches_the_model_labelled():
    block = fa._table_context([_ctx_row("booked_at"), _ctx_row("amount")],
                              authority=_authority(as_of_proposed="booked_at"))[0]
    assert (block["as_of_column"], block["as_of_status"]) == ("booked_at", "ai_proposed")


def test_a_human_endorsed_as_of_says_so():
    block = fa._table_context(
        [_ctx_row("booked_at", is_as_of=True, availability_fact_event_id="evt_a")],
        authority=_authority(as_of_human=True))[0]
    assert (block["as_of_column"], block["as_of_status"]) == ("booked_at", "human_confirmed")


def test_an_auto_confirmed_source_as_of_is_never_labelled_human():
    block = fa._table_context(
        [_ctx_row("booked_at", is_as_of=True, availability_fact_event_id="evt_a")],
        authority=_authority(as_of_human=False))[0]
    assert block["as_of_status"] == "source_declared"


def test_a_file_declared_as_of_with_no_servable_fact_is_also_source_declared():
    block = fa._table_context([_ctx_row("booked_at", is_as_of=True)], authority=_authority())[0]
    assert (block["as_of_column"], block["as_of_status"]) == ("booked_at", "source_declared")


# ── PRECEDENCE: a confirmed value always wins, and the loser is never mentioned ──────────────────


def test_a_confirmed_grain_hides_the_ais_disagreeing_proposal():
    """THE DISAGREEMENT CASE. A human confirmed grain=(cust_id); the AI worked out
    (region, cust_id). The human's answer is what ships and the AI's is not mentioned at all — the
    model is choosing FEATURES, not adjudicating governance, and a block carrying both would invite
    it to pick. `region` must appear nowhere in the block, under any key."""
    block = fa._table_context([_ctx_row("cust_id", is_grain=True, grain_fact_event_id="evt_1"),
                               _ctx_row("region")],
                              authority=_authority(grain_human=True,
                                                   grain_proposed=["region", "cust_id"]))[0]
    assert (block["grain_columns"], block["grain_status"]) == (["cust_id"], "human_confirmed")
    assert "region" not in repr(block)


def test_a_file_declaration_outranks_an_ai_proposal():
    """The other half of the same rule, and the one that decides which of the two UNREVIEWED
    assertions travels. The source shipped the data; the model read its column names."""
    block = fa._table_context([_ctx_row("cust_id", is_grain=True), _ctx_row("region")],
                              authority=_authority(grain_proposed=["region"]))[0]
    assert (block["grain_columns"], block["grain_status"]) == (["cust_id"], "source_declared")
    assert "region" not in repr(block)


def test_a_confirmed_as_of_hides_the_ais_disagreeing_proposal():
    block = fa._table_context(
        [_ctx_row("z_ts", is_as_of=True, availability_fact_event_id="evt_a"), _ctx_row("a_ts")],
        authority=_authority(as_of_human=True, as_of_proposed="a_ts"))[0]
    assert (block["as_of_column"], block["as_of_status"]) == ("z_ts", "human_confirmed")
    assert "a_ts" not in repr(block)


def test_the_two_axes_are_labelled_independently():
    """A table can have a human-endorsed grain and only an AI guess at its time anchor. One status
    per axis, never one verdict for the table."""
    block = fa._table_context(
        [_ctx_row("cust_id", is_grain=True, grain_fact_event_id="evt_1"), _ctx_row("booked_at")],
        authority=_authority(grain_human=True, as_of_proposed="booked_at"))[0]
    assert block["grain_status"] == "human_confirmed"
    assert block["as_of_status"] == "ai_proposed"


def test_an_ai_proposal_naming_a_column_the_caller_cannot_SEE_is_dropped_whole():
    """M6 READ SCOPE — the one way this task could leak. The confirmed and file-declared grains are
    built FROM the candidate rows, which are already read-scoped, so their column names can only be
    names the caller was offered. The AI's proposal is the only value that arrives from OUTSIDE that
    set: it was validated against the table at propose time, and a sensitivity tag added since (or a
    thinner caller) can have removed one of its columns from the menu.

    Emitting it anyway would put a column name the caller may not see into the prompt AND hand the
    model a reference it cannot ground. The whole proposal drops — a compound grain half-emitted is
    a different table."""
    block = fa._table_context([_ctx_row("cust_id")],
                              authority=_authority(grain_proposed=["cust_id", "secret_col"]))[0]
    assert "grain_columns" not in block and "grain_status" not in block
    assert "secret_col" not in repr(block)
    # the single-column axis takes the same rule
    hidden = fa._table_context([_ctx_row("cust_id")],
                               authority=_authority(as_of_proposed="secret_ts"))[0]
    assert "as_of_column" not in hidden and "secret_ts" not in repr(hidden)


def test_the_source_declared_SENTENCE_is_true_in_the_state_that_degrades_to_it(db):
    """PIN THE SENTENCE, NOT JUST THE LABEL — review's finding, and the sharper half of it.

    `_table_fact_authority` is fail-soft, so an unreadable stream degrades a GENUINELY
    human-confirmed grain to `source_declared`. The first draft of the directive defined that token
    as "the uploaded catalog file asserted it, so NOBODY has reviewed it". In this state that is not
    a weaker claim than the truth — it is a DIFFERENT and FALSE one: it asserts a fact about a file
    that never declared anything, and denies a review that actually happened.

    This test drives the hardest of those states — a real human confirmation through the real
    four-eyes gate, with the read then forced to fail — and asserts the label AND the sentence
    attached to it together, because the label alone was already green while the sentence was false.

    UPDATED IN ROUND 2, deliberately: this test pinned "NO HUMAN REVIEW IS RECORDED", which review
    then showed was ITSELF false in a state neither round had walked — a REJECTED re-verification,
    where a human review is emphatically recorded because it is the refusal. The claim is now about
    a sign-off that STANDS. See `test_a_REJECTED_re_verification_is_NOT_called_human_confirmed` and
    `test_the_source_declared_sentence_holds_for_a_REPUDIATED_signature`."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity
    from tests.featuregen.overlay.upload.conftest import _confirm_grain

    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref

    _sealed()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
    rows = [CanonicalRow("degp", "facility_limits", "cust_id", "text"),
            CanonicalRow("degp", "facility_limits", "limit_amt", "numeric")]
    assert ingest_upload(db, "degp", rows, actor=owner, now=now).status == "ingested"
    _propose_ai_grain(db, "degp", "facility_limits", ["cust_id"])
    _confirm_grain(db, "degp", "facility_limits", ["cust_id"], actor=admin)
    project_table_facts_for_ref(db, source="degp", table="facility_limits", now=now)

    cols = fa._candidate_columns(db, "degp", roles=())
    # A HUMAN really did confirm this grain — established through the real gate, not asserted.
    healthy = fa._table_fact_authority(db, cols)
    assert healthy[("degp", "facility_limits")]["grain"]["human"] is True

    class _Broken:
        """A conn whose event read raises — the fail-soft path, reached without patching the code."""

        def __getattr__(self, name):
            return getattr(db, name)

        def execute(self, *_a, **_kw):
            raise RuntimeError("event stream unreadable")

    degraded = fa._table_fact_authority(_Broken(), cols)
    assert degraded == {}, "the fail-soft path did not trigger — this test proves nothing"
    block = fa._table_context(cols, authority=degraded)[0]
    assert block["grain_status"] == "source_declared"

    # …and THIS is what the model is told that token means. The claim must be one the platform can
    # evidence — here, that no sign-off STANDS, which is true when none can be read.
    directive = fa._table_context_directive([block], cites_grounding=True)
    assert "NO HUMAN SIGN-OFF STANDS" in directive
    # Both superseded drafts, each false in a state that really produces this token.
    assert "NOBODY has reviewed it" not in directive
    assert "the uploaded catalog file asserted it, so" not in directive
    assert "NO HUMAN REVIEW IS RECORDED" not in directive


def test_an_unresolved_authority_never_claims_a_human_endorsement():
    """THE DEGRADED PATH. The fact-stream read is fail-soft (an unreadable stream must not take
    down feature generation), so the block can be built with no authority at all. It then falls to
    the WEAKER claim, in the same direction `_human_reviewed` itself fails: unknown is not human.
    Under-claiming makes a reviewed grain look unreviewed; over-claiming hands the model confidence
    nobody earned, which is the defect this task exists to fix."""
    block = fa._table_context([_ctx_row("cust_id", is_grain=True, grain_fact_event_id="evt_1")])[0]
    assert block["grain_status"] == "source_declared"


def test_a_table_with_neither_carries_neither_the_value_nor_a_status():
    """HONEST ABSENCE. A status with no value beside it would be a claim about a grain that does not
    exist, and `no grain at all` is a different statement from `an unconfirmed grain`."""
    block = fa._table_context([_ctx_row("amount")])[0]
    assert not {"grain_columns", "grain_status", "as_of_column", "as_of_status"} & set(block)


def test_a_confirmed_grain_is_never_widened_by_a_file_declaration():
    """THE CONFIGURATION TASK 8's RELAXATION COULD HAVE BROKEN — a human confirmed something the
    file disagrees with. `project_table_facts_for_ref` SPARES file-declared columns from its clear,
    so a table whose file declares (cust_id, region) while a human confirmed grain=(cust_id) really
    does carry is_grain on both, one stamped and one not.

    Emitting the UNION there would assert a grain nobody attested — not the file's and not the
    human's — and would label the human's own answer as unreviewed on the way past. A confirmation
    is the stronger statement, so where any confirmation exists the confirmed set wins.

    UPDATED FOR 8b, deliberately: the assertion that used to read `confirmed` now reads
    `human_confirmed`, because this test's own premise is that a HUMAN confirmed it. Task 8 could
    not say that; it had only one word for both confirmers."""
    cols = [_ctx_row("cust_id", is_grain=True, grain_fact_event_id="evt_1"),
            _ctx_row("region", is_grain=True)]
    block = fa._table_context(cols, authority=_authority(grain_human=True))[0]
    assert block["grain_columns"] == ["cust_id"]
    assert block["grain_status"] == "human_confirmed"


def test_a_confirmed_as_of_wins_over_a_declared_one():
    """Same rule on the time axis, and the ordering makes it bite: `a_ts` sorts first, so a naive
    `first as-of column` pick would hand the model the UNCONFIRMED one and call it the anchor."""
    cols = [_ctx_row("a_ts", is_as_of=True),
            _ctx_row("z_ts", is_as_of=True, availability_fact_event_id="evt_a")]
    block = fa._table_context(cols, authority=_authority(as_of_human=True))[0]
    assert (block["as_of_column"], block["as_of_status"]) == ("z_ts", "human_confirmed")


def test_a_multi_column_declared_grain_is_carried_whole_and_marked_source_declared():
    """Nothing is confirmed here, so the whole file-declared compound grain travels — sorted, and
    with one status for the set. A compound grain half-emitted is a different table."""
    cols = [_ctx_row("region", is_grain=True), _ctx_row("cust_id", is_grain=True)]
    block = fa._table_context(cols, authority=_authority())[0]
    assert block["grain_columns"] == ["cust_id", "region"]
    assert block["grain_status"] == "source_declared"


def test_a_multi_column_ai_proposed_grain_is_carried_whole_and_sorted():
    """The AI's compound grain travels under the same rule as the file's — whole, sorted, one
    status for the set. The sort matters: the proposal arrives in the model's own order and two
    equivalent grains must not read as two different tables."""
    block = fa._table_context([_ctx_row("cust_id"), _ctx_row("region")],
                              authority=_authority(grain_proposed=["region", "cust_id"]))[0]
    assert block["grain_columns"] == ["cust_id", "region"]
    assert block["grain_status"] == "ai_proposed"


def test_the_two_status_keys_are_egress_classified(monkeypatch):
    """The landmine every payload task in this plan has hit. Neither key appears in any
    `_TABLE_CONTEXT_*` classifier by default and the adapter's terminal branch returns None, so
    emitting one unclassified refuses the WHOLE payload — not the field. Each is pinned against the
    list it ACTUALLY belongs to, so this fails both if someone drops one and if someone re-grades
    one.

    Task 8b adds NO key — it widens the value set of these two — so this pin is the one that proves
    the widening needed no new classification. The AI-proposed block is included so the assertion
    covers the values that did not exist when the pin was written."""
    from featuregen.overlay.upload import enrich_llm
    from featuregen.overlay.upload.enrich_llm import sanitize_feature_context

    blocks = fa._table_context([_ctx_row("cust_id", is_grain=True),
                               _ctx_row("booked_at", is_as_of=True)])
    blocks += fa._table_context(
        [_ctx_row("cust_id", table="proposed_only"), _ctx_row("booked_at", table="proposed_only")],
        authority=_authority(grain_proposed=["cust_id"], as_of_proposed="booked_at",
                             table="proposed_only"))
    assert {"grain_status", "as_of_status"} <= set(blocks[0])
    assert blocks[1]["grain_status"] == "ai_proposed"
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
    """THREE values, all short tokens, all naming an AUTHORITY. Anything else on this key is a value
    the model cannot weigh — and one the directive below does not define, which is worse.

    Driven across every state that can produce a status, not a hand-written list: whatever the
    emitter can put on these keys has to be inside the closed set the module publishes."""
    blocks = fa._table_context([_ctx_row("cust_id", is_grain=True),
                               _ctx_row("booked_at", is_as_of=True,
                                        availability_fact_event_id="evt_a")])
    blocks += fa._table_context(
        [_ctx_row("cust_id", table="t2", is_grain=True, grain_fact_event_id="e"),
         _ctx_row("booked_at", table="t2", is_as_of=True, availability_fact_event_id="e")],
        authority=_authority(grain_human=True, as_of_human=True, table="t2"))
    blocks += fa._table_context(
        [_ctx_row("cust_id", table="t3"), _ctx_row("booked_at", table="t3")],
        authority=_authority(grain_proposed=["cust_id"], as_of_proposed="booked_at", table="t3"))
    statuses = {b.get("grain_status") for b in blocks} | {b.get("as_of_status") for b in blocks}
    assert statuses - {None} == fa.TABLE_FACT_STATUSES
    assert fa.TABLE_FACT_STATUSES == {"human_confirmed", "source_declared", "ai_proposed"}


def test_every_status_token_is_defined_for_the_model_in_the_directive():
    """A TOKEN WHOSE MEANING LIVES ONLY IN A PYTHON DOCSTRING IS NOT LABELLING. Task 8's review
    found the payload carried these tokens as bare JSON with no prompt text defining them anywhere,
    so the model read the English word and took `confirmed` at face value. Every member of the
    closed vocabulary must appear in the sentence that travels with the payload."""
    directive = fa._table_context_directive(
        [{"table": "t", "grain_status": "ai_proposed"}], cites_grounding=True)
    assert directive, "the table-context directive is empty — the tokens reach the model undefined"
    for token in fa.TABLE_FACT_STATUSES:
        assert token in directive, f"{token} is emitted but never explained to the model"


def test_the_directive_is_absent_when_no_table_context_travels():
    """Flag-off (`feature_context_enabled()` false) sends NO table_context at all, and a sentence
    explaining keys that are not in the payload is noise the egress scanner still has to walk. The
    instruction is then BYTE-IDENTICAL to its pre-8b form — asserted against the composition, not
    against itself."""
    assert fa._table_context_directive([], cites_grounding=True) == ""
    assert fa._generation_instruction("predict churn", table_context=[]) == (
        "predict churn" + fa._DERIVES_FROM_DIRECTIVE + fa._grounding_directive())


def test_the_directive_travels_when_a_table_context_does():
    """…and when a block IS sent, the sentence rides with it, appended after the redacted objective
    as a fixed PII-free constant so the egress guard still scans it and llm_call records it."""
    instruction = fa._generation_instruction(
        "predict churn", table_context=[{"table": "t", "grain_status": "source_declared"}])
    assert instruction.startswith("predict churn")
    assert fa._table_context_directive([{"table": "t"}], cites_grounding=True) in instruction


def test_EVERY_call_that_sends_a_table_context_also_sends_the_vocabulary(db, v5):
    """THE DEFECT REVIEW FOUND: `feature_recipe` put `table_context` in its inputs and called the
    model with the raw `nl_query`, so the tokens travelled as bare JSON on one surface of three —
    the exact failure Task 8's review raised, surviving in the change that was meant to fix it. It
    is reachable from `POST /features/recipe`, and `Recipe.grain_table` / `as_of_column` come back
    to the caller looking grounded.

    Driven across ALL THREE surfaces rather than the two I remembered, because "every call site"
    was a claim I made and had not checked."""
    from featuregen.overlay.upload.feature_assist import feature_recipe, refine_idea

    _bank_graph(db)
    seen: list[tuple[dict, str]] = []

    class _Client:
        def call(self, request):
            from featuregen.intake.llm import LLMResult
            seen.append((request.inputs, request.inputs["redacted_intent"]))
            return LLMResult(output={"features": [_idea()], "grain_table": "accounts",
                                     "derives_from": [_AMOUNT], "aggregation": "sum_90d"},
                             self_reported_scores={}, call_ref="", status="ok")

    recommend_features(db, "predict churn", _Client(), catalog_source="bank", budget=1,
                       critic=False)
    refine_idea(db, {"name": "orig", "derives_from": [_AMOUNT]}, "tighten it", _Client(),
                catalog_source="bank")
    feature_recipe(db, "total spend per account", _Client(), catalog_source="bank")

    # Read off the WIRE (`catalog_metadata` is where `build_llm_inputs` puts it), so this checks
    # what actually egressed rather than what the caller believed it assembled.
    carried = [(inputs["catalog_metadata"]["table_context"], intent) for inputs, intent in seen
               if inputs.get("catalog_metadata", {}).get("table_context")]
    assert len(carried) >= 3, "a surface stopped sending table_context — re-derive this test"
    checked = 0
    for blocks, intent in carried:
        statuses = ({b.get("grain_status") for b in blocks}
                    | {b.get("as_of_status") for b in blocks}) - {None}
        assert statuses, "the bank fixture stopped producing a status — this test proves nothing"
        for token in statuses:
            assert token in intent, (
                f"a call sent {token!r} in table_context with no definition in its instruction")
            checked += 1
    assert checked >= 3
    # NAMED, not counted: the recipe call is the surface that was broken, so this test must fail if
    # it stops being exercised rather than quietly passing on the two that were always fine.
    assert any(intent.startswith("total spend per account") for _blocks, intent in carried), (
        "the feature_recipe surface is not in this sample — the regression it pins is unguarded")


def test_the_recipe_call_is_not_told_to_write_a_grounding_entry(db, v5):
    """…and the fix must not repeat the defect it fixes. The recipe RESPONSE contract is
    {grain_table, join_table, derives_from, aggregation, as_of_column} — `Recipe` reads it key by
    key and there is no `grounding` array at any version. Telling this call to write one would spend
    its output on a field nothing reads, which is the same prompt/schema inversion as demanding
    output the wire item forbids, arriving by the other door."""
    from featuregen.overlay.upload.feature_assist import feature_recipe

    _bank_graph(db)
    seen: list[str] = []

    class _Client:
        def call(self, request):
            from featuregen.intake.llm import LLMResult
            seen.append(request.inputs["redacted_intent"])
            return LLMResult(output={"grain_table": "accounts", "derives_from": [_AMOUNT]},
                             self_reported_scores={}, call_ref="", status="ok")

    feature_recipe(db, "total spend per account", _Client(), catalog_source="bank")
    assert seen[-1].startswith("total spend per account")
    assert "grounding" not in seen[-1]
    # …while the tokens it DOES send are still defined.
    assert "source_declared" in seen[-1]


def test_the_grounding_clause_asks_about_ALL_THREE_statuses_not_only_the_weak_ones(v5):
    """THE SECOND-ORDER HALF of review's third finding. The clause used to say "where a feature
    rests on a grain that is NOT `human_confirmed`, say so", which made `human_confirmed` the SILENT
    DEFAULT — a reader could not tell a signed grain from one whose status the model simply never
    mentioned.

    That mattered most in exactly the state the same round fixed: a human-signed grain whose fact has
    lapsed is labelled `human_confirmed`, so under the old clause the lapse vanished from the label
    AND from the audit trail in one breath. Naming the status whichever it is costs one short phrase
    and makes silence mean something again."""
    directive = fa._table_context_directive(
        [{"table": "t", "grain_status": "human_confirmed"}], cites_grounding=True)
    assert fa._TABLE_CONTEXT_STATUS_GROUNDING_CLAUSE in directive
    clause = fa._TABLE_CONTEXT_STATUS_GROUNDING_CLAUSE
    assert "whichever of the three it is" in clause
    assert "is not `human_confirmed`" not in clause, (
        "the clause once again asks only about the weak statuses, making a signed grain silent")
    assert "`human_confirmed`" in clause   # …and says so explicitly rather than by omission


def test_the_directives_grounding_clause_is_version_gated(v5, monkeypatch):
    """THE DEFECT THIS PIN EXISTS FOR, found by running the suite rather than by reading the code.

    The vocabulary sentence originally ended "…say which you relied on in `grounding`", which asks
    for output the v4 wire item FORBIDS — the exact prompt/schema inversion `_grounding_directive`
    was written to prevent, reintroduced through a different door. The clause is now split out and
    gated off `_grounding_directive()`'s own emptiness, so the two cannot drift apart.

    The token DEFINITIONS still travel at v4, because v4 does send `table_context` and those tokens
    really are in the payload."""
    blocks = [{"table": "t", "grain_status": "source_declared"}]
    assert "grounding" in fa._table_context_directive(blocks, cites_grounding=True)  # v5 fixture
    monkeypatch.setenv(fa.FEATURE_CONTEXT_VERSION_ENV, "4")
    at_v4 = fa._table_context_directive(blocks, cites_grounding=True)
    assert "grounding" not in at_v4
    assert fa.TABLE_FACT_STATUSES <= {t for t in fa.TABLE_FACT_STATUSES if t in at_v4}
    # The OTHER gate, at v5 where the version test alone would let the clause through: a call whose
    # response contract has no `grounding` array must not be told to write one.
    monkeypatch.delenv(fa.FEATURE_CONTEXT_VERSION_ENV, raising=False)
    assert "grounding" not in fa._table_context_directive(blocks, cites_grounding=False)


def test_the_directive_is_skipped_for_blocks_that_carry_no_status():
    """A table_context can travel with no grain or as-of on ANY block (every table abstained). The
    tokens are then not in the payload, so defining them is the same noise as flag-off."""
    assert fa._table_context_directive(
        [{"table": "t", "table_definition": "a table"}], cites_grounding=True) == ""


# ── the FOUR states, each driven through REAL code against a REAL database ───────────────────────
#
# The unit tests above hand `_table_context` a hand-built authority map. These build the state with
# the real ingest / propose / confirm / project paths and read it back through the real resolver, so
# a resolver that mislabels cannot pass by agreeing with a fixture.
#
#   human-endorsed  -> test_a_humanly_confirmed_grain_reads_human_confirmed_end_to_end
#   source-declared -> test_an_ordinary_uploads_declared_grain_reads_source_declared_not_human
#   AI-proposed     -> test_an_AI_PROPOSED_grain_now_REACHES_the_table_block
#   absent          -> test_a_table_with_no_grain_and_no_proposal_still_carries_neither


def _sealed() -> None:
    from datetime import timedelta

    from featuregen.overlay.config import OverlayConfig, register_overlay_config

    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


#: The Pass B service proposer — a NON-human actor, exactly as `enrich_llm._ENRICH_ACTOR` is.
_SERVICE = IdentityEnvelope(subject="featuregen-overlay-enrichment", actor_kind="service",
                            authenticated=True, auth_method="internal", role_claims=())


def _drain_overlay(conn) -> None:
    """Bring the overlay read model to head — `resolve_fact` reads `overlay_fact_state`, which only
    the projection populates (mirrors `conftest._drain`)."""
    from featuregen.overlay.projection import OverlayProjection
    from featuregen.projections.runner import run_projection

    while run_projection(conn, OverlayProjection()) >= 500:
        pass


def _resolved_block(db, source: str, table_index: int = 0) -> dict:
    """The table block the REAL assembly would send: real candidate rows, real authority resolver."""
    cols = fa._candidate_columns(db, source, roles=())
    return fa._table_context(cols, authority=fa._table_fact_authority(db, cols))[table_index]


def _propose_ai_grain(db, source: str, table: str, columns: list[str], *,
                      as_of: str | None = None) -> None:
    """Seed a Pass B proposal through the REAL `_propose_table_facts` under the REAL service actor —
    the same call `ingest`'s Pass B block makes (`ingest.py:2777`)."""
    from featuregen.overlay.upload.table_synth import _propose_table_facts

    syn = {"grain": {"columns": columns, "is_unique": True} if columns else None,
           "availability_time": ({"column": as_of, "basis": "posted_at"} if as_of else None),
           "table_role": None, "primary_entity": None}
    _propose_table_facts(db, source, {table: syn}, actor=_SERVICE,
                         source_snapshot_id="snap-8b")


def test_an_ordinary_uploads_declared_grain_reads_source_declared_not_human(db):
    """SOURCE-DECLARED, end to end. `ingest._assert_fact` auto-confirms a file-declared grain/as-of
    with `authority_basis=source_declared` ("the fact is authoritative because the SOURCE declared
    it"), so this upload's grain is VERIFIED and fully operational with nobody having reviewed it.

    Task 8 labelled it `confirmed`, which is the word a reader takes for review. It now reads
    `source_declared`, and the test asserts the STREAM says the same thing — the label and the
    evidence for it are checked together."""
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

    block = _resolved_block(db, "authp")
    assert (block["grain_columns"], block["grain_status"]) == (["bal_id"], "source_declared")
    assert (block["as_of_column"], block["as_of_status"]) == ("snap_ts", "source_declared")
    # …and the label is not a guess: the CONFIRMED event carries a source-declared authority basis
    # and no confirmers at all, which is exactly what `_human_reviewed` refuses to call human.
    payloads = [p for (p,) in db.execute(
        "SELECT payload FROM events WHERE aggregate = 'overlay_fact' AND type = %s",
        (facts.OVERLAY_FACT_CONFIRMED,)).fetchall()]
    assert payloads, "the upload asserted no table facts — this test is not exercising the path"
    assert all(p.get("authority_basis") == facts.AUTHORITY_SOURCE_DECLARED for p in payloads)
    assert not any(p.get("confirmers") for p in payloads)
    # The grain IS operational — this is not the unservable case. Both states say `source_declared`
    # because the axis is WHO ASSERTED it, and the execution gate reads the fact, never this block.
    assert db.execute("SELECT 1 FROM graph_node WHERE catalog_source = 'authp' AND kind = 'column' "
                      "AND is_grain AND grain_fact_event_id IS NOT NULL").fetchone() is not None


def test_a_humanly_confirmed_grain_reads_human_confirmed_end_to_end(db):
    """HUMAN-ENDORSED, end to end, through the REAL four-eyes gate: the service proposes, a
    platform-admin HUMAN confirms via the real `confirm_fact` command, the projection stamps
    `graph_node`. This is the ONLY state that may claim review, and it is the one Task 8 could not
    distinguish from the test above."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity
    from tests.featuregen.overlay.upload.conftest import _confirm_grain

    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref

    _sealed()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
    rows = [CanonicalRow("humanp", "facility_limits", "cust_id", "text"),
            CanonicalRow("humanp", "facility_limits", "limit_amt", "numeric")]
    assert ingest_upload(db, "humanp", rows, actor=owner, now=now).status == "ingested"

    _propose_ai_grain(db, "humanp", "facility_limits", ["cust_id"])
    _confirm_grain(db, "humanp", "facility_limits", ["cust_id"], actor=admin)
    project_table_facts_for_ref(db, source="humanp", table="facility_limits", now=now)

    block = _resolved_block(db, "humanp")
    assert (block["grain_columns"], block["grain_status"]) == (["cust_id"], "human_confirmed")


def _human_confirmed_grain(db, source: str, *, now):
    """A grain a HUMAN signed through the real four-eyes gate, projected onto graph_node.

    The shared setup for the lapse tests below: each one then drives the fact OUT of VERIFIED and
    asserts what the block says about a signature that is still stamped on the row."""
    from tests.featuregen._helpers import mint_test_identity
    from tests.featuregen.overlay.upload.conftest import _confirm_grain

    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref

    _sealed()
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
    rows = [CanonicalRow(source, "facility_limits", "cust_id", "text"),
            CanonicalRow(source, "facility_limits", "limit_amt", "numeric")]
    assert ingest_upload(db, source, rows, actor=owner, now=now).status == "ingested"
    _propose_ai_grain(db, source, "facility_limits", ["cust_id"])
    _confirm_grain(db, source, "facility_limits", ["cust_id"], actor=admin)
    project_table_facts_for_ref(db, source=source, table="facility_limits", now=now)
    assert db.execute(
        "SELECT 1 FROM graph_node WHERE catalog_source = %s AND kind = 'column' AND is_grain "
        "AND grain_fact_event_id IS NOT NULL", (source,)).fetchone() is not None
    return admin


def test_a_human_signed_grain_that_EXPIRED_still_says_a_human_signed_it(db):
    """THE FOURTH PRODUCING STATE — the one two rounds of review and I all walked past.

    A grain a human signs is stamped onto `graph_node` with THAT HUMAN'S confirmed-event id. When
    the TTL timer armed at confirm time fires, the fact folds to REVERIFY — and NOTHING clears the
    stamp: `expiry` demotes join edges and semantic bindings, the overlay projection touches only
    `overlay_fact_state`/`overlay_proposal`, and `stamp_reconcile` says the drift is "left
    drifted-and-visible rather than force-stamped or, worse, wiped" BY DESIGN.

    So the row still carries the human's own signature, `_grain_block` still takes the confirmed
    branch off it — and the first version of Task 8b answered `source_declared`, defined to the
    model as "NO HUMAN REVIEW IS RECORDED for it, which usually means the uploaded catalog file
    asserted it". Both halves false: the review IS recorded, and its event id is stamped on the very
    row the block was built from.

    Expiry is a TTL lapse, not a repudiation. The signature stands, so the label follows it. Whether
    the fact can currently EXECUTE is the other axis, and `resolve_fact` — untouched — still says no.
    """
    from datetime import UTC, datetime, timedelta

    from featuregen.overlay.expiry import fire_due_overlay_expiries

    now = datetime(2026, 8, 1, tzinfo=UTC)
    _human_confirmed_grain(db, "expp", now=now)

    assert fire_due_overlay_expiries(db, now=now + timedelta(days=4000)) >= 1
    _drain_overlay(db)

    cols = fa._candidate_columns(db, "expp", roles=())
    authority = fa._table_fact_authority(db, cols)
    assert authority[("expp", "facility_limits")]["grain"]["human"] is True
    block = fa._table_context(cols, authority=authority)[0]
    assert (block["grain_columns"], block["grain_status"]) == (["cust_id"], "human_confirmed")

    # AND THE CLAUSE MUST BE TRUE HERE TOO — the third breach of the clause-truth rule, and the
    # first in the OVER-claim direction, which this task calls the dangerous one. The fix that
    # routed this state into `human_confirmed` also wrote "and that sign-off STILL STANDS", which is
    # false in exactly the state it created: the fold has moved the signed value to `prior_value`,
    # the status is in `_AWAITING_CONFIRMATION`, a re-verify task is open and `resolve_fact` refuses
    # to serve it. The platform can EVIDENCE the lapse. What it may claim is only that nobody
    # WITHDREW the signature.
    directive = fa._table_context_directive([block], cites_grounding=True)
    assert "has not been withdrawn" in directive
    assert "still stands" not in directive, (
        "the label tells the model a lapsed sign-off is still in force")


def test_a_human_signed_grain_that_drift_STALED_still_says_a_human_signed_it(db):
    """The other routine way a signed fact leaves VERIFIED — the background drift scan, which runs
    outside any ingest. Same stamp, same rule."""
    from datetime import UTC, datetime

    from featuregen.overlay import facts
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import append_overlay_event, load_fact
    from featuregen.overlay.upload.upload_catalog import table_ref

    now = datetime(2026, 8, 1, tzinfo=UTC)
    admin = _human_confirmed_grain(db, "stalep", now=now)

    fk = fact_key(table_ref("stalep", "facility_limits"), "grain")
    stream = load_fact(db, fk)
    # The SAME event `catalog_changes._stale_one` appends, with the same payload key.
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_STALED, actor=admin,
        expected_version=stream[-1].stream_version,
        payload={"catalog_change_ref": "public.facility_limits",
                 "stales_confirmed_event_id": fold_overlay_state(stream).confirmed_event_id})
    assert fold_overlay_state(load_fact(db, fk)).status == "STALE"

    cols = fa._candidate_columns(db, "stalep", roles=())
    block = fa._table_context(cols, authority=fa._table_fact_authority(db, cols))[0]
    assert block["grain_status"] == "human_confirmed"


def test_a_REJECTED_re_verification_is_NOT_called_human_confirmed(db):
    """AND THE LINE THE FIX MUST NOT CROSS. `_AWAITING_CONFIRMATION` includes REVERIFY, so a human
    can REFUSE the re-confirmation of a grain they once signed — and the stamp survives that too.

    A lapse and a refusal are not the same event. Expiry retires a signature by the clock; rejection
    is a person saying THIS VALUE IS WRONG. Following the stamp blindly would answer
    `human_confirmed` — "a person reviewed and signed it" — for a value a person explicitly refused,
    which is the strongest claim in the vocabulary attached to the weakest evidence for it.

    So the endorsement must STAND, not merely have existed. This is the over-claim guard for the
    fix to the two tests above."""
    from datetime import UTC, datetime, timedelta

    from tests.featuregen.overlay.upload.conftest import _reject_grain

    from featuregen.overlay.expiry import fire_due_overlay_expiries

    now = datetime(2026, 8, 1, tzinfo=UTC)
    admin = _human_confirmed_grain(db, "rejvp", now=now)

    assert fire_due_overlay_expiries(db, now=now + timedelta(days=4000)) >= 1
    _drain_overlay(db)
    _reject_grain(db, "rejvp", "facility_limits", actor=admin)

    cols = fa._candidate_columns(db, "rejvp", roles=())
    authority = fa._table_fact_authority(db, cols)
    assert authority[("rejvp", "facility_limits")]["grain"]["human"] is False
    block = fa._table_context(cols, authority=authority)[0]
    assert block["grain_status"] == "source_declared", (
        "a value a human REFUSED was labelled as one they signed")

    # …AND THE SENTENCE MUST BE TRUE FOR THE STATE JUST CONSTRUCTED. This assertion used to live in
    # a test NAMED for a repudiated signature that hand-built a block and drove no such thing —
    # review's finding, and a test named for a state it does not construct is precisely how "true in
    # the states I enumerated" defects propagate. It is now attached to the real one.
    directive = fa._table_context_directive([block], cites_grounding=True)
    assert "NO HUMAN SIGN-OFF STANDS" in directive
    # Every superseded draft, each false in a state that really produces this token.
    assert "NO HUMAN REVIEW IS RECORDED" not in directive   # false HERE: the refusal IS a review
    assert "NOBODY has reviewed it" not in directive


def test_a_human_signed_AS_OF_that_lapsed_is_labelled_like_its_grain_twin(db):
    """THE RESIDUAL I FLAGGED AND REVIEW ASKED ME TO CLOSE. `_as_of_block` reads the same `human`
    flag `_grain_block` does, and `_table_fact_authority` branches on `state.status` alone with no
    per-fact-type logic — so divergence between the two axes is structurally impossible. That is a
    one-line invariant, and until now nothing tested it: every lapse test drove the GRAIN axis.

    Drives the availability_time fact through the same TTL expiry and asserts the as-of column
    reports what its grain twin reports."""
    from datetime import UTC, datetime, timedelta

    from tests.featuregen._helpers import mint_test_identity
    from tests.featuregen.overlay.upload.conftest import _open_grain_task

    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.commands import confirm_fact
    from featuregen.overlay.expiry import fire_due_overlay_expiries
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref

    _sealed()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
    rows = [CanonicalRow("aslapse", "facility_limits", "cust_id", "text"),
            CanonicalRow("aslapse", "facility_limits", "booked_at", "timestamp")]
    assert ingest_upload(db, "aslapse", rows, actor=owner, now=now).status == "ingested"

    # BOTH axes proposed by the service and signed by the same human, through the real gate.
    _propose_ai_grain(db, "aslapse", "facility_limits", ["cust_id"], as_of="booked_at")
    for fact_type, value in (("grain", {"columns": ["cust_id"], "is_unique": True}),
                             ("availability_time", {"column": "booked_at", "basis": "posted_at"})):
        _task, target, ref = _open_grain_task(db, "aslapse", "facility_limits", actor=admin,
                                              fact_type=fact_type)
        res = confirm_fact(db, Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ref, "fact_type": fact_type, "target_event_id": target, "value": value},
            admin, f"confirm-{target}"))
        assert res.accepted, res.denied_reason
    _drain_overlay(db)
    project_table_facts_for_ref(db, source="aslapse", table="facility_limits", now=now)

    cols = fa._candidate_columns(db, "aslapse", roles=())
    signed = fa._table_context(cols, authority=fa._table_fact_authority(db, cols))[0]
    assert signed["grain_status"] == signed["as_of_status"] == "human_confirmed"

    # …and after the TTL fires on BOTH facts the two axes still agree.
    assert fire_due_overlay_expiries(db, now=now + timedelta(days=4000)) >= 2
    _drain_overlay(db)
    lapsed = fa._table_context(cols, authority=fa._table_fact_authority(db, cols))[0]
    assert lapsed["as_of_column"] == "booked_at"
    assert lapsed["grain_status"] == lapsed["as_of_status"] == "human_confirmed", (
        "the two axes diverged — `_table_fact_authority` branches on status alone, so this can only "
        "mean per-fact-type logic crept into one of them")


def test_an_AI_PROPOSED_grain_now_REACHES_the_table_block(db):
    """WHAT TASK 8b DELIVERS — the inverse of the Task-8 pin this replaces.

    Pass B routes its grain/availability candidates into PROPOSED-only governed facts; `resolve_fact`
    serves VERIFIED only; `is_grain` is written by exactly two things — the FILE (`build_graph`) and
    the VERIFIED projection. So the candidate row is still bare, and this test asserts that. The
    grain now travels anyway, read from the PROPOSED stream beside the resolved projection and
    labelled `ai_proposed`."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref

    _sealed()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    # The file declares NO grain and NO as-of — which is precisely when Pass B proposes them.
    rows = [CanonicalRow("propp", "facility_limits", "cust_id", "text"),
            CanonicalRow("propp", "facility_limits", "booked_at", "timestamp"),
            CanonicalRow("propp", "facility_limits", "limit_amt", "numeric")]
    assert ingest_upload(db, "propp", rows, actor=owner, now=now).status == "ingested"

    _propose_ai_grain(db, "propp", "facility_limits", ["cust_id"], as_of="booked_at")
    project_table_facts_for_ref(db, source="propp", table="facility_limits", now=now)

    # THE LINE THAT MUST NOT MOVE: the execution path is unchanged. A PROPOSED fact still sets no
    # flag, so nothing that reads `graph_node` — the compiler, the spine, the binding projection —
    # can see this grain. Only the prompt does.
    assert not db.execute("SELECT 1 FROM graph_node WHERE catalog_source = 'propp' "
                          "AND kind = 'column' AND (is_grain OR is_as_of)").fetchall()

    block = _resolved_block(db, "propp")
    assert (block["grain_columns"], block["grain_status"]) == (["cust_id"], "ai_proposed")
    assert (block["as_of_column"], block["as_of_status"]) == ("booked_at", "ai_proposed")


def test_a_table_with_no_grain_and_no_proposal_still_carries_neither(db):
    """ABSENT, end to end. The fourth state, and the regression this task could most easily cause:
    a resolver that invents an empty proposal would put a status on every table in the catalog."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay.upload.ingest import ingest_upload

    _sealed()
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    rows = [CanonicalRow("barep", "facility_limits", "cust_id", "text"),
            CanonicalRow("barep", "facility_limits", "limit_amt", "numeric")]
    assert ingest_upload(db, "barep", rows, actor=owner,
                         now=datetime(2026, 8, 1, tzinfo=UTC)).status == "ingested"

    block = _resolved_block(db, "barep")
    assert not {"grain_columns", "grain_status", "as_of_column", "as_of_status"} & set(block)


def test_a_HUMAN_proposed_draft_is_never_called_ai_proposed(db):
    """THE CONFIGURATION NOBODY CONSIDERED. `ai_proposed` claims a MODEL inferred the value. Every
    grain DRAFT in this codebase today comes from Pass B under the service actor, and
    `table_fact_governance` already hard-codes that assumption (`origin = llm_proposed_not_profiled`)
    — but a proposal is a generic governed command, and a human-actor DRAFT would wear a label that
    is simply false about who wrote it.

    The resolver checks the DRAFT event's own actor. A human-proposed draft is not surfaced at all,
    which is exactly what shipped before this task — no regression, and no lie."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay import facts
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.store import append_overlay_event
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.upload_catalog import table_ref

    _sealed()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    human = mint_test_identity(subject="user:steward", role_claims=("data_owner",))
    assert human.actor_kind == "human", "this test needs a HUMAN proposer to mean anything"
    rows = [CanonicalRow("humdraft", "facility_limits", "cust_id", "text"),
            CanonicalRow("humdraft", "facility_limits", "limit_amt", "numeric")]
    assert ingest_upload(db, "humdraft", rows, actor=owner, now=now).status == "ingested"

    fk = fact_key(table_ref("humdraft", "facility_limits"), "grain")
    append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED, actor=human, expected_version=0,
        payload={"catalog_object_ref": {"catalog_source": "humdraft", "object_kind": "table",
                                        "schema": "public", "table": "facility_limits"},
                 "object_ref": "public.facility_limits", "fact_type": "grain",
                 "proposed_value": {"columns": ["cust_id"], "is_unique": True},
                 "proposal_fingerprint": "fp", "proposed_by": human.subject})

    block = _resolved_block(db, "humdraft")
    assert "grain_status" not in block, "a HUMAN's draft was mislabelled as the AI's proposal"
    assert "grain_columns" not in block


def test_a_rejected_ai_proposal_does_not_travel(db):
    """The other lifecycle state a naive `read the proposal value` would surface. A REJECTED grain
    is a value a human looked at and REFUSED; putting it in the prompt labelled `ai_proposed` would
    re-float an answer governance already killed. Only a folded DRAFT is an open proposal —
    the same test `table_fact_governance._build_view` applies to its own queue."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity
    from tests.featuregen.overlay.upload.conftest import _reject_grain

    from featuregen.overlay.upload.ingest import ingest_upload

    _sealed()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
    rows = [CanonicalRow("rejp", "facility_limits", "cust_id", "text"),
            CanonicalRow("rejp", "facility_limits", "limit_amt", "numeric")]
    assert ingest_upload(db, "rejp", rows, actor=owner, now=now).status == "ingested"

    _propose_ai_grain(db, "rejp", "facility_limits", ["cust_id"])
    _reject_grain(db, "rejp", "facility_limits", actor=admin)

    block = _resolved_block(db, "rejp")
    assert "grain_status" not in block, "a REJECTED proposal was re-floated to the model"


def test_the_authority_read_is_ONE_query_however_many_tables(db):
    """THE HOT-PATH GUARD. `_table_context` runs once per optional column as the budget loop
    searches for a fit, so the read that feeds it is hoisted to ONE call per assembly — and that
    call must not itself be an N+1 over tables. Driven at 4 tables so a per-table read shows up as
    4-plus statements rather than hiding inside a single-table fixture."""
    from datetime import UTC, datetime

    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay.upload.ingest import ingest_upload

    _sealed()
    owner = mint_test_identity(subject="user:owner", role_claims=("data_owner",))
    rows = [CanonicalRow("manyp", f"t{i}", "cust_id", "text", is_grain=True) for i in range(4)]
    assert ingest_upload(db, "manyp", rows, actor=owner,
                         now=datetime(2026, 8, 1, tzinfo=UTC)).status == "ingested"

    cols = fa._candidate_columns(db, "manyp", roles=())
    assert len({c["table"] for c in cols}) == 4
    executed: list[str] = []
    real_execute = type(db).execute

    def _counting(self, sql, *a, **kw):
        executed.append(str(sql))
        return real_execute(self, sql, *a, **kw)

    try:
        type(db).execute = _counting
        authority = fa._table_fact_authority(db, cols)
    finally:
        type(db).execute = real_execute
    assert set(authority) == {("manyp", f"t{i}") for i in range(4)}
    assert len(executed) == 1, f"the authority read fanned out per table: {executed}"
