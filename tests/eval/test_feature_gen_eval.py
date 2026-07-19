"""Slice 3A-iv Task 6 — key-gated real-provider feature-gen quality gate (spec §9).

Runs each gold objective twice against the PINNED provider: baseline (thin menu, flag off) vs enriched
(widened menu + tri-state, flag on). Computes the §9 metrics, writes a versioned report under
tests/eval/reports/, then asserts the delivery bars. A key-gated test that merely SKIPS is not itself
the gate (that is the hermetic core in test_feature_eval.py + the byte-identity units); THIS run is the
manual, versioned evidence.

Run it (needs a live key; skips cleanly without one):

    FEATUREGEN_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... \
        .venv/bin/python -m pytest -m eval tests/eval/test_feature_gen_eval.py -q -s
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.eval.feature_eval import (
    GenFeature,
    cost_regression,
    relevance_lift,
    relevance_rate,
    restricted_leaks,
    token_total,
    unsafe_accepted,
)
from tests.eval.gold_features import GOLD

from featuregen.intake.llm import DEFAULT_LLM_MODEL
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import recommend_features_report
from featuregen.overlay.upload.graph import build_graph

pytestmark = pytest.mark.eval

# A seeded sample/PII sentinel embedded in a column definition — it must NEVER reach the provider
# (the nested field-aware egress adapter from 3A-iii sanitizes definition-kind fields before dispatch).
_SENTINEL = "SAMPLE:jane.doe@acme-bank.example"
_SENTINELS = frozenset({_SENTINEL})

# Delivery bars (spec §9).
_MIN_RELEVANCE_LIFT = 0.15      # >= 15% relative
_MAX_COST_REGRESSION = 0.25     # <= 25%

_BANK_ROWS = [
    CanonicalRow("bank", "transactions", "acct_id", "integer",
                 joins_to="accounts.account_id", cardinality="N:1"),
    CanonicalRow("bank", "transactions", "txn_id", "integer", is_grain=True, entity="Transaction"),
    CanonicalRow("bank", "transactions", "amount", "numeric",
                 definition=f"signed transaction amount (e.g. {_SENTINEL})",
                 additivity="additive", unit="dollars", currency="USD", entity="Transaction"),
    CanonicalRow("bank", "transactions", "merchant_id", "integer", entity="Merchant"),
    CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
    CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True, entity="Account"),
    CanonicalRow("bank", "accounts", "balance", "numeric", definition="end-of-day ledger balance",
                 additivity="semi_additive", unit="dollars", currency="USD", entity="Account"),
    CanonicalRow("bank", "accounts", "cust_id", "integer",
                 joins_to="customers.cust_id", cardinality="N:1", entity="Customer"),
    CanonicalRow("bank", "customers", "cust_id", "integer", is_grain=True, entity="Customer"),
    CanonicalRow("bank", "loans", "loan_id", "integer", is_grain=True, entity="Loan"),
    CanonicalRow("bank", "loans", "principal", "numeric", definition="loan principal outstanding",
                 additivity="additive", unit="dollars", currency="USD", entity="Loan"),
]


def _gens(report) -> list[GenFeature]:
    return [GenFeature(name=i.name, derives_from=tuple(i.derives_from),
                       operation_kind=i.operation_kind, validation_status=i.validation_status,
                       requirement_count=len(i.requirements))
            for i in report.ideas]


def _egress_payloads(db) -> list[str]:
    rows = db.execute("SELECT redacted_input FROM llm_call "
                      "WHERE task LIKE 'overlay.feature.%'").fetchall()
    return [json.dumps(r[0]) for r in rows if r[0] is not None]


def _run(db, client, objective, entity, *, feature_context: bool):
    if feature_context:
        os.environ["FEATUREGEN_FEATURE_CONTEXT"] = "1"
    else:
        os.environ.pop("FEATUREGEN_FEATURE_CONTEXT", None)
    return recommend_features_report(db, objective, client, catalog_source="bank", entity=entity,
                                     roles=("platform_admin",), now=datetime.now(UTC), critic=False)


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                    reason="live provider eval; set ANTHROPIC_API_KEY to run")
def test_feature_gen_baseline_vs_enriched(db):
    from featuregen.intake.llm_claude import ClaudeConfig, build_claude_llm
    os.environ.setdefault("FEATUREGEN_LLM_PROVIDER", "anthropic")
    model = os.environ.get("FEATUREGEN_LLM_MODEL", DEFAULT_LLM_MODEL)
    client = build_claude_llm(ClaudeConfig(enabled=True, model=model))

    build_graph(db, "bank", _BANK_ROWS)

    per_case: list[dict] = []
    base_rates: list[float] = []
    enr_rates: list[float] = []
    base_tokens = 0
    enr_tokens = 0
    unsafe: list[str] = []
    leaks: set[str] = set()

    for g in GOLD:
        if g.catalog_source != "bank":
            continue   # this fixture is the 'bank' catalog; skip cases anchored elsewhere
        before = {r[0] for r in db.execute("SELECT llm_call_ref FROM llm_call").fetchall()}
        base = _run(db, client, g.objective, g.entity, feature_context=False)
        mid = {r[0] for r in db.execute("SELECT llm_call_ref FROM llm_call").fetchall()}
        enr = _run(db, client, g.objective, g.entity, feature_context=True)
        after = {r[0] for r in db.execute("SELECT llm_call_ref FROM llm_call").fetchall()}

        base_gens, enr_gens = _gens(base), _gens(enr)
        br = relevance_rate(base_gens, g.expected_columns, g.relevance_terms)
        er = relevance_rate(enr_gens, g.expected_columns, g.relevance_terms)
        base_rates.append(br)
        enr_rates.append(er)

        base_tokens += _tokens_for(db, mid - before)
        enr_tokens += _tokens_for(db, after - mid)

        unsafe += [f"{g.objective}:{f.name}" for f in unsafe_accepted(enr_gens)]
        leaks |= set(restricted_leaks(_egress_payloads(db), _SENTINELS))

        per_case.append({"objective": g.objective, "baseline_relevance": round(br, 3),
                         "enriched_relevance": round(er, 3),
                         "baseline_features": len(base_gens), "enriched_features": len(enr_gens)})

    n = len(base_rates)
    assert n > 0, "no 'bank' gold cases were exercised"
    mean_base = sum(base_rates) / n
    mean_enr = sum(enr_rates) / n
    lift = relevance_lift(mean_base, mean_enr)
    cost_reg = cost_regression(base_tokens, enr_tokens)
    base_accept = sum(c["baseline_features"] for c in per_case)
    enr_accept = sum(c["enriched_features"] for c in per_case)

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "provider": os.environ.get("FEATUREGEN_LLM_PROVIDER"),
        "settings": {"critic": False, "roles": ["platform_admin"]},
        "gold_cases_exercised": n,
        "mean_baseline_relevance": round(mean_base, 4),
        "mean_enriched_relevance": round(mean_enr, 4),
        "relevance_lift": None if lift == float("inf") else round(lift, 4),
        "baseline_accepted": base_accept,
        "enriched_accepted": enr_accept,
        "baseline_tokens": base_tokens,
        "enriched_tokens": enr_tokens,
        "cost_regression": None if cost_reg == float("inf") else round(cost_reg, 4),
        "unsafe_accepted": unsafe,
        "restricted_leaks": sorted(leaks),
        "per_case": per_case,
    }

    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = reports_dir / f"feature_gen_eval_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\n" + json.dumps(report, indent=2))   # visible with -s

    # Delivery bars (spec §9) — assert AFTER writing the report so a failure still leaves diagnostics.
    assert report_path.exists()
    assert unsafe == [], f"unsafe-accepted features (DESIGN_CHECKED with requirements): {unsafe}"
    assert sorted(leaks) == [], f"restricted/sample fields egressed unsanitized: {sorted(leaks)}"
    assert enr_accept >= base_accept, \
        f"grounded-acceptance regressed: enriched {enr_accept} < baseline {base_accept}"
    assert lift >= _MIN_RELEVANCE_LIFT, \
        f"relevance lift {lift:.3f} < required {_MIN_RELEVANCE_LIFT} (see {report_path})"
    assert cost_reg <= _MAX_COST_REGRESSION, \
        f"cost regression {cost_reg:.3f} > allowed {_MAX_COST_REGRESSION} (see {report_path})"


def _tokens_for(db, refs: set[str]) -> int:
    if not refs:
        return 0
    rows = db.execute("SELECT cost_metadata FROM llm_call WHERE llm_call_ref = ANY(%s)",
                      (list(refs),)).fetchall()
    return sum(token_total(r[0]) for r in rows if r[0] is not None)
