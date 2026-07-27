"""MEASUREMENT HARNESS — not a feature, not a regression guard. SKIPPED unless PER_TABLE_MEASURE=1.

Question: `contract/gate1._template_candidates` grounds the ~157-template registry CATALOG-WIDE and
`templates.ground_all_outcomes` yields AT MOST ONE candidate per template, so a template that binds on
`pt_txn_ledger` is "used up" and `pt_card_paymt` / `pt_loan_repay` never see it. This harness measures
what PER-TABLE grounding would add, and how duplicative it would be.

It changes NOTHING in production: per-table grounding is forced by substituting gate-1's own grounding
seam (`gate1._ground_template_outcomes`, already a substitutable seam) with a grounder that hands
`ground_template_outcome` a `columns` list narrowed to ONE table's columns — the optional pre-loaded
`columns` parameter added in 8593a4bb, whose rows carry `table`.

Run:  PER_TABLE_MEASURE=1 pytest tests/featuregen/overlay/upload/test_per_table_grounding_measurement.py -s
Dump: PER_TABLE_MEASURE_OUT=/path/to.json
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_ftr_adapter import _HDR, _row

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.upload import templates as templates_module
from featuregen.overlay.upload.canonical import validate_rows
from featuregen.overlay.upload.contract import gate1 as gate1_module
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE
from featuregen.overlay.upload.suggestions import suggest_features_for_table
from featuregen.overlay.upload.table_fact_governance import (
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES, ground_template_outcome
from featuregen.overlay.upload.upload_catalog import table_ref

pytestmark = pytest.mark.skipif(
    os.environ.get("PER_TABLE_MEASURE") != "1",
    reason="measurement harness — set PER_TABLE_MEASURE=1 to run")

SOURCE = "ptm_multi_table_ftr"            # NON-generic: a generic name collides with the suite's
NOW = datetime(2026, 7, 27, tzinfo=UTC)   # schema-less TECHNICAL sources and MF-6 would hold this.
_FQN_PREFIX = "DPL_EIB_COMPLIANCE."

# A wide-ish bank catalog: THREE transaction-shaped tables that would each satisfy the same recipe
# families, plus one structurally different table. table -> col -> (concept, declared type, term).
_COLUMNS = {
    "ptm_txn_ledger": {
        "CIF_ID": ("customer_id", "varchar", "Customer Identifier"),
        "ACCT_ID": ("account_id", "varchar", "Account Identifier"),
        "TXN_AMT": ("monetary_flow", "decimal", "Transaction Amount"),
        "BAL_AMT": ("monetary_stock", "decimal", "Account Balance"),
        "AS_OF_DT": ("as_of_date", "date", "As Of Date"),
        "TXN_TS": ("event_timestamp", "timestamp", "Transaction Timestamp"),
        "TXN_CNT": ("count", "integer", "Transaction Count"),
        "CUST_HOLD": ("custody_holding", "decimal", "Custody Holding"),
        "SETL_STAT": ("settlement_status", "varchar", "Settlement Status"),
    },
    "ptm_card_paymt": {
        "CARD_CIF": ("customer_id", "varchar", "Card Customer Identifier"),
        "CARD_ACCT": ("account_id", "varchar", "Card Account Identifier"),
        "PAY_AMT": ("monetary_flow", "decimal", "Card Payment Amount"),
        "OUTS_BAL": ("monetary_stock", "decimal", "Outstanding Balance"),
        "VAL_DT": ("as_of_date", "date", "Value Date"),
        "AUTH_TS": ("event_timestamp", "timestamp", "Authorisation Timestamp"),
        "PAY_CNT": ("count", "integer", "Payment Count"),
    },
    "ptm_loan_repay": {
        "LOAN_CIF": ("customer_id", "varchar", "Loan Customer Identifier"),
        "LOAN_ACCT": ("account_id", "varchar", "Loan Account Identifier"),
        "REPAY_AMT": ("monetary_flow", "decimal", "Repayment Amount"),
        "PRIN_BAL": ("monetary_stock", "decimal", "Principal Balance"),
        "DUE_DT": ("as_of_date", "date", "Instalment Due Date"),
        "POST_TS": ("event_timestamp", "timestamp", "Posting Timestamp"),
        "REPAY_CNT": ("count", "integer", "Repayment Count"),
    },
    "ptm_mkt_risk_pos": {
        "BOOK_ID": ("book_id", "varchar", "Trading Book Identifier"),
        "VAR_AMT": ("var", "decimal", "Value At Risk"),
        "RISK_DT": ("as_of_date", "date", "Risk As Of Date"),
    },
}
_GRAIN = {"ptm_txn_ledger": ("cif_id", "as_of_dt"),
          "ptm_card_paymt": ("card_cif", "val_dt"),
          "ptm_loan_repay": ("loan_cif", "due_dt"),
          "ptm_mkt_risk_pos": ("book_id", "risk_dt")}
_CONCEPT_OF = {(t, c): spec[0] for t, cols in _COLUMNS.items() for c, spec in cols.items()}

_SERVICE = IdentityEnvelope(subject="featuregen-overlay-enrichment", actor_kind="service",
                            authenticated=True, auth_method="internal", role_claims=())
_UPLOADER = IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))


def _ftr_csv() -> str:
    return _HDR + "".join(
        _row(source_row=str(n), fqn=f"{_FQN_PREFIX}{table.upper()}.{col}", term_name=term,
             definition=f'"{term}, recorded on {table}."', data_type=declared)
        for n, (table, col, (_concept, declared, term)) in enumerate(
            ((t, c, spec) for t, cols in _COLUMNS.items() for c, spec in cols.items()), start=1))


def _govern_table_facts(conn, table: str, grain_column: str, as_of_column: str) -> None:
    """Grain + availability through the REAL propose -> confirm -> project governance path."""
    admin = mint_test_identity(subject="user:ptm-admin", role_claims=("platform-admin",))
    ref = table_ref(SOURCE, table)
    for fact_type, value in (("grain", {"columns": [grain_column], "is_unique": True}),
                             ("availability_time", {"column": as_of_column, "basis": "posted_at"})):
        res = propose_fact(conn, Command(
            "propose_fact", "overlay_fact", None,
            {"ref": ref, "fact_type": fact_type, "proposed_value": value},
            _SERVICE, proposal_fingerprint(value)))
        assert res.accepted, res.denied_reason
        ctx = load_table_fact_confirmation_context(conn, fact_key(ref, fact_type))
        res = confirm_fact(conn, Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
             "target_event_id": ctx["target_event_id"]},
            admin, f"confirm-{ctx['target_event_id']}"))
        assert res.accepted, res.denied_reason
        assert project_verified_table_fact(conn, SOURCE, ref, fact_type, now=NOW) == "projected"


@pytest.fixture
def wide_catalog(overlay_conn):
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))
    upload = to_glossary_upload(read_ftr_glossary(_ftr_csv(), source=SOURCE))
    good = validate_rows(upload.rows, SOURCE, profile=FTR_GLOSSARY_PROFILE).good
    concepts = {content_hash(r): _CONCEPT_OF[(r.table.lower(), r.column.upper())] for r in good}
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": c} for h, c in concepts.items()]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": t, "domain": "payments"} for t in _COLUMNS]}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
    })
    res = ingest_upload(overlay_conn, SOURCE, upload.rows, actor=_UPLOADER, now=NOW,
                        client=client, glossary=upload)
    assert res.status == "ingested", res.status
    for table, (grain, as_of) in _GRAIN.items():
        _govern_table_facts(overlay_conn, table, grain, as_of)
    return SOURCE


# ── the per-table grounder: the real seam, columns narrowed to ONE table ─────────────────────────
def _per_table_grounder(table: str):
    def _ground(conn, templates, *, catalog_source, roles=(), **kw):
        cols = [c for c in templates_module._load_columns(conn, catalog_source, roles)
                if c.table == table]
        return [ground_template_outcome(conn, t, catalog_source=catalog_source, roles=roles,
                                        columns=cols) for t in templates]
    return _ground


def _cards(payload: dict) -> list[dict]:
    return [s for g in payload["groups"] for s in g["suggestions"]]


def _key(card: dict) -> tuple[str, str]:
    return (card["name"], card["grain_table"])


class _Counters:
    def __init__(self):
        self.loads = 0
        self.statements = 0


@pytest.fixture
def counters(monkeypatch):
    c = _Counters()
    real_load = templates_module._load_columns

    def _spy(conn, catalog_source, roles):
        c.loads += 1
        return real_load(conn, catalog_source, roles)

    monkeypatch.setattr(templates_module, "_load_columns", _spy)
    return c


def _count_statements(conn, counters, monkeypatch):
    """Wrap the connection's own execute so EVERY statement this pass issues is counted."""
    cls = type(conn)
    real = cls.execute

    def _exec(self, *a, **kw):
        counters.statements += 1
        return real(self, *a, **kw)

    monkeypatch.setattr(cls, "execute", _exec)


def test_measure_per_table_grounding(overlay_conn, wide_catalog, counters, monkeypatch, capsys):
    tables = list(_COLUMNS)
    _count_statements(overlay_conn, counters, monkeypatch)

    # ── 1. BASELINE — today's behaviour, catalog-wide grounding, one call per table ──────────────
    counters.loads = counters.statements = 0
    t0 = time.perf_counter()
    baseline = {t: suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=t)
                for t in tables}
    base_secs = time.perf_counter() - t0
    base_loads, base_stmts = counters.loads, counters.statements

    # single-page cost (what one screen view actually costs today)
    counters.loads = counters.statements = 0
    t0 = time.perf_counter()
    suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=tables[0])
    base_one_secs = time.perf_counter() - t0
    base_one = (counters.loads, counters.statements)

    # ── 2. PER-TABLE grounding — the same endpoint, grounding constrained to the table ───────────
    counters.loads = counters.statements = 0
    t0 = time.perf_counter()
    per_table = {}
    for t in tables:
        monkeypatch.setattr(gate1_module, "_ground_template_outcomes", _per_table_grounder(t))
        per_table[t] = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=t)
    pt_secs = time.perf_counter() - t0
    pt_loads, pt_stmts = counters.loads, counters.statements

    counters.loads = counters.statements = 0
    monkeypatch.setattr(gate1_module, "_ground_template_outcomes", _per_table_grounder(tables[0]))
    t0 = time.perf_counter()
    suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=tables[0])
    pt_one_secs = time.perf_counter() - t0
    pt_one = (counters.loads, counters.statements)
    monkeypatch.setattr(gate1_module, "_ground_template_outcomes",
                        gate1_module._ground_template_outcomes)

    # ── 3/4. deltas + duplication ───────────────────────────────────────────────────────────────
    base_keys = {t: {_key(c) for c in _cards(baseline[t])} for t in tables}
    pt_cards = {t: {_key(c): c for c in _cards(per_table[t])} for t in tables}
    base_names_anywhere = {k[0] for t in tables for k in base_keys[t]}

    additional = {t: [c for k, c in pt_cards[t].items() if k not in base_keys[t]] for t in tables}
    # a template is "duplicated" when the SAME name is now produced on more than one table
    name_tables: dict[str, set[str]] = {}
    for t in tables:
        for name, _ in pt_cards[t]:
            name_tables.setdefault(name, set()).add(t)

    dup_additional = {t: [c for c in additional[t] if len(name_tables[c["name"]]) > 1]
                      for t in tables}
    new_template_additional = {t: [c for c in additional[t] if c["name"] not in base_names_anywhere]
                               for t in tables}

    n_add = sum(len(v) for v in additional.values())
    n_dup = sum(len(v) for v in dup_additional.values())
    n_new = sum(len(v) for v in new_template_additional.values())

    # concrete duplicate examples: same template name on >= 2 tables, with the columns each binds
    examples = []
    for name, ts in sorted(name_tables.items()):
        if len(ts) < 2:
            continue
        examples.append({
            "name": name,
            "on": [{"table": t, "uses": pt_cards[t][(name, t)]["uses"],
                    "recipe": pt_cards[t][(name, t)]["recipe"],
                    "status": pt_cards[t][(name, t)]["validation_status"],
                    "was_in_baseline": (name, t) in base_keys[t]}
                   for t in sorted(ts)],
        })

    report = {
        "tables": tables,
        "baseline": {t: {"suggested": baseline[t]["summary"]["suggested"],
                         "clean": baseline[t]["summary"]["clean_ready"],
                         "needs_review": baseline[t]["summary"]["needs_review"],
                         "names": sorted(n for n, _ in base_keys[t])} for t in tables},
        "per_table": {t: {"suggested": per_table[t]["summary"]["suggested"],
                          "clean": per_table[t]["summary"]["clean_ready"],
                          "needs_review": per_table[t]["summary"]["needs_review"],
                          "names": sorted(n for n, _ in pt_cards[t])} for t in tables},
        "additional": {t: [{"name": c["name"], "uses": c["uses"], "recipe": c["recipe"],
                            "status": c["validation_status"],
                            "duplicate_of_other_table": len(name_tables[c["name"]]) > 1,
                            "template_new_to_catalog": c["name"] not in base_names_anywhere}
                           for c in additional[t]] for t in tables},
        "totals": {
            "baseline_total": sum(baseline[t]["summary"]["suggested"] for t in tables),
            "per_table_total": sum(per_table[t]["summary"]["suggested"] for t in tables),
            "additional": n_add,
            "additional_duplicate_template": n_dup,
            "additional_new_template": n_new,
            "baseline_empty_tables": [t for t in tables
                                      if baseline[t]["summary"]["suggested"] == 0],
            "per_table_empty_tables": [t for t in tables
                                       if per_table[t]["summary"]["suggested"] == 0],
            "additional_status": dict(Counter(
                c["validation_status"] for v in additional.values() for c in v)),
            "baseline_status": dict(Counter(
                c["validation_status"] for t in tables for c in _cards(baseline[t]))),
            "per_table_status": dict(Counter(
                c["validation_status"] for t in tables for c in _cards(per_table[t]))),
        },
        "cost": {
            "n_tables": len(tables),
            "n_templates": len(ALL_TEMPLATES),
            "baseline_all_tables": {"loads": base_loads, "statements": base_stmts,
                                    "secs": round(base_secs, 3)},
            "per_table_all_tables": {"loads": pt_loads, "statements": pt_stmts,
                                     "secs": round(pt_secs, 3)},
            "baseline_one_page": {"loads": base_one[0], "statements": base_one[1],
                                  "secs": round(base_one_secs, 3)},
            "per_table_one_page": {"loads": pt_one[0], "statements": pt_one[1],
                                   "secs": round(pt_one_secs, 3)},
        },
        "baseline_rejections": {t: [(r["name"], r["code"]) for r in baseline[t]["rejections"]]
                                for t in tables},
        "per_table_rejections": {t: [(r["name"], r["code"]) for r in per_table[t]["rejections"]]
                                 for t in tables},
        "duplicate_examples": examples,
        "column_counts": {t: len(cols) for t, cols in _COLUMNS.items()},
    }

    # ── diagnostic: is a template that per-table grounding finds LOST catalog-wide, or merely
    # filed on the wrong table? Read the catalog-wide grounding outcome for every additional name.
    wide = {o.template_id: o for o in templates_module.ground_all_outcomes(
        overlay_conn, ALL_TEMPLATES, catalog_source=SOURCE, roles=())}
    tid_of_name: dict[str, str] = {}
    for t in tables:
        for o in _per_table_grounder(t)(overlay_conn, ALL_TEMPLATES, catalog_source=SOURCE,
                                        roles=()):
            if o.feature is not None:
                tid_of_name.setdefault(o.feature.name, o.template_id)
    fate = {}
    for name in sorted({c["name"] for v in additional.values() for c in v}):
        o = wide.get(tid_of_name.get(name, ""))
        fate[name] = {
            "template_id": tid_of_name.get(name),
            "grounded_catalog_wide_on": (
                o.feature.grain_table if o is not None and o.feature is not None else None),
            "status": str(o.status) if o is not None else "?",
            "reason_codes": list(o.reason_codes) if o is not None else [],
        }
    report["catalog_wide_fate"] = fate
    report["totals"]["additional_lost_catalog_wide"] = sum(
        1 for v in additional.values() for c in v
        if fate[c["name"]]["grounded_catalog_wide_on"] is None)

    out = os.environ.get("PER_TABLE_MEASURE_OUT")
    if out:
        with open(out, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
    with capsys.disabled():
        print("\n" + json.dumps(report["totals"], indent=2))
        print(json.dumps(report["cost"], indent=2))
