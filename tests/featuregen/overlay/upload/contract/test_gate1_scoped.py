"""Phase-1B Task 4 — scoped grounding wired into ``build_considered_set``.

When ``FEATUREGEN_INTENT_SCOPED_APPLICABILITY=1`` and a *narrowing* ``ApplicabilityResult`` is supplied
for a hypothesis-mode intent, ``_template_candidates`` grounds only the applicability's eligible recipe
subset (not the whole ``ALL_TEMPLATES`` registry). Everything else — the LLM alternatives, the anchor,
the persisted snapshot — is unchanged, and the builder NEVER persists the scope (Task 7 owns that).

Behaviour-neutral by default: flag OFF (or ``applicability=None``) grounds ``ALL_TEMPLATES`` exactly as
today (byte-identical). Definition-mode bypasses scoping entirely. The narrowing is genuine only when
the eligible set is strictly smaller than the full registry — an unscoped/all-eligible result grounds
everything (fail-open asymmetry). See
``docs/superpowers/plans/2026-07-10-phase1b-scoped-grounding.md`` Task 4.
"""
from datetime import UTC, datetime

import featuregen.overlay.upload.contract.gate1 as gate1
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import build_considered_set
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope, applicability_result
from featuregen.overlay.upload.templates import ALL_TEMPLATES

NOW = datetime(2026, 7, 10, tzinfo=UTC)
FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"

CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
ALL_IDS = frozenset(t.id for t in ALL_TEMPLATES)
# A credit recipe and a fraud recipe — both out of scope for a churn narrowing.
CREDIT_RECIPE = "credit_utilisation"
FRAUD_RECIPE = "txn_velocity_spike"


def _bank_churn(db):
    # A churn-shaped catalog carrying the concept-tagged columns the retail_churn templates ground on
    # (mirrors tests/.../contract/test_gate1.py::_bank_churn), so there is a non-trivial grounded
    # "templates" lens and the LLM `avg_balance_90d` also grounds — the two-source model end to end.
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


def _client() -> FakeLLM:
    """The generation tasks build_considered_set drives (no recognizer entry — recognition is now an
    explicit API step, not in-flow)."""
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def _capture_grounded_ids(monkeypatch) -> dict:
    """Spy on ``gate1.ground_all`` to capture the exact template-id set grounding was handed — the
    direct proof that scoping narrows (or does not narrow) the ground universe, independent of what the
    churn catalog happens to route."""
    captured: dict = {}
    real = gate1.ground_all

    def spy(conn, templates, **kwargs):
        tlist = list(templates)
        captured["ids"] = {t.id for t in tlist}
        return real(conn, tlist, **kwargs)

    monkeypatch.setattr(gate1, "ground_all", spy)
    return captured


def _templates_lens(cs) -> set[str]:
    return {f.name for s in cs.alternatives if s.lens == "templates" for f in s.features}


def _shape(cs):
    """The considered-set alternatives as a comparable shape: each lens + its ordered feature names."""
    return sorted((s.lens, tuple(f.name for f in s.features)) for s in cs.alternatives)


def _build(db, client, intent, **kwargs):
    return build_considered_set(db, intent, client, catalog_source="bank",
                                target_ref="public.accounts.churned", now=NOW, **kwargs)


def _scope_row_count(db) -> int:
    return db.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0]


# ── flag OFF / applicability=None → byte-identical to today ──────────────────────────────────────────
def test_flag_off_applicability_none_is_byte_neutral(db, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    _bank_churn(db)

    plain = _build(db, _client(), submit_intent(hypothesis=HYPOTHESIS, actor="ds1"))
    with_none = _build(db, _client(), submit_intent(hypothesis=HYPOTHESIS, actor="ds1"),
                       applicability=None)

    assert _shape(with_none) == _shape(plain)          # every lens + feature name identical
    assert _templates_lens(with_none) == _templates_lens(plain)
    # a real templates lens grounded (so the comparison is non-trivial) and the LLM proposal survived.
    assert any(lens == "templates" and names for lens, names in _shape(plain))
    assert "avg_balance_90d" in {n for _lens, names in _shape(plain) for n in names}
    assert with_none.applicability is None             # nothing carried when none supplied


def test_flag_off_ignores_a_narrowing_applicability(db, monkeypatch):
    # Even with a narrowing applicability supplied, the flag being OFF grounds the full registry.
    monkeypatch.delenv(FLAG, raising=False)
    _bank_churn(db)
    captured = _capture_grounded_ids(monkeypatch)
    result = applicability_result(ConfirmedScope(primary=CHURN))

    cs = _build(db, _client(), submit_intent(hypothesis=HYPOTHESIS, actor="ds1"),
                applicability=result)

    assert captured["ids"] == set(ALL_IDS)             # full grounding — flag gates the narrowing
    assert cs.applicability is result                  # still carried through for Task 5


# ── flag ON + a churn narrowing → ground only the eligible subset ─────────────────────────────────────
def test_flag_on_churn_narrowing_grounds_only_eligible(db, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    _bank_churn(db)
    captured = _capture_grounded_ids(monkeypatch)
    result = applicability_result(ConfirmedScope(primary=CHURN))
    assert len(result.eligible_ids) < len(ALL_IDS)     # precondition: a genuine narrowing

    cs = _build(db, _client(), submit_intent(hypothesis=HYPOTHESIS, actor="ds1"),
                applicability=result)

    # Grounding was handed EXACTLY the eligible (churn) subset — not the whole registry.
    assert captured["ids"] == set(result.eligible_ids)
    assert "balance_trend" in captured["ids"]          # a churn recipe is eligible
    assert CREDIT_RECIPE not in captured["ids"]         # credit recipe excluded from the ground set
    assert FRAUD_RECIPE not in captured["ids"]          # fraud recipe excluded from the ground set
    # The surfaced templates lens contains only churn recipes; no credit/fraud recipe id appears.
    names = _templates_lens(cs)
    assert "balance_trend_90d" in names
    assert not any(n.startswith((CREDIT_RECIPE, FRAUD_RECIPE)) for n in names)
    assert cs.applicability is result                  # the SAME object carried to the disposition stage


# ── flag ON + an unscoped (all-eligible) result → NOT a narrowing → full grounding ───────────────────
def test_flag_on_unscoped_result_grounds_all(db, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    _bank_churn(db)
    captured = _capture_grounded_ids(monkeypatch)
    result = applicability_result(ConfirmedScope(primary=None, unscoped=True))
    assert result.eligible_ids == ALL_IDS              # unscoped → all recipes eligible (not a narrowing)

    cs = _build(db, _client(), submit_intent(hypothesis=HYPOTHESIS, actor="ds1"),
                applicability=result)

    assert captured["ids"] == set(ALL_IDS)             # fail-open asymmetry: everything grounds
    assert cs.applicability is result


# ── definition-mode bypasses scoping entirely ─────────────────────────────────────────────────────────
def test_definition_mode_bypasses_scoping(db, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    _bank_churn(db)
    captured = _capture_grounded_ids(monkeypatch)
    result = applicability_result(ConfirmedScope(primary=CHURN))
    assert len(result.eligible_ids) < len(ALL_IDS)     # a narrowing that must NOT apply in definition mode

    intent = submit_intent(hypothesis=HYPOTHESIS,
                           definition="90-day average balance per customer", actor="ds1")
    assert intent.intake_mode == "definition"
    cs = _build(db, _client(), intent, applicability=result)

    assert captured["ids"] == set(ALL_IDS)             # bypass — grounding unchanged vs ALL_TEMPLATES
    assert cs.anchor is not None and cs.anchor.name == "avg_balance_90d"   # anchor still produced
    assert cs.applicability is result                  # carried through regardless of the bypass


# ── the builder persists NO scope row ────────────────────────────────────────────────────────────────
def test_build_considered_set_writes_no_scope_row(db, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    _bank_churn(db)
    assert _scope_row_count(db) == 0
    result = applicability_result(ConfirmedScope(primary=CHURN))

    _build(db, _client(), submit_intent(hypothesis=HYPOTHESIS, actor="ds1"), applicability=result)

    # Scope persistence is the API layer's job (Task 7) — the computation-only builder writes nothing.
    assert _scope_row_count(db) == 0
