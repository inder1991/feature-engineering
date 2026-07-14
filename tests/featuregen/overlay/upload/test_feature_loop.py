"""The validated generate-validate-refine loop for recommend_features."""
from datetime import UTC, datetime, timedelta

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import recommend_features
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=UTC)


def _bank(db):
    rows = [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True),  # point-in-time basis
        CanonicalRow("bank", "accounts", "churned", "boolean"),   # the target label
    ]
    build_graph(db, "bank", rows)


def _fresh_watermark(db, source, now):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES (%s, %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, now, now))


def test_loop_rejects_leaky_and_unsafe_keeps_good(db):
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "leaky", "derives_from": ["public.accounts.churned"]},                 # leaks target
        {"name": "unsafe", "derives_from": ["public.accounts.balance"],
         "aggregation": "sum_all_time"},                                                # unsafe SUM
        {"name": "good", "derives_from": ["public.accounts.balance"],
         "aggregation": "avg_90d"},                                                     # fine
    ]})})
    out = recommend_features(db, "predict churn", client, catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW)
    names = {f.name for f in out}
    assert names == {"good"}                    # leaky + unsafe were rejected


class _SeqLLM:
    """Returns responses in CALL order regardless of inputs (FakeLLM keys its counter on the input
    hash, which the loop deliberately changes each round via `avoid`)."""
    def __init__(self, responses):
        self._responses, self._i = responses, 0

    def call(self, request):
        from featuregen.intake.llm import LLMResult
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return LLMResult(output=r, self_reported_scores={}, call_ref="", status="ok")


def test_loop_refines_across_rounds(db):
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    # round 1: only a leaky idea; round 2: a good one -> the loop must continue and find it.
    client = _SeqLLM([
        {"features": [{"name": "leaky", "derives_from": ["public.accounts.churned"]}]},
        {"features": [{"name": "good", "derives_from": ["public.accounts.balance"],
                       "aggregation": "avg_90d"}]},
    ])
    out = recommend_features(db, "predict churn", client, catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW, target=1, budget=3)
    assert [f.name for f in out] == ["good"]


def test_loop_rejects_stale_source(db):
    _bank(db)
    # watermark is 3 days old -> beyond the 24h freshness window.
    _fresh_watermark(db, "bank", NOW - timedelta(days=3))
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "good", "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d"}]})})
    out = recommend_features(db, "predict churn", client, catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW)
    assert out == []                            # the only candidate's source is stale


def test_cross_domain_gather_spans_catalogs(db):
    """With an entity anchor, the loop gathers candidates from EVERY catalog holding that entity."""
    build_graph(db, "deposits", [
        CanonicalRow("deposits", "accounts", "cust_ref", "integer", entity="Customer"),
        CanonicalRow("deposits", "accounts", "balance", "numeric"),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True)])
    build_graph(db, "cards", [
        CanonicalRow("cards", "card_accounts", "cust_id", "integer", entity="Customer"),
        CanonicalRow("cards", "card_accounts", "spend", "numeric"),
        CanonicalRow("cards", "card_accounts", "txn_date", "timestamp", as_of=True)])
    _fresh_watermark(db, "deposits", NOW)
    _fresh_watermark(db, "cards", NOW)

    captured = {}

    class _Capture:
        def call(self, request):
            captured["refs"] = {c["object_ref"]
                                for c in request.inputs["catalog_metadata"]["columns"]}
            from featuregen.intake.llm import LLMResult
            # propose a CROSS-DOMAIN feature: balance (deposits) + spend (cards)
            return LLMResult(output={"features": [{"name": "cross", "aggregation": "avg_90d",
                "derives_from": ["public.accounts.balance", "public.card_accounts.spend"]}]},
                self_reported_scores={}, call_ref="", status="ok")

    out = recommend_features(db, "predict churn", _Capture(), entity="Customer", now=NOW, target=1)
    # the menu spanned both catalogs...
    assert "public.accounts.balance" in captured["refs"]
    assert "public.card_accounts.spend" in captured["refs"]
    # ...and a cross-domain feature was accepted (both sources fresh, no leak/unsafe).
    assert [f.name for f in out] == ["cross"]


def test_multi_set_and_advisory_recommendation(db):
    from featuregen.overlay.upload.feature_assist import recommend_feature_sets, recommend_set
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    client = FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary",
            "reasoning": "monetary best matches the balance-drop hypothesis"}),
    })
    sets = recommend_feature_sets(db, "predict churn", client, catalog_source="bank",
                                  target_ref="public.accounts.churned", now=NOW,
                                  lenses=("behavioral", "monetary"), per_set=1)
    assert {s.lens for s in sets} == {"behavioral", "monetary"}
    assert all(len(s.features) == 1 for s in sets)          # each set is validated + non-empty

    rec = recommend_set(db, sets, "customers churn when balance drops", client)
    assert rec.recommended_lens == "monetary"               # advisory pick
    assert rec.reasoning                                    # explained
    assert "backtest" in rec.caveat                         # and honestly caveated


def _bank_with_asof(db, as_of=True):
    rows = [CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
            CanonicalRow("t", "accounts", "balance", "numeric")]
    if as_of:
        rows.append(CanonicalRow("t", "accounts", "posted_at", "timestamp", as_of=True))
    build_graph(db, "t", rows)
    _fresh_watermark(db, "t", NOW)


def test_windowed_feature_needs_point_in_time_basis(db):
    # No as-of column on the table -> a windowed feature can't be point-in-time -> rejected.
    _bank_with_asof(db, as_of=False)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
         "aggregation": "avg_90d"}]})})
    assert recommend_features(db, "churn", client, catalog_source="t", now=NOW) == []


def test_windowed_feature_ok_with_as_of(db):
    _bank_with_asof(db, as_of=True)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
         "aggregation": "avg_90d"}]})})
    out = recommend_features(db, "churn", client, catalog_source="t", now=NOW)
    assert [f.name for f in out] == ["avg_balance_90d"]


def test_non_windowed_feature_needs_no_as_of(db):
    _bank_with_asof(db, as_of=False)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "current_balance", "derives_from": ["public.accounts.balance"],
         "aggregation": "latest"}]})})       # not windowed -> no point-in-time requirement
    out = recommend_features(db, "churn", client, catalog_source="t", now=NOW)
    assert [f.name for f in out] == ["current_balance"]


def test_loop_rejects_ambiguous_cross_catalog_column(db):
    # B3: the same object_ref in two entity-linked catalogs can't be resolved to one catalog -> rejected
    build_graph(db, "c1", [
        CanonicalRow("c1", "accounts", "cust", "integer", entity="Customer"),
        CanonicalRow("c1", "accounts", "val", "numeric"),
        CanonicalRow("c1", "accounts", "posted_at", "timestamp", as_of=True)])
    build_graph(db, "c2", [
        CanonicalRow("c2", "accounts", "cust", "integer", entity="Customer"),
        CanonicalRow("c2", "accounts", "val", "numeric"),
        CanonicalRow("c2", "accounts", "posted_at", "timestamp", as_of=True)])
    _fresh_watermark(db, "c1", NOW)
    _fresh_watermark(db, "c2", NOW)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "ambig", "derives_from": ["public.accounts.val"], "aggregation": "avg_90d"}]})})
    out = recommend_features(db, "x", client, entity="Customer", now=NOW)
    assert out == []   # public.accounts.val is in c1 AND c2 -> ambiguous -> rejected


def test_gauntlet_catches_windowed_names_without_digit_unit(db):
    # M1: "avg_last_12_months" / "ytd" evade the old regex but ARE windowed -> need an as-of column
    _bank_with_asof(db, as_of=False)   # table 't' has NO as-of column
    for agg in ("avg_last_12_months", "ytd_total", "cumulative_balance", "monthly_avg"):
        client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": f"f_{agg}", "derives_from": ["public.accounts.balance"], "aggregation": agg}]})})
        assert recommend_features(db, "x", client, catalog_source="t", now=NOW) == [], agg


def test_gauntlet_catches_additive_unsafe_names_without_sum(db):
    # M2: "total_balance"/"running_total" sum a semi-additive balance but contain no "sum" substring
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "balance", "numeric", additivity="semi_additive"),
        CanonicalRow("t", "accounts", "posted_at", "timestamp", as_of=True)])
    _fresh_watermark(db, "t", NOW)
    for agg in ("total_balance", "running_total", "cumulative"):
        client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": f"f_{agg}", "derives_from": ["public.accounts.balance"], "aggregation": agg}]})})
        assert recommend_features(db, "x", client, catalog_source="t", now=NOW) == [], agg


def test_feature_assist_egress_guard_blocks_pii_objective(db):
    # M6: raw user text is scanned before dispatch — a PII objective is blocked, not leaked to the LLM
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "good", "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d"}]})})
    out = recommend_features(db, "predict churn for joe@bank.com", client,
                             catalog_source="bank", now=NOW)
    assert out == []   # egress guard raised on the email -> no dispatch -> no features


def test_gauntlet_rejects_mixed_units(db):
    # a feature combining dollars + cents is silently wrong -> rejected
    build_graph(db, "t", [
        CanonicalRow("t", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("t", "accounts", "balance", "numeric", unit="dollars"),
        CanonicalRow("t", "accounts", "fee", "numeric", unit="cents"),
        CanonicalRow("t", "accounts", "posted_at", "timestamp", as_of=True)])
    _fresh_watermark(db, "t", NOW)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "mixed", "derives_from": ["public.accounts.balance", "public.accounts.fee"],
         "aggregation": "avg_90d"}]})})
    assert recommend_features(db, "x", client, catalog_source="t", now=NOW) == []


class _LoopLLM:
    """Dispatches by task so a test can drive the LLM-1 (generator) <-> LLM-2 (critic) refine loop:
    generate calls consume `gens` in order, critique calls consume `crits` in order (last repeats)."""
    def __init__(self, gens, crits):
        self._gens, self._crits, self._gi, self._ci = list(gens), list(crits), 0, 0

    def call(self, request):
        from featuregen.intake.llm import LLMResult
        if request.task == "overlay.feature.recommend":
            out = self._gens[min(self._gi, len(self._gens) - 1)]
            self._gi += 1
        elif request.task == "overlay.feature.critique_candidates":
            out = self._crits[min(self._ci, len(self._crits) - 1)]
            self._ci += 1
        else:
            out = {}
        return LLMResult(output=out, self_reported_scores={}, call_ref="", status="ok")


def test_critic_flags_then_generator_fixes(db):
    # LLM-2 critic flags round 1's candidate -> fed back -> LLM-1 fixes it round 2 -> accepted.
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    gens = [
        {"features": [{"name": "weak", "derives_from": ["public.accounts.balance"],
                       "aggregation": "avg_90d"}]},
        {"features": [{"name": "fixed", "derives_from": ["public.accounts.balance"],
                       "aggregation": "avg_30d"}]},
    ]
    crits = [{"issues": [{"name": "weak", "issue": "weak fit to the churn hypothesis"}]},
             {"issues": []}]
    out = recommend_features(db, "predict churn", _LoopLLM(gens, crits), catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW, target=1, budget=3)
    assert [f.name for f in out] == ["fixed"]   # weak held back by critic; fixed accepted


def test_critic_capped_at_three_reviews_then_forwards_to_human(db):
    # The critic loop is BOUNDED: at most 3 reviews. If never satisfied, LLM-1's output goes FORWARD
    # to the human carrying the residual critic note (advisory) — nothing is dropped for a note alone.
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    calls = {"gen": 0, "crit": 0}

    class _AlwaysFlag:
        def call(self, request):
            from featuregen.intake.llm import LLMResult
            if request.task == "overlay.feature.recommend":
                calls["gen"] += 1
                out = {"features": [{"name": "cand", "derives_from": ["public.accounts.balance"],
                                     "aggregation": "avg_90d"}]}
            elif request.task == "overlay.feature.critique_candidates":
                calls["crit"] += 1
                cands = request.inputs["catalog_metadata"]["candidates"]
                out = {"issues": [{"name": c["name"], "issue": "still weak"} for c in cands]}
            else:
                out = {}
            return LLMResult(output=out, self_reported_scores={}, call_ref="", status="ok")

    out = recommend_features(db, "predict churn", _AlwaysFlag(), catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW, target=1, budget=5)
    assert calls["crit"] == 3                       # capped at 3 reviews, not budget=5
    assert [f.name for f in out] == ["cand"]        # forwarded to the human, not dropped
    assert out[0].critic_note == "still weak"       # carries the residual advisory note


def test_gauntlet_returns_structured_rejection_codes(db):
    from featuregen.overlay.upload.feature_assist import RejectCode, _validate_idea
    _bank(db)
    known = {"public.accounts.churned", "public.accounts.balance"}
    src_of = {"public.accounts.churned": {"bank"}, "public.accounts.balance": {"bank"}}
    _, rej = _validate_idea(db, {"name": "l", "derives_from": ["public.accounts.churned"]},
                            known, src_of, "public.accounts.churned", None, timedelta(hours=24))
    assert rej.code == RejectCode.LEAKAGE
    _, rej = _validate_idea(db, {"name": "u", "derives_from": ["nope"]},
                            known, src_of, None, None, timedelta(hours=24))
    assert rej.code == RejectCode.UNGROUNDED


def test_redundant_of_detects_same_derives_and_agg():
    from featuregen.overlay.upload.feature_assist import FeatureIdea, _redundant_of
    a = FeatureIdea("a", "", ["x"], "avg_90d", "t", derives_pairs=(("bank", "x"),))
    b = FeatureIdea("b", "", ["x"], "avg_90d", "t", derives_pairs=(("bank", "x"),))   # dup, new name
    c = FeatureIdea("c", "", ["y"], "avg_90d", "t", derives_pairs=(("bank", "y"),))
    assert _redundant_of(b, [a]) is True
    assert _redundant_of(c, [a]) is False


def test_registry_dedup_skips_already_registered(db):
    from featuregen.overlay.upload.features import FeatureSpec, register_feature
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    register_feature(db, FeatureSpec(name="existing", aggregation="avg_90d",
                                     derives_from=(("bank", "public.accounts.balance"),)))
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "dup", "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d"}]})})
    out = recommend_features(db, "x", client, catalog_source="bank", now=NOW, critic=False)
    assert out == []   # duplicates a registered feature -> skipped


def test_accepted_features_are_design_checked(db):
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "g", "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d"}]})})
    out = recommend_features(db, "x", client, catalog_source="bank", now=NOW, critic=False)
    assert out and out[0].verification == "DESIGN-CHECKED"


def test_set_signals_counts_columns_and_size(db):
    from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet, set_signals
    _bank(db)
    fs = FeatureSet("monetary", [
        FeatureIdea("a", "", ["public.accounts.balance"], "avg_90d", "accounts",
                    derives_pairs=(("bank", "public.accounts.balance"),)),
        FeatureIdea("b", "", ["public.accounts.balance"], "avg_30d", "accounts",
                    derives_pairs=(("bank", "public.accounts.balance"),))])
    sig = set_signals(db, fs)
    assert sig["size"] == 2 and sig["distinct_columns"] == 1   # both read the same column


def test_router_flat_catalog_gates_out_temporal_and_aggregation(db):
    from featuregen.overlay.upload.feature_assist import _candidate_columns, route_strategies
    build_graph(db, "flat", [
        CanonicalRow("flat", "t", "a", "numeric"),
        CanonicalRow("flat", "t", "b", "numeric")])   # 2 numeric; no as-of / join / entity
    names = [n for n, _ in route_strategies(db, _candidate_columns(db, "flat", ()))]
    assert "unary" in names and "ratio" in names
    assert "temporal" not in names          # no as-of column -> don't propose windowed features
    assert "aggregation" not in names       # no join edge
    assert "distributional" not in names    # no entity


def test_router_includes_temporal_and_distributional_when_present(db):
    from featuregen.overlay.upload.feature_assist import _candidate_columns, route_strategies
    build_graph(db, "rich", [
        CanonicalRow("rich", "acct", "cust", "integer", entity="Customer"),
        CanonicalRow("rich", "acct", "bal", "numeric"),
        CanonicalRow("rich", "acct", "posted_at", "timestamp", as_of=True)])
    names = [n for n, _ in route_strategies(db, _candidate_columns(db, "rich", ()))]
    assert "temporal" in names          # as-of present
    assert "distributional" in names    # entity present


def test_router_includes_aggregation_with_a_join_key(db):
    from featuregen.overlay.upload.feature_assist import _candidate_columns, route_strategies
    build_graph(db, "j", [
        CanonicalRow("j", "orders", "id", "integer", is_grain=True),
        CanonicalRow("j", "orders", "acct_id", "integer", joins_to="accounts.id"),
        CanonicalRow("j", "accounts", "id", "integer", is_grain=True)])
    names = [n for n, _ in route_strategies(db, _candidate_columns(db, "j", ()))]
    assert "aggregation" in names       # a join key exists -> aggregation applies


def test_router_includes_aggregation_when_only_the_parent_side_is_candidate(db):
    # M-6: graph_edge.to_ref is COLUMN-level (public.accounts.id — both declared edges and Pass-C
    # projected edges store 3-segment to_refs), so the parent (to) side must be matched against the
    # candidate COLUMN refs. Comparing it against 2-segment public.<table> refs never matched,
    # silently dropping the "aggregate children up" family for entity-grain candidate sets.
    from featuregen.overlay.upload.feature_assist import route_strategies
    build_graph(db, "par", [
        CanonicalRow("par", "orders", "id", "integer", is_grain=True),
        CanonicalRow("par", "orders", "acct_id", "integer", joins_to="accounts.id"),
        CanonicalRow("par", "accounts", "id", "integer", is_grain=True)])
    # Candidates are ONLY the parent pk — the child fk (the edge's from_ref) is NOT in the set.
    cols = [{"object_ref": "public.accounts.id", "catalog_source": "par"}]
    names = [n for n, _ in route_strategies(db, cols)]
    assert "aggregation" in names   # children join INTO the candidate column -> aggregate them up


def test_feature_rationale_flows_from_generator_to_gate1(db):
    # §14.2 — a per-feature causal rationale rides the candidate through to Gate #1.
    _bank(db)
    _fresh_watermark(db, "bank", NOW)
    client = FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "avg_bal", "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d",
         "rationale": "90-day average balance operationalizes the balance-level hypothesis"}]})})
    out = recommend_features(db, "predict churn", client, catalog_source="bank",
                             target_ref="public.accounts.churned", now=NOW, critic=False)
    assert out and out[0].rationale.startswith("90-day average balance")
